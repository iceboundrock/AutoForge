# GitHub safety: source of truth, verification, merge, dry-run

Read this before changing any GitHub read or write (`src/autoforge/github.py`,
`GitHubClient`), any verification of an agent claim against GitHub
(`src/autoforge/validation.py`, the post-phase checks in
`src/autoforge/engine.py`), PR discovery, the merge gate and pre-merge evidence
(`src/autoforge/premerge.py`, `safety.*` and `merge.*` configuration), EPIC
maintenance, the EPIC dependency graph the parallel scheduler reads
(`src/autoforge/epic_graph.py`), or the dry-run mode.

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
reads the worktree itself, never the agent's word for it, and never
through a pointer the agent can rewrite: `HEAD` is read through the git
common dir after the strict import of the issue's private objects, in
every `sandbox.mode` ([Outer sandbox threat
model](#outer-sandbox-threat-model-adr-0005)). `HEAD` is
detached (a `HEAD` attached to a local branch is refused), equal to the
reported `head_sha`, different from the recorded base, descending from it
and from the branch head the entry recorded (so the push is a
fast-forward), and every commit of the published range passes the
commit-message policy ("Published content" below). `pr_title` and
`pr_body` pass the published-content policy, and the composed body (the
agent's body, `Closes #n` and the marker) passes the credential rule as a
whole. A worktree read or an ancestry proof that cannot be completed is
inconclusive (`VerificationError`, for `resume`), never a correction.

Then the candidate is validated (ADR 0006 D5.1, D5.2, #173): before the
precondition read below, the controller runs `validation.commands` against
the exact commit the check accepted (never `HEAD` read again), each
definition in a fresh export of its tree, reusing a record that already
holds the outcome for that commit, definition and policy. Nothing is read
for, planned or sent to the publication unless every definition passed. A
`FAIL` or `TIMEOUT` is saved with the phase's failure counter in one write,
nothing is planned, and the next step launches the agent again with the
failure in its prompt; the third failure of the phase entry blocks, naming
the candidate, the definition and the record, with nothing pushed or
created ([workflow.md](workflow.md), "Candidate validation gates").

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
   head still at the branch's old value is GitHub catching up, read again
   within the step ("GitHub catching up with a push" below) and
   transient once that wait is spent; any other head or base blocks, as
   does a default branch read again after such a wait and found changed.
3. The read-back: one complete listing of the open PRs, in which exactly
   one carries the issue's marker, and it is the PR the record observed,
   open, of this repository, headed at exactly the pushed candidate on
   `autoforge/<n>` and based on the default branch. A head still at the
   branch's old value, when nothing else is wrong, is waited for the same
   way and transient once the wait is spent; any other mismatch blocks,
   as does a default branch read again after such a wait and found
   changed. Only then is the PR bound (`current_pr_url`,
   `current_head_sha`, `current_branch`) and the review history reset.

A replan's replacement PR is held to the same marker rule, marker and
sole-claimant alike, by the transaction's target predicates on one
complete listing at binding, on the final read before the close, at the
confirmation after it and at activation, with the PR being superseded
excluded by identity ([replan-transaction.md](replan-transaction.md)), so
the PR the controller activates is one this entry finds again after a lost
state file. The controller creates that PR too (#164): the replan agent
commits on the default branch head the entry recorded and returns the PR's
title and body; the controller pushes the commit to the branch it derives
from the transaction id (a `push` record against an absent branch) and
creates the PR (`replacement_pr`), its body followed by `Closes #<n>`, the
issue's `ai-implementation` marker and the transaction marker, all
rendered by the controller. The PR is created only while that branch is at
the pushed candidate and bound only at exactly that head (replan-transaction.md,
"The controller publishes the replacement").

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

Then, before each launch (a correction relaunch too), the credential
preflight runs and the phase's read context is written ("Agents hold no
GitHub credential" below): a refusal stops the step before anything is
charged or saved.

### Before REVIEW

The reviewer makes no GitHub write in this phase (ADR 0004 K4, #162). It
reviews the bound revision and returns its verdict, its findings and its
prose sections; the controller renders the round's comment, posts it (a
`review_comment` record) and reads it back ("After REVIEW" below).

The journal is read first. A persisted completion context means the
round's result was already accepted and its comment planned: the phase is
completed from the record and no reviewer is launched. Otherwise the
reviewer is launched only after the controller has bound the revision it
is to review, all read from GitHub: the PR HEAD, its base branch, and the
merge base of the two (`repos/{owner}/{repo}/compare/{base}...{head}`,
`merge_base_commit.sha`, the commit the PR diff is computed from; a base
rewritten under its name moves it, ordinary commits on the base do not,
#96). A read that fails refuses the entry; nothing is launched against a
revision the controller could not name. It has then read the PR's
comments for a comment already carrying the `ai-review-result` marker of
the upcoming round at the bound HEAD, base and merge base:

- one or more: `BLOCKED` without launching the reviewer, naming the
  comment. The controller journals the round's comment before it posts
  it, so one it finds with no plan is not its own: a human's, one a
  reviewer of the previous contract posted (D13.5), or the journaled
  review of another AutoForge controller using the same login, from
  another machine, clone or state directory, which authorship cannot tell
  apart from the others (D9.3). It is never adopted, edited or duplicated
  (D9.6): adopting it would let any author define the round's findings,
  and the validated result it would need does not exist. `unblock` does
  not change that. The reason asks the operator to check who posted it
  (#415): a stray or foreign comment is deleted, or its marker removed,
  then `unblock` runs the round; another controller's comment is that
  run's review record, so it is not deleted, this run is not unblocked,
  and one of the two runs is stopped. Two or more are named together with
  the same check and remedies: the controller never chooses between them,
  and one of them may be another controller's. The entry's reason says
  what came before it, from the persisted attempt counter and whether the
  read precedes a relaunch: nothing launched on a first entry; before a
  relaunch (a correction or a transient retry), the launch just before,
  which ended without a usable result, and its run-log directory; on a
  resumed entry, an earlier launch of the round that ran or may have run,
  with no claim of a result. The same read after the reviewer returned
  (before anything is planned) blocks the same way, on one comment or
  several; its reason says a reviewer ran and names the run-log
  `control-result.json` that alone holds its result, never that nothing
  was launched.
- a comment for the same round at another HEAD, against another base, or
  from another merge base is not this round's: the round's identity is the
  diff it decided on, and a review posted before the PR was retargeted, or
  before its base was rewritten under its name, decided on another diff
- a marker that names no base, or no merge base, was written before the
  marker recorded one; it reviewed a diff nobody recorded, so it matches
  no bound key, but it is not a defect (a PR mid-flight carries one from
  every earlier round, and a defect would block every later entry on it)
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

Last, the bound HEAD and merge base are fetched into the shared object
store through the controller's transport, so the reviewer diffs them
locally and fetches nothing itself. A failed fetch propagates: nothing was
launched, and `resume` fetches again. A dry-run plan names these reads,
the fetch and the comment the step would post, and performs none of them.

The bound HEAD's validation evidence is then required (ADR 0006 D5.5,
#173): the records for that exact commit under the set and policy in force
are reused (the producer that published it validated it), and what is
missing runs now (a human's push), each in a fresh export. The
`review_evidence` binding naming them is saved with the reviewer's launch,
and the reviewer's prompt presents those records only: trusted provenance
for each, the output of a non-`PASS` one fenced as untrusted data. A `FAIL`
is presented, not refused; a clean verdict over it is refused as a
correction before anything is posted
([control-result-protocol.md](control-result-protocol.md), "Review results
and the controller's validation evidence"). A validation that cannot
conclude blocks with nothing launched or posted. A round completed from
its journal validates nothing.

### After REVIEW

The result is checked before it is accepted, so a refusal is an ordinary
correction retry with nothing posted:

- `round` and `reviewed_head_sha` are cross-checks, never targets: they
  must be the round and the HEAD the controller bound (D8.1). A reviewer
  reporting another round or HEAD is asked to correct it, never followed.
- the prose sections (`spec`, `standards`, `assessment`, `observations`,
  `verification`, `summary`) and every finding's title, location (as the
  code span the comment renders it in) and required resolution pass the
  published-content policy ("Published content" below)
- the controller renders the comment from the result: the heading, the
  binding line (the HEAD, base and merge base it bound, never the
  reviewer's), the findings, the prose sections under fixed headings, the
  needs-fix line and the marker, whose `needs_fix_round` and `finding_ids`
  come from the same result, so the comment cannot tell a different story
  from the findings the controller persists. Each finding is a heading
  line (id, classification, location, title) followed by its required
  resolution as a block of its own, at column 0 between blank lines, so
  every field keeps the code spans and fences it was judged with alone.
  The whole body passes the credential rule (D8.3), everything before the
  marker passes the mention rule as composed (D8.2), and the body fits
  GitHub's comment limit of 65,536 characters (D8.6); the per-finding
  bounds are unchanged.

Then, in order, all from one persisted plan:

1. The precondition read: the PR's comments are read again. A comment
   carrying the round's marker at the bound revision that appeared while
   the reviewer ran blocks with nothing planned or posted, as at entry.
   Otherwise the `review_comment` record (the rendered body) is saved with
   the completion context (the round, its verdict, its findings and the
   prose sections) in one save. Every load re-validates the findings and
   sections under the parser's rules, requires the body to be exactly
   the comment rendered from them at the revision the marker binds, and
   judges that comment as a whole again, so a body edited on disk after
   the save, or a context whose fields compose a comment the result path
   would have refused, is refused before recovery can post it.
2. The record is driven: reconciled before anything is posted, then
   posted at most within its attempt bound. The read-back is one complete
   comment listing in which exactly one top-level comment carries the
   round's marker, with exactly the planned body. A matching comment with
   another body, or two, is a conflict that blocks naming them; the
   controller never edits, duplicates or chooses between them.
3. The post-review re-read: a closed or merged PR refuses the round
   (`VerificationError`, nothing consumed); a HEAD, base or merge base that
   moved during the review makes it stale; otherwise its verdict decides.
   The round is judged from the persisted verdict and findings, never from
   the comment's text, against the revision its marker binds. The save that
   consumes the round drops the plan.

A crash anywhere in this sequence resumes from the plan: the comment is
posted at most once, and the round completes without relaunching the
reviewer. `unblock` into a phase whose plan is saved routes back to
`REVIEW`, which completes it the same way.

The posted comment's URL is the REVIEW -> FIX handoff artifact (#80). The
round persists the URL GitHub reported on the read-back, in its `pull/<n>`
form (`last_review_comment_url`, also recorded in `review_history`), and
the FIX prompt renders it as `REVIEW_COMMENT_URL`. The FIX prompt names
that comment as the authoritative review for the round at the reviewed HEAD
and tells the fixer not to substitute another PR comment or round; a human
comment, an earlier or stale round, or an unrelated bot comment on the same
PR is never the handoff because only the comment carrying the round's
marker at the bound HEAD, base and merge base can be verified. A round that
went stale (HEAD, base or merge base moved while the reviewer worked)
records its comment URL in its history entry and in
`last_review_comment_url` for the next REVIEW prompt's
`PREVIOUS_REVIEW_COMMENT_URL`, but launches no fixer, so a stale comment
never becomes a FIX handoff.

A clean round's comment is also what its review certificate is keyed by
(#174): the certificate names the comment's URL, id and marker key (round,
HEAD, base, merge base) as #162 read it back, or as its journal observed
it, and no comment, prompt or read context ever carries the certificate.

The reviewer is a full coding agent launched in the issue's worktree, and
the REVIEW prompt makes the phase read-only: it posts, commits, pushes and
edits nothing, and a defect it finds is a finding for the fixer, never its
own fix (#19). That is a prompt rule; what the controller enforces is the
binding above, and that a round comment it did not journal stops the round.
A reviewer that pushes anyway moves the HEAD its own round was bound to,
and the controller treats that exactly as any other push during the round:
the round is stale, consumed against the cap, its findings carried, and the
next round reviews the reviewer's commit. It is not made a hard error
because the post-review read cannot tell the reviewer's push from an
operator's or a fixer's, and blocking on every push during a round would
turn ordinary concurrent work on the PR into a human decision.

### Before FIX

The fixer publishes nothing (#163, ADR 0004): it commits on the
worktree's detached `HEAD` and returns one resolution per open finding.
The controller posts the acceptance comment the fixer asks for (#211),
creates the follow-up issues, appends the deferral markers and pushes the
commit itself, from a plan it journals before anything is sent ("After
FIX" below). The prompt offers the `acceptance_action` resolution as
`safety.acceptance_actions` allows (not at all under `off`) and lists the
actions the operator declined for the round. An operator action (#243,
ADR 0004 K10) is a comment on another issue that the operator posts and
the controller only verifies: the controller never posts one, under any
setting. The prompt offers the `operator_acceptance` resolution only when
the entry recorded allowed targets (below), lists the round's declined
operator actions and the operator comments already verified for the
round.

The journal is read first. A persisted completion context means the
fixer's result was already accepted and its plan saved: the phase is
completed from the journal (After FIX, steps 2 to 4) and no fixer is
launched. A PR head equal to the planned candidate is then the
controller's own push, never a reason to review first; only a head that
is neither the reviewed HEAD nor the candidate, or a base or merge base
that left the findings' binding, goes to `REVIEW`. A saved plan does not
bypass the rules below: they are applied to it again before it sends
anything (After FIX, step 2).

Otherwise the fixer is launched only while the PR HEAD, base and merge
base read from GitHub equal the reviewed HEAD, base and merge base the
open findings are bound to. A HEAD past it is an unverified push (an
operator, or a fixer of the previous contract); a base other than
`reviewed_base_ref` is a retargeted PR whose findings were raised on a
diff against another base (#95); a merge base other than
`reviewed_merge_base_sha` under the same base name is a base rewritten
under its name, whose findings were raised on the diff from the old merge
base (#96). In every case the review is stale and the phase goes to
`REVIEW` of the actual revision without launching the fixer, with the open
findings carried to that review as prior findings to re-check
(workflow.md, "Bind reviews to PR HEAD SHA and to the PR identity" and
"Stale rounds keep their findings"). A PR whose base or merge base cannot
be read is refused as a verification failure, as the review and merge
entries refuse it, never read as a retarget or a rewrite. The base is
compared only when `reviewed_base_ref` is set and the merge base only when
`reviewed_merge_base_sha` is set: a protocol-2 or protocol-3 state file
loaded in `FIX` lacks them, so its fixer is launched and the next completed
review writes the binding. A PR headed in another repository, with no
readable head repository (GitHub reports none when the head repository was
deleted, so a same-named branch of the run's repository is not proven to be
its head), or with no readable head branch, blocks: the controller pushes a
fix only to a branch it has proven is the PR's head in the run's
repository.

The controller then lists the repository's open issues (strictly: a
listing that may be truncated blocks, because "none exists" is then not
knowable) for the `ai-follow-up` marker of `{"finding_id", "pr"}`:

- exactly one per open finding: its URL is handed to the fixer in
  `FOLLOW_UP_ISSUES` as that finding's follow-up, to reuse, never to
  recreate; the fixer must resolve that finding as `follow_up_created`
  with that URL
- two or more for one finding: `BLOCKED` without launching the fixer
- a marker whose `finding_id` is not of the form `R<round>-F<n>` (or longer
  than the parser's bound), an issue carrying the same marker twice, or any
  other unreadable follow-up marker on an open issue: `BLOCKED` naming the
  issue, whatever PR the marker names
- a closed issue carrying the marker is not the finding's open follow-up
- the same listing's issues carrying this PR's marker for a finding of an
  earlier round go to the fixer as `EXISTING_FOLLOW_UP_ISSUES`; a finding
  that turns out to be one of those problems is resolved in the PR or, when
  deferring again is right, deferred to that issue (the controller appends
  the new finding's marker to it; an issue may carry several markers),
  never to a second issue
- the current issue carrying an open finding's marker: `BLOCKED` without
  launching the fixer. The current issue is never a follow-up of its own
  PR's findings, so the controller neither hands it over as one nor
  records a finding as deferred to it (PR #205 R3-F1); once the marker is
  removed, `unblock` starts a fresh entry

The entry observation records the PR branch at the reviewed HEAD and, per
open finding, its follow-up issue or none, and is saved before the launch
(D4.4). A re-entry after a launch of this entry (a correction relaunch, or
a `resume` after the fixer failed) honors it: a follow-up issue that
appeared or disappeared since, or a branch that moved, is not the
controller's and blocks with nothing created, appended or pushed; it is
never adopted (ADR 0004 §2.5). `unblock` starts a fresh entry that hands
such an issue to the fixer. The one-shot re-entry of a run upgraded while a
fixer of the previous contract was publishing (D13.6) has no observation to
honor: its follow-ups are handed over as found.

Unless `safety.acceptance_actions` is `off`, the entry also reads the run's
issue once and records in the observation the issues an operator action
may target (`operator_targets`, ADR 0004 D5.8): the issues its body
references (`#n` or an issue URL of the repository, first mention first,
at most 60), minus the run's issue and PR, plus the EPIC when it is in the
run's repository, each with why it is allowed, and the body's digest. A
conclusive read failure records none and offers no operator action; a
transient one propagates, nothing launched. Every relaunch of the entry is
held to that set, even when the issue body changes since.

Last, the reviewed HEAD is fetched into the shared object store through
the controller's transport (objects only; no ref moves), so the fixer
starts from it and contacts no remote. A failed fetch propagates: nothing
was launched, and `resume` fetches again. A dry-run plan names these reads,
the fetch and the writes the step would make, and every persisted record it
would reconcile first, and performs none of them.

### After FIX

The result is checked before it is accepted, so a refusal is an ordinary
correction (the fixer is asked again) with nothing created, appended or
pushed. The controller reads the worktree itself, never the fixer's word
for it:

- `previous_head_sha` is the reviewed HEAD the findings are bound to
- the resolutions cover exactly the open finding IDs, one each, as
  `fixed`, `follow_up_created`, `no_change_with_rationale`, (#211)
  `acceptance_action` or (#243) `operator_acceptance`
- an `acceptance_action` (at most one per result) is refused under
  `safety.acceptance_actions: off`, when the comment the controller renders
  from it (its header, the text, the finding's marker) fails the credential
  rule as a whole, the mention rule or GitHub's comment limit, when its
  action id is one the operator declined for this round, and once the
  operator declined `MAX_DECLINES_PER_ROUND` (16) actions of the round,
  the most its history keeps (PR #221 R3-F1)
- an `operator_acceptance` (at most four actions per result, never beside
  an `acceptance_action`) is refused under `off`, when the entry recorded
  no allowed target, at the same shared decline bound, for a target outside
  the entry's set, for a (finding, target) already verified this round,
  when its text links outside the run's repository, when the comment the
  controller renders from it (its header, the text, the action's marker)
  fails the published-content rules as a whole, and when its action id is
  one the operator declined for this round. Each target is read: one
  GitHub answers as missing or as a pull request is a correction; any
  other conclusive failure (transferred, another repository, no
  permission) blocks the result; a transient one propagates
- a finding whose follow-up issue the entry found is resolved as
  `follow_up_created` with exactly that issue
- any other deferral either names an issue the entry handed over (a
  finding's own follow-up or an earlier round's), never the current issue
  whatever marker it carries (an earlier round's marker on it hands it
  over, but a deferral to it is still refused), or asks for a new issue (`follow_up_issue`: bounded title and body, no
  marker); the composed body (the fixer's text, the controller's reference
  to the PR and finding, and the marker) passes the credential rule as a
  whole, and the new issues together fit what one FIX round journals
- `HEAD` is detached and equal to `head_sha`; it is either the reviewed
  HEAD itself (nothing committed, and then no finding is `fixed`) or a
  descendant of it, proven by the transport, every commit of whose range
  passes the commit-message policy ("Published content" below)
- a `fixed` finding's `commit_sha`, when given, is one of the commits after
  the reviewed HEAD up to the candidate

A worktree read or an ancestry proof that cannot be completed is
inconclusive (`VerificationError`, for `resume`), never a correction.

Then the candidate (the reviewed HEAD itself when nothing was committed) is
validated exactly as after `ANALYZE_EXECUTE`, before the precondition read
(ADR 0006 D5.1, D5.2, #173): a failure creates, appends and pushes nothing,
starts no review round, and relaunches the fixer with the failure in its
prompt, up to the same bound.

Then, in order, all from one persisted plan:

1. The precondition read: the PR is re-read, and a HEAD past the reviewed
   one is a push the controller did not make: `FIX -> REVIEW` of the
   actual HEAD with nothing sent and no fix recorded. A PR no longer
   proven headed at a branch of the run's repository (the entry's rule)
   blocks with nothing planned or sent. The open issues are
   listed again, and each open finding's follow-up must be the one the
   entry recorded; each issue a deferral names must be open in this
   repository, with a body the marker block can be appended to (the
   credential rule on the composed body). Anything else blocks with
   nothing planned or sent. Otherwise the `acceptance_comment` record
   (K9, first; its target the run's issue, derived from state, and its
   authorization the setting now: `policy` with its own id approved under
   `auto`, `operator` with none under `approve`) or the `operator_comment`
   records (K10, first, in result order; each target from the entry's set,
   with why it is allowed and the body digest the entry read, and no
   binding), the `follow_up_issue` records (K5,
   in result order, against a watermark read now), the `follow_up_append`
   records (K6, one per handed-over issue, its findings' markers in result
   order, against the digest of the body read now) and the `push` record
   (K1, the candidate, compare-and-swap against the reviewed HEAD; none
   when nothing was committed) are saved with the completion context in
   one save.
2. Before anything is sent, and again at every later entry that completes
   the plan from the journal (PR #205 R2), the entry's rules are applied
   to the saved plan:
   - the deferrals: one naming the current issue (a plan saved before that
     rule, or a reused follow-up the entry recorded as the current issue)
     blocks with nothing more sent; such a plan cannot be completed as
     saved, and the run starts again (PR #205 R3-F1);
   - the PR: a closed one is a `VerificationError` with nothing more sent,
     for `resume` once it is reopened;
   - the PR head: one that is neither the reviewed HEAD nor the candidate
     is the same drift (`FIX -> REVIEW`, the plan dropped; an issue it
     already created or a marker it already appended stays on GitHub as an
     earlier round's deferral for the next fixer);
   - the base and merge base: a PR retargeted from the reviewed base, or a
     base rewritten under its name, is the same drift, with the findings
     carried to that review (#95, #96). The merge base is read against the
     reviewed HEAD, not the PR's, so the controller's own push (a
     fast-forward that may merge the base in) is never mistaken for it. An
     unreadable base is a `VerificationError` with nothing sent; a merge
     base that cannot be read for a conclusive reason blocks;
   - the push target: the PR must still be headed in the run's repository
     at the branch the entry observed and the push record targets; a head
     repository GitHub no longer reports, a fork's, or another branch
     blocks with nothing more sent;
   - the open issues, against every resolution: a finding resolved as
     `fixed` or `no_change_with_rationale`, or deferred to a record not
     issued yet, has no open issue carrying its marker (one is not the
     controller's); a finding whose follow-up is settled (reused from the
     entry, or its record already `observed`, which the journal alone
     never re-proves) has exactly one, the issue the plan names. A closed,
     unmarked or duplicated follow-up, or an unjournaled marker, blocks
     with nothing more sent; the controller never adopts, chooses between
     or recreates follow-up issues. A record issued but not read back yet
     is left to its own reconciliation, which reads the same marker before
     anything is sent again.
3. The records are driven in plan order. Operator comments come first,
   in a prefix pass that sends nothing (#243): each is verified from its
   target's complete comment listing (its exact payload, or the comment
   the operator bound, unchanged; ADR 0004 K10), and its observation or
   conflict saved before the next is read. The pass does not stop at the
   first one still waiting; while any is not verified, the step blocks
   naming each (its id, target, finding, payload size and SHA-256, and the
   `status --action`, `unblock --bind-evidence` and `--decline-action`
   commands; see
   [BLOCKED on operator actions](../usage.md#blocked-on-operator-actions)),
   and nothing later in the plan is sent. Otherwise the acceptance
   comment comes first: it is posted only once authorized and while the
   run's issue is open in the PR's repository, and read back from the issue's complete
   comment listing as exactly one comment with its marker and its exact
   bytes (whoever posted it), adopted only while the issue is still open
   in the PR's repository (PR #221 R1-F2). Until then it is held: nothing
   at all is sent (an issue that stops qualifying between the comment's
   write and its read-back holds it the same way, the record `attempted`
   and the reason saying the comment was sent, PR #221 R2-F1) and the
   step blocks with a reason the controller renders
   (the action id, the finding, the payload's size and SHA-256, the push
   held back and the worktree holding it, and the ways forward; see
   [Workflow: leaving BLOCKED](workflow.md#leaving-blocked-the-operators-unblock)).
   A marked comment with other bytes, or two, is a conflict. Then the push, the commit point, is
   issued only after every follow-up is on GitHub, and, after any
   follow-up write, only once the PR is read again and held to step 2's
   rules (open, head, base and merge base, push target) and the open
   issues are read again and agree with every resolution as in step 2
   (PR #205 R2-F2, R3-F2). Otherwise nothing is pushed and the follow-ups
   stay on GitHub: a closed PR is a `VerificationError` for `resume`, a
   moved head or base is the same drift, and anything else blocks. A body edited between the
   plan and the append is rebased (D5.5) within the size and credential
   rules. A push refused by its lease while the PR head moved is the same
   drift, never forced; any other conflict blocks naming the record and
   the repair, and `unblock` reconciles it again within its attempt bound.
4. The read-back: one complete listing of the open issues, checked against
   every resolution as in step 2 (each deferred finding has exactly one
   open issue carrying its marker, the issue its record created or
   appended to, or the one the entry handed over, and no other finding has
   any); the PR headed at the candidate; and its base and merge base still
   the ones the findings are bound to (otherwise the same drift: the
   candidate is reviewed as stale, no fix recorded); no deferral naming
   the current issue; and the PR still headed in the run's repository at
   the branch the plan pushed to. A target lost after the push (a head
   repository GitHub no longer reports, a fork's, another branch) blocks
   with the fix not recorded and the findings open; the plan stays saved,
   so `unblock` completes it from the journal and sends nothing GitHub
   already holds (PR #205 R3-F2). A head still at the
   reviewed HEAD after the push, when the base, the merge base and the
   push target all hold, is GitHub catching up, waited for within
   the step ("GitHub catching up with a push" below) and transient
   once that wait is spent; one of them lost while the head still lags
   is routed as above at once, and any other head is drift. After such a
   wait the open issues are listed and checked again, since a follow-up
   can be closed or unmarked during it. Only then are the
   resolutions recorded
   (`last_fix_resolutions`, each deferral with the URL GitHub returned, and
   an acceptance action with its verified comment's `acceptance_comment_url`;
   `acceptance_outcomes` records it `verified`; an operator acceptance with
   its `operator_evidence`, each verified comment's URL, evidence class and
   digests, and `operator_comment_outcomes` records each `verified`),
   the open findings cleared and the phase advanced to `REVIEW`, whose
   prompt renders those resolutions as `PREVIOUS_FIX_RESOLUTIONS`: data
   for the reviewer to check, never a reply posted on the PR.

The marked issue is the durable record of a deferral: state never records
a resolution GitHub contradicts.

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
3. A `next_issue_url` of `null` is confirmed on GitHub before anything
   reports the EPIC complete ("A `null` selection" below, #287).
4. The roadmap section is written when one is stored and merges are
   pending (EPIC updates, below).
5. A selected `next_issue_url` is verified like the first issue in
   `INITIALIZING`, and as one of the EPIC's own issues, before switching
   issues:

- it parses as an issue URL of the configured repository
- it is neither the EPIC nor the issue just finished
- the issue exists and is `OPEN`
- it is a native sub-issue of the EPIC with none of its own (#287): its
  issue object (`get_issue_node`, `GET .../issues/{n}`) names the EPIC as
  `parent_issue_url`, its `sub_issues_summary.total` is 0, and its state
  in that read is `open`. The issue read and this one are two reads, and
  the later one decides: an issue closed between them is refused, never
  switched to on the earlier answer

Identity checks (EPIC, just-finished issue, parent) compare repository
case-insensitively plus issue number, never URL strings: GitHub owner and
repository names are case-insensitive.

The last rule is the one `autoforge epic run` admits by ("EPIC dependency
graph" below; ADR 0008 D3.1, D3.6). Before it, any open issue of the
repository was accepted, so on a public repository an issue a stranger
opened could be chosen as the next task by injected text. An issue the
EPIC body mentions or lists in a task list, but that GitHub does not name
as the EPIC's sub-issue, is refused: prose is never authority. The first
issue is the operator's choice and is not held to the rule. Blockers are
not read: the order of a sequential run stays the agent's choice. On the
parent read, a not-found is a bad selection, a transient failure takes the
bounded retry below, and any other failure (an answer without the
sub-issue summaries included) is conclusive. The agent is shown the EPIC's
sub-issues, each with its state and title, and the EPIC's own count of
them as `epic-sub-issues.md` of its read context, read with
`get_issue_node` and `list_titled_sub_issues` (the listing read with each
row's title kept, since a sub-issue the EPIC body never names would
otherwise reach the agent as a bare URL). A listing that holds another
number of issues than the EPIC counts says it is incomplete. That file is
a convenience; the controller decides from its own reads.

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
again. Never switch to an unverified issue, and never end the run on an
unverified `null`. A rejected selection has no effect the controller did
not already verify: the progress comment is posted once and never again, a
roadmap section already written was read back and closed its batch, and no
issue was switched.

#### A `null` selection

`null` is the agent's claim that none of the EPIC's issues remains, and it
may rest on a read context that showed the listing unreadable or short.
The controller reads the EPIC's node and its sub-issue listing itself,
complete by the rules the scheduler's snapshot applies
(`epic_graph.read_children`: within the bound of 100, de-duplicated by
id, each row naming the EPIC as its parent, and exactly as many distinct
issues as the EPIC's `sub_issues_summary.total`). The count matters:
GitHub leaves a sub-issue the token may not read out of the listing, so
only the comparison says one is missing, and a missing one may still be
open. A sub-issue remains when the controller would accept it as a
selection: in the run's repository, open, without sub-issues of its own,
not the issue just finished, and not owned by a scheduler namespace. So
an open nested EPIC, or a sub-issue in another repository, does not keep
the run going.

- Nothing remains: the roadmap section is written when due and the run
  enters `DONE`.
- One remains: the `null` is a rejected selection, naming the issues that
  remain (at most ten), and takes the bounded retry above. A second
  rejection enters `BLOCKED`: the run does not end as complete while the
  EPIC lists work it could still do.
- A transient failure of either read takes the same bounded retry.
- A listing that is not complete (the count differs, a bound is exceeded,
  a row is inconsistent), or a conclusive failure of either read, enters
  `BLOCKED` at once, without invoking the agent again: asking again would
  not make GitHub list what it left out.

This check comes before the roadmap splice, so a refused `null` writes no
section reporting the EPIC complete and leaves the merge counter as it
was; the progress comment stays posted.

### Before MERGE

Verify:

- PR remains open
- PR is mergeable
- the PR's check results were read, with the PR, in one checks read
  ("The PR identity read and the checks read" below); GitHub refusing this
  credential them is `BLOCKED`, never "no checks" and never passed
- the status rollup holds at least one check (#289): a rollup read and
  empty is `BLOCKED` under every configuration, `verify_check_definition:
  false` and `required_checks: []` included, because "every check passed"
  over no check is vacuous
- every check in the status rollup passes (all checks, not only the
  required ones)
- at least one of them actually succeeded (#289): a check run that
  concluded `SUCCESS` or a commit status in state `SUCCESS`. `NEUTRAL` and
  `SKIPPED` fail nothing, but a rollup of only those is `BLOCKED` under
  every configuration too: an automatic merge never passes only because no
  check failed
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

## The PR identity read and the checks read

`GitHubClient` reads a pull request in one of two ways (#291):

- **The identity read** (`get_pr`): state, HEAD, base, branch,
  mergeability, draft, auto-merge, body, head repository and closing
  issues. It does not request `statusCheckRollup`, and its `checks` is
  `None`, which means *not read*: it is never "no checks". Every caller
  that binds or verifies a PR without judging its checks uses it: the
  `REVIEW` and `FIX` bindings and read-backs (`_require_open_pr` and its
  callers), PR discovery and adoption in `ANALYZE_EXECUTE`, the adopted
  PR's effect reconcile, the replan transaction, `unblock`, the post-merge
  re-read and `pr_exists`. A listing (`list_open_prs`, `list_all_prs`,
  `list_prs_for_head`) carries no check results either.
- **The checks read** (`get_pr_with_checks`): the same fields and the
  head's status-check rollup in one `gh pr view` answer, so the checks
  judged are those of the HEAD read with them. Its `checks` is always a
  list, and `[]` is GitHub reporting no check for that head. A rollup that
  is not a list of objects is a conclusive `GitHubError`, never a shorter
  list: an entry dropped as unreadable could be the failing one. Only the
  merge gate (`READY_FOR_MERGE`, `MERGE`) and `REVIEW`'s read context make
  this read.

GitHub refuses a fine-grained personal access token the check runs of a
private repository's pull request, and no permission it can be given
changes that. It answers `Resource not accessible by personal access token`
on a `statusCheckRollup` path, and `gh` then fails the whole view. The
client classifies that answer, and the same answer for a GitHub App
installation token (`... by integration`) on a `statusCheckRollup` path, as
`GitHubChecksAccessError`: conclusive, never retried, never a not-found and
never `[]`. The classification reads GitHub's answer, never the command
line (which names `statusCheckRollup` itself); the same phrase on another
path, and a bare HTTP 403, are not this error. A 404, a transient failure
and an authentication failure keep their own classes. `pr_exists` answers
`False` only on a conclusive not-found; a refused or failed read
propagates, since it is not a PR that does not exist.

Where the checks read is refused:

- `REVIEW` proceeds. Its read context's `checks.md` says that GitHub
  refused the controller's credential the check results, so they are
  unknown (neither "no check" nor "every check passed"), and that the merge
  gate reads them again. The PR, HEAD, base and merge base the review is
  bound to come from the identity read and are verified as before.
- `FIX` makes no checks read.
- `READY_FOR_MERGE` and `MERGE` are `BLOCKED` on the first refusal, before
  anything else is judged. The reason names the refusal, the
  credential and its remedy (`gh auth login`, an OAuth login, or a classic
  token with the `repo` scope; a GitHub App installation token needs Checks:
  read), and ends with `unblock`. Nothing is merged or counted and no
  re-check is spent: reading again returns the same refusal. HEAD-SHA and
  PR-identity binding, the review certificate and the check-definition
  comparison are unchanged; they run only on a checks read that succeeded.
- One identity read follows the refusal, only to tell an already merged PR
  apart (a `MERGE` crash recovery, or a human's merge that `unblock` sent to
  `READY_FOR_MERGE` to reconcile). The reason then says the PR is already
  merged, at which head, and that the controller cannot reconcile or count
  that merge with this credential; it is still `BLOCKED`, never reconciled
  on the identity read, so the run cannot reach `UPDATE_EPIC` until a
  credential that may read the checks is in place. An identity read that
  fails, or answers with another PR, keeps the general reason.

`doctor`'s checks-read row makes the same read before a run
("Doctor's checks-read check" below).

### GitHub catching up with a push

GitHub moves a pull request's head asynchronously after a push to its
branch: the branch can show the new commit while the PR (`headRefOid` of
the identity read and of a listing) still shows the old one (on PR #402
apparently for about 13 s, inferred from its update time, not measured).
Three reads come right after the controller's own push: `ANALYZE_EXECUTE`'s
check before it adopts a PR, its read-back of the published PR, and `FIX`'s
read-back. Each treats a PR still headed at the commit the push replaced
(the branch's old value, the reviewed HEAD) as GitHub catching up, when the
checks it makes before the head pass. Such a read is made again after each
pause of `PUSHED_HEAD_WAIT_SECONDS` (1, 2, 4, 8, 15, 15 and 15 s, 60 s in
all) within the same step, and the step goes on as soon as a read is
anything else (#403); the run's progress output says that it waits. Every
read is held to the rules of the first, so a head that turns into a third
commit is still drift or a block, and a read that fails ends the wait with
its error. A head still there after the last pause is
`GitHubUnavailableError`, and `resume` reads it again.

Waiting only for the head means a read that fails any other rule is not
waited for, and what the step read before the wait is read again after
it (PR #404). `FIX` keeps waiting only while the PR's base and merge base
are still the ones the findings are bound to and its head branch is still
the plan's push target, so a retarget or a base rewritten during the wait
goes to `REVIEW` at once and a lost push target blocks, instead of ending
as a transient lag; after a wait it lists the open issues and checks every
resolution again before recording the fix. `ANALYZE_EXECUTE` reads the
default branch again after a wait and blocks, adopting and binding
nothing, when it is no longer the one the step read before its push.

The wait is fixed, not configured: 60 s is several times the one lag seen
so far, and short enough not to leave a real outage unreported for long.
The re-reads change no state and send nothing: the plan and the push
record are saved before the read-back.

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
- write an agent's read context: the rendered prompt names a placeholder
  instead of the per-launch directory.

Never rely only on telling an LLM "do not modify anything".

---

## Controller-owned effects

ADR 0004 (`docs/adr/0004-authority-boundary-and-typed-external-effects.md`)
moves every externally visible write to the controller. #160 adds the
mechanism and wires its first consumer, the `UPDATE_EPIC` progress comment;
#161 wires `ANALYZE_EXECUTE` (the push of the agent's commit and the
implementation PR, opened or adopted); #162 wires `REVIEW` (the round's
review comment); #163 wires `FIX` (the follow-up issues it creates, the
deferral markers it appends and the push of the fixer's commit, last);
#164 wires `REPLAN_REEXECUTE` (the push of the replan agent's commit to the
derived replacement branch, then the replacement PR). Every phase that
publishes now does so through the controller; no agent makes a GitHub
write or pushes. #211 adds one kind, K9: the acceptance comment a fixer
may ask for on the run's own issue, which also needs an authorization
(`safety.acceptance_actions`: the policy, or the operator's approval of its
action id, a hash of its owner, target, marker and exact bytes that is
recomputed from the record, so an approval never carries over to other
bytes). There is no generic write: the agent supplies only the text, and a
URL, target, method, path or command in its request is refused. #243 adds
K10, the first operator-performed kind: a comment on another issue of the
repository that the operator posts and the controller only verifies. It
adds no write: its operation is read-only (`OperatorCommentOp.issue`
raises), `drive` refuses its record and `effect_ops.verify` is its one
path, so no setting makes the controller post one (ADR 0004 D2.1a).

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
- **Completion is judged on GitHub's exact text.** `gh` rewrites the JSON
  it prints, so a create's read-back that finds one object with its
  identity, in place, whose title or body `gh` printed otherwise, or whose
  payload holds a `^` (which another text can print as), reads that object
  once more past `gh` (the exact read, below) and judges it there.
  Equality stays byte for byte.
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
- **Every controller `gh` process** (`github.gh_workdir`,
  `github.gh_request`, #283) starts in a fresh empty directory the
  controller creates in the temporary directory (outside the checkout
  unless `TMPDIR` lies inside it) and removes once `gh` exits,
  with `GIT_DIR` naming that directory, so any git `gh` runs finds no
  repository and exits: not the checkout, not one a `GIT_DIR` of the
  operator's shell names, not one around a temporary directory inside a
  checkout. No call relies on its working directory to name the
  repository: every one names it, and the repository a run starts in is
  read from the checkout's `origin` through the hardened local request
  and then from GitHub (`gh repo view <owner>/<repo>`), which follows a
  rename. A relative `github.command` is made absolute first, and so is
  each relative `PATH` entry the child inherits, so a bare `gh`, and
  every program `gh` starts, is looked up as from the controller's own
  directory: left relative, `..` would reach the shared temporary
  directory the empty one was made in. `doctor`'s
  `gh --version` and `gh auth status` run the same way. The one `gh` that
  runs elsewhere is `gh auth git-credential`, which git starts as the
  credential helper of the transport's own fetch and push, in the private
  git directory the transport made for it (above).
- **Every other controller git process** runs in the operator's repository
  through `git_transport.local_git_request` (ADR 0004 D7.3, D7.5): the
  run's `origin` read at `INITIALIZING`, the
  per-issue worktree's `git worktree add` and its identity read, the
  candidate read and the strict import, the workspace reader
  (`rev-parse`, `symbolic-ref`, `status`), the lock's common-dir read and
  the pre-merge export. Hooks and the file-system
  monitor are off at command-line precedence, and so are replacement
  objects and the commit-graph. The child starts from
  `LOCAL_GIT_ENV_ALLOWLIST`, with no token and no `GIT_*` variable of the
  operator's shell, so nothing a hook, monitor or `GIT_CONFIG_*` planted in
  the shared repository or the shell could start runs with the controller's
  credential. A filter driver still runs during a checkout (the per-issue
  worktree's), with no more authority than the agent; the pre-merge export
  reads objects only and runs none ("Controller-run verification commands"
  below). `autoforge doctor` is an operator
  diagnostic outside any run and keeps its own requests: it runs only
  `git --version`, `rev-parse --show-toplevel` and `remote get-url`, none
  of which starts a hook, the monitor or a filter, plus the launch
  preflight's `git config --list`, which goes through the hardened local
  request like every controller git process (#165).

### `gh`'s sanitizer and the exact read (#327)

`gh` rewrites every JSON reply before it prints it: go-gh's
`asciisanitizer` is installed on its HTTP transport (seen in gh 2.103.2,
which links go-gh v2.17.0). Each C0 or C1 control character, and its JSON
escape, becomes caret notation, and so does the *text* of an escape: a
body holding the six characters `\u001b`, which GitHub's JSON carries as
`\\u001b`, prints as `\^[`, three characters shorter. The sanitizer works
on the reply's bytes before any JSON is decoded; `gh`'s cache and its
`GH_DEBUG` log see the rewritten reply too, and no flag, Accept type or
command turns it off. It is a terminal protection and it does its job, but
it makes every `gh` read of a title or body **lossy** for the controller:
`\^[` is also what the literal text `\^[` prints as, so no comparison of
what `gh` printed can prove that an object holds a given text. Normalising
the payload, or treating `\^[` and `\u001b` as equal, would accept a body
the controller never wrote.

That is how #327 was found: the body of PR #314 (#284's run) explained
terminal output and held `\u001b[0m` as text. The create landed byte for
byte, the read-back printed by `gh` was 4405 characters against the
journal's 4408, and the record became a conflict on the controller's own
PR.

**The exact read.** `GitHubClient.get_pr_exact`, `get_issue_exact` and
`get_comment_exact` read one object with GitHub's REST API directly:

- one `GET` of `https://api.github.com/repos/<owner>/<repo>/pulls/<n>`,
  `.../issues/<n>` or `.../issues/comments/<id>`, the path built from a
  parsed `github.com` URL (`_repository_path`), so no other host,
  repository or path is reachable;
- the credential is the one `gh` holds for `github.com`, printed by
  `gh auth token --hostname github.com` and sent as a bearer token. It
  stays in memory for the one request: never in argv, a file, the state,
  a log or an error, and `HttpsRequest`'s repr leaves the headers out;
- TLS that verifies the chain and the host name against the system's
  trust store, with no key log (`SSLKEYLOGFILE` is ignored: it would write
  the secrets of the connection that carries the credential to a file); no
  redirect followed (a 3xx would carry the credential elsewhere; it is a
  conclusive error); the client's `timeout_seconds` on each socket
  operation of connecting (the TCP connection, the TLS handshake, sending
  the request), and on reading the whole reply from the start of the call:
  the status line, the headers, a chunked body's framing and trailers, and
  the body, every receive waiting at most until that deadline, so a server
  that trickles any of it cannot hold the controller (a proxy's answer to
  the tunnel is read the same way); at most
  `EXACT_READ_MAX_BYTES` (2 MiB) of a body and `EXACT_READ_ERROR_BYTES`
  (4 KiB) of an error reply read;
- read-only: no other method exists, and nothing on GitHub changes.

Its failures are classified as `gh`'s are, and none of them is ever a
reason to write. No connection, a timeout, a 5xx, a 429 or a rate-limited
403 (by its headers, or by GitHub's message, which a secondary limit may
send alone) is `GitHubUnavailableError`: nothing is saved or sent and `resume`
reads again (after a write, `EffectPending`, the record still
`attempted`). A 404 is `GitHubNotFoundError`; a 401, any other 403, a
redirect, a certificate that does not verify, a reply that is not one
`application/json` object within the bound, an answer naming another
object, or a `gh auth token` that fails or prints anything but one visible
token is a conclusive `GitHubError`, and the phase blocks with nothing
sent again. No message quotes the reply; an error reply is read only to
tell a rate limit from a refusal.

**When it runs, and what stays strict.** Only in a create's completion
read-back (K2 implementation PR, K4 review comment, K5 follow-up issue,
K7 replacement PR, K8 progress comment, K9 acceptance comment), and only
when exactly one object carries the identity, every condition other than
its text holds on `gh`'s read (for a PR: open, on the planned head branch
of the run's repository, onto the planned base, among the head's PRs and
above the watermark where there is one; for a follow-up issue: open, above
the watermark, in the repository, not the run's issue; for a comment: on
the parent's complete listing), and either its title or body differs on
`gh`'s read or the payload's title or body holds a `^`. Every rewrite of
the sanitizer leaves a `^`, so a payload without one can read back equal
only from its own bytes, while one with `^C` in it also reads back equal
from an object holding the control character `\x03` there. The
whole completion predicate is then judged again on the exact object: the
title and body byte for byte, and every other condition once more, so a PR
closed in between is not adopted on its text. Two objects carrying one
identity are a conflict without any exact read; so is one whose other
conditions fail, which no sanitizer explains. A payload without a `^`
whose object `gh` prints as it is takes no exact read and no extra request.
The persisted payload is never changed to fit a read-back.

**Terminal protection is not turned off.** Everything `gh` prints still
goes through its sanitizer. The exact read's text is compared and then
dropped: it is never printed, logged, put in a conflict reason or an
error, or handed to an agent. The read context an agent gets is still
`gh`'s sanitized read.

**Not covered yet.** The base an append is composed on (K3 adoption, K6
follow-up append) and the EPIC roadmap edit read the body through `gh`, so
a base holding escape text or a control character would be rewritten by
the controller's own write; K10 evidence compares a comment `gh` printed.
These are follow-up work (#328), not part of #327. So are the open
points of the exact read itself (#329): how a 404 or a credential failure
of the exact read is classified in K7, where any conclusive failure ends
the replan, and a `doctor` row for it.

**Troubleshooting.** A create that blocks with *"… carries its identity
but is not the object the controller intended (another body, title, head
or state)"* on what looks like the controller's own object: with this
version the read-back already judged it on its exact read, so the object
really differs (a human edit, another head or base, a closed object);
compare it with the payload in `status --json`. A run blocked that way by
an earlier version, on an object whose body holds `\u001b`-like text,
recovers with `unblock`: the conflict reopens with its attempt count kept,
the read-back runs again with the exact read and observes the object, and
nothing is written again. An exact read that keeps failing names its cause
in the reason: `gh auth status` and `gh auth token --hostname github.com`
must work for the user running AutoForge, and `api.github.com` must be
reachable (the proxy variables apply, as they do to `gh`).

### Published content

Agent text the controller publishes under the operator's identity
(`progress`, `roadmap_section`, `pr_title`, `pr_body`, a review's prose
sections and its findings' titles, locations and required resolutions, a
new follow-up issue's title and body, an acceptance comment's body, an
operator comment's text, which the operator posts under their identity
(#243), and the payloads of later kinds) is
refused by
`result_parser.published_text_problem`, and the agent is asked to correct
it, when it contains:

- an HTML comment opening a controller marker (`<!-- ai-` or
  `<!-- autoforge-`, any spacing or case);
- a credential class `redaction.credential_classes` recognises (named, never
  quoted);
- a closing keyword followed by an issue reference, even inside code;
- an `@`-mention outside a code span or fenced block.

`published_payload_problem` applies the credential rule to a whole payload,
so a credential split across fields is refused too.
`published_markdown_problem` applies the mention rule to Markdown the
controller composes from several fields (the review comment before its
marker): a fence one field leaves open and a later field closes, or raw
HTML in one field, can leave a mention outside code that each field alone
kept in code.

An operator comment's text (#243, ADR 0004 D8.3) is held to more: at most
`MAX_OPERATOR_COMMENT_CHARS` (16000), no image and no raw HTML outside code
(`operator_markup_problem`), and every link or bare URL inside the run's
repository (`operator_comment_link_problem`: no other host, repository or
scheme, no relative, `www.` or e-mail link, no `..`, also once entity and
percent escapes are decoded). The controller applies the link rule when it
checks the result and again when it loads a saved record, and judges the
whole rendered payload (header, text, marker) as above.

A commit the controller pushes is published too. Every commit of the range
from the base to the candidate is read back by the transport and refused
(`result_parser.commit_message_problem`, named by SHA, never quoted) when
its message contains a credential class, or a closing keyword naming any
issue other than the run's own (`#n`, `owner/repo#n` or the issue's URL):
GitHub would close that issue once the commit reaches the default branch.

## Agents hold no GitHub credential

ADR 0004 moved every externally visible write to the controller; #165
removes the authority to make one from everything else AutoForge launches.
Agents and repository-defined commands (`validation.commands`,
`merge.verification_commands`) receive no GitHub or git credential through
anything AutoForge controls, so a prompt-injected agent that tries a write
fails for lack of a credential, not for lack of an instruction. The
mechanism is `src/autoforge/child_env.py`, provider-neutral and applied by
the engine to all three kinds of launch the same way; Pi keeps #132's
policy (docs/pi-policy.md).

### The operation classification

The classification of ADR 0004 D1.2 names operations, not tools; #148 and
#149 cite it. Every mutation the workflow makes and every `gh` and `git`
operation the prompts used to name is in one class.

| Operation | Class | Who performs it |
|---|---|---|
| push a branch: the implementation, a fix, a replacement | controller-only | the controller, effect K1 |
| open or adopt the implementation PR, the replacement PR | controller-only | the controller, effects K2, K3, K7 |
| post a round's review comment | controller-only | the controller, effect K4 |
| create a follow-up issue, append a follow-up marker | controller-only | the controller, effects K5, K6 |
| post the EPIC progress comment; splice the EPIC roadmap | controller-only | the controller, effect K8 and the roadmap write |
| post the acceptance comment on the run's issue | controller-only | the controller, effect K9, once authorized (#211) |
| post a comment on another issue of the run's repository as acceptance evidence | controller-only | the operator; the controller never posts it and verifies it as effect K10 (#243) |
| close, reopen or comment on the source PR of a replan | controller-only | the controller's replan transaction |
| merge; disable auto-merge | controller-only | the controller, behind `safety.allow_merge` |
| delete the merged PR's head branch on GitHub | controller-only | the controller, after a verified merge, behind `merge.delete_branch` (#283) |
| edit a comment in place, close an issue, delete any other branch, labels, assignees, milestones, any other REST or GraphQL mutation | controller-only | nobody: there is no kind for it |
| read an issue, a PR, comments, checks, the EPIC and its issues | read-only external | the controller, into the agent's read context (below) |
| `git fetch` of the revisions a phase works on | network read | the controller, before the launch (objects only) |
| read the diff of the bound revision | agent-local | the agent, `git diff` over what the controller fetched |
| commit on the worktree's detached `HEAD`, `git rev-parse`, `git log` | agent-local | the agent; never load-bearing |
| create, delete or move a local branch or a worktree | operator-owned local | nobody (the controller's single `worktree add` aside) |

### The authority rule

**A provider interaction approval never grants a controller-only
operation** (ADR 0004 D1.3). An approval (a provider permission prompt or
mode, an interaction answer, an SDK permission callback) changes what a
provider tool may *attempt*; it never changes which credentials the process
holds. The rule holds by construction: the credential a controller-only
operation needs is absent from every agent and repository-command runtime.
Provider permission modes and deny lists are defence in depth at most,
never the boundary.

### What every launch gets

- **Names never inherited.** `FORBIDDEN_ENV_NAMES` and
  `FORBIDDEN_ENV_PREFIXES`: gh's tokens and configuration location
  (`GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`,
  `GITHUB_ENTERPRISE_TOKEN`, `GH_HOST`, `GH_CONFIG_DIR`), the programs git
  and ssh run for a credential or a connection (`GIT_ASKPASS`,
  `SSH_ASKPASS`, `GIT_SSH`, `GIT_SSH_COMMAND`), the SSH agent socket
  (`SSH_AUTH_SOCK`), git configuration from the environment
  (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_COUNT`,
  `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_KEY_*`, `GIT_CONFIG_VALUE_*`), the
  session bus a desktop keyring answers on (`XDG_RUNTIME_DIR`,
  `DBUS_SESSION_BUS_ADDRESS`), where curl and wget find the
  configuration file they read on every request (`CURL_HOME`, `WGETRC`,
  `WGET2RC`), `NETRC`, the `.netrc` that curl 8.16 and later, Python's
  `requests` and Go read in place of `~/.netrc`, and where a shell finds a
  startup file it runs before a command (`BASH_ENV`, which every
  non-interactive bash runs; `ENV`, an interactive sh's; `ZDOTDIR`,
  zsh's). None is in the default allow-list (`SHELL` no longer is either:
  the controller sets it), and
  config load refuses any of them, or a `*` prefix covering one, in
  `execution.env_allowlist`, `env_allowlist_extra` or an adapter's
  `environment_names`, naming the variable and the rule and never a value.
  One check (`refuse_forbidden_env`), with no provider branch in the engine.
- **Variables the controller sets**, over the allow-listed environment, for
  one launch, from a directory it creates (`mkdtemp`, mode 0700) and removes
  when the launch ends:
  - `GH_CONFIG_DIR`: an empty directory, so `gh` finds no `hosts.yml`;
  - `GH_TOKEN` and `GH_ENTERPRISE_TOKEN`: the placeholder
    `autoforge-no-github-credential`. With no token in its environment or
    configuration `gh` falls back to the keyring, which any same-UID
    process reaches on macOS; a token in the environment is used first, and
    GitHub refuses this one;
  - `GIT_CONFIG_GLOBAL`: a file holding only the author and committer
    identity (`user.*`, `author.*`, `committer.*`) copied from the
    operator's global git configuration, so `~/.gitconfig` and
    `$XDG_CONFIG_HOME/git/config` are not read, and an agent's
    `git config --global` writes land in that file;
  - `GIT_TERMINAL_PROMPT=0`, and at git's command scope
    (`GIT_CONFIG_COUNT`), which git reads after every configuration file and
    which no file can override: `credential.helper=` (an empty value clears
    the helper list, URL-specific helpers included), `core.askPass=`,
    `http.extraHeader=` and `core.sshCommand` set to
    `ssh -F /dev/null -o IdentityFile=none -o IdentitiesOnly=yes -o IdentityAgent=none -o ControlPath=none -o BatchMode=yes`;
  - `CURL_HOME`: a directory holding an empty `.curlrc`, and `WGETRC`
    (wget 1.x) and `WGET2RC` (wget2): an empty file. curl reads the first
    default configuration file it finds, `$CURL_HOME/.curlrc` before the
    one in `$XDG_CONFIG_HOME` (`curlrc` in curl 8.18, `.curlrc` in 8.5),
    `~/.curlrc` and the password database's home, and wget reads the file
    its variable names instead of
    `~/.wgetrc` or `~/.config/wget/wget2rc`. Either file can add an
    `Authorization` header to every request, so a plain `curl` or `wget`
    would otherwise send the operator's token; the child keeps `HOME` and
    `XDG_CONFIG_HOME`, and its curl and wget still read none of them;
  - `ZDOTDIR`: an empty directory. zsh reads `.zshenv` from `ZDOTDIR`,
    else `HOME`, on every start, `zsh -c` and a zsh shebang included, and
    `.zprofile`, `.zshrc` and `.zlogin` from there for a login or
    interactive shell; an `export GH_TOKEN=...` in any of them would
    replace the placeholder for everything the shell runs. A launch's zsh
    reads none of the operator's (`/etc/zshenv` and the other system files
    are the administrator's);
  - `SHELL`: a shell that runs a command without a startup file of the
    operator's (`child_env.child_shell`): the operator's own when it is an
    absolute, executable `sh`, `bash`, `zsh`, `dash`, `ksh` or `mksh`, else
    the `bash` on `PATH`, else `/bin/sh`. The agent CLIs start their shell
    tool from it: OpenCode 2.0.26 as `$SHELL -c` (a fish or nu `SHELL` it
    replaces with `bash`); Claude Code 2.1.296 starts the bash or zsh its
    adapter names in `CLAUDE_CODE_SHELL` (below). A fish or csh-family
    shell reads its configuration on
    every start, and no variable redirects it, so a launch never gets one
    through `SHELL`.
- **A preflight before every launch** (`child_env.preflight`): one
  `git config --list --show-scope --includes` in the launch's directory,
  through the hardened local request, a read of `~/.netrc` (`_netrc`), and
  a probe of each shell a launch may start as a login shell
  (`shell_startup_problems`, below).
  It refuses a credential helper in repository or worktree scope (the
  shared common dir), a remote URL or `insteadOf` base with an embedded
  credential, and an `http.extraHeader` (a URL-specific one beats the
  generic reset) in system, repository or worktree scope, and a `.netrc`
  entry for GitHub or a `default` entry (git's HTTPS transport reads it
  through `HOME`, which every child keeps). The readers disagree on netrc
  syntax (curl 7.84 and later and wget quote with `"` and match keywords in
  any case, Python's `netrc` module, behind `requests`, drops a `\` before
  any character, and where a `macdef` body or a comment ends differs), so
  every word is compared, split at whitespace and `"`, without `\` and
  without regard to case, wherever it stands: a GitHub host or `default` in
  a password, a comment or a macro body refuses the file too. For an agent
  the refusal is a
  `ConfigurationError` raised before anything is charged, saved or launched,
  so state is untouched; for a candidate validation command it is checked
  again once and then blocks the run with no record and nothing pushed or
  published (`ValidationInconclusiveError`), and for a pre-merge command it
  blocks the merge, as a failing command does. It names the scope and the key's shape, never a value or a
  URL. `autoforge doctor` runs the same check
  and reports the launch environment by name only.
- **The login-shell probe.** `bash -l` reads `/etc/profile` and the first
  of `~/.bash_profile`, `~/.bash_login` and `~/.profile`, and `sh -l`
  `/etc/profile` and `~/.profile`, all through `HOME`, which every child
  keeps and no variable redirects. So the preflight runs each shell a
  launch may start (its `SHELL`, and the `sh`, `bash` and `zsh` on `PATH`)
  once as `<shell> -l -c <probe>`, in the launch's directory and
  environment (its allow-list, the controller-set variables, the operator's
  `HOME`), through the executor with orphan containment. Once the startup
  files have run, the shell `exec`s the controller's own interpreter
  (`python -I -S`), which reads the environment the shell exports, what
  everything it starts inherits: it compares every controller-set variable
  with its value and looks for every withheld name, and prints after a
  marker only the names that changed, so no value leaves that process and
  nothing a startup file prints before the marker counts. Withheld names
  that give nothing while the probed variables stand are tolerated
  (`STARTUP_TOLERATED`: `SSH_AUTH_SOCK` with `IdentityAgent=none`,
  `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` with the placeholder
  token, `GH_HOST`), so a profile that starts an SSH agent refuses nothing.
  A startup file that exports a token, unsets the placeholder, names an
  askpass or an SSH command, or points `BASH_ENV` or `ZDOTDIR` back at the
  operator's files refuses the launch, naming the shell and the variables;
  so does a probe that cannot answer (it exits, times out or prints no
  marker). A launch's zsh reads its startup files
  from the empty `ZDOTDIR`, so the operator's `.zshenv` and the rest are
  no problem and are not refused. It costs one short process per shell per
  launch.
- **Provider integrations.** An MCP server, a plugin or a hook of the
  operator's or the repository's configuration may hold a GitHub token of
  its own, so none runs in a launch; each mechanism was decided from the
  installed CLI's `--help` or build and checked against it
  (`providers.py` records how):
  - Claude Code runs with `--safe-mode`, `--strict-mcp-config` and
    `--setting-sources=user`: no hook, installed plugin or MCP server of
    the user's or the project's settings, `.mcp.json`, `--settings`,
    `--plugin-dir` or `--mcp-config` runs, `CLAUDE_CODE_SAFE_MODE=0`
    notwithstanding, and the project's and the local settings of the
    worktree (`.claude/settings.json`, `.claude/settings.local.json`),
    which the repository controls, are not read. A profile's `extra_args`
    cannot carry a flag that would bring one back (`--mcp-config`,
    `--plugin-dir`, `--plugin-url`, `--settings`, `--setting-sources`,
    `--agents`, `--chrome`, `--ide`): config load refuses it, naming the
    flag and never its value. `doctor` fails a `claude` whose `--help`
    lacks one of the three, since it would refuse every launch's argv.
  - Claude Code's Bash tool runs no function or alias of a shell startup
    file. 2.1.296 runs `<shell> -c -l` once per session, sources
    `~/.zshrc` or `~/.bashrc` through `HOME` (whatever `ZDOTDIR` says) and
    writes the functions, aliases and options it then has to a snapshot,
    which every command sources first; without one (writing it failed) the
    command runs in a login shell, which reads `~/.bash_profile`. A `gh`
    function or alias there that passes a token would reach the agent's
    ordinary `gh`, and no flag or variable turns the snapshot off. What
    2.1.296 runs next in the same shell, before the command and in both
    cases, is the text of the file `CLAUDE_ENV_FILE` names, so every launch
    names a file of its own (mode 0600, removed when the launch ends)
    holding `providers.CLAUDE_SHELL_CLEANER`. It removes every function and
    alias, in bash through POSIX mode (an assignment to `POSIXLY_CORRECT`,
    which no function shadows, puts the special builtin `unset` ahead of
    any function, `builtin` included) and in zsh with `unfunction -m '*'`
    and `unalias -a` / `-as`, then checks that none is left (bash's
    `declare -F` and `alias`, zsh's `$functions` and alias tables) and
    otherwise ends the shell, so the command does not run. The launch also
    sets `CLAUDE_CODE_SHELL` to a bash or a zsh (its `SHELL` when it is
    one, else the one on `PATH`) and `CLAUDE_CODE_SHELL_PREFIX` to empty,
    over whatever the operator's shell passed through `CLAUDE_*`: either
    could name a program of the operator's to run every command in or
    through.
  - The same file gives a login shell back the launch's `PATH` (#347).
    A login shell reads `/etc/profile`, and Debian's sets `PATH` outright,
    so without a snapshot a command would not find what the launch put on
    `PATH`. After the cleaner, the file sets `PATH` to the one Claude Code
    starts with (`providers.claude_login_path`; in a sandbox, as rewritten
    there), in a login shell only. With a snapshot the shell is no login
    shell, and 2.1.310 to 2.1.312 (read from the build) write their own
    `PATH` into the snapshot. The file is completed once the sandbox's
    description is known (`AgentProvider.complete_isolation`), before
    Claude Code starts. `PATH` decides which programs a command finds, not
    which credentials it can reach.
  - 2.1.296 also sets the `env` of the user's settings (`settings.json` in
    `CLAUDE_CONFIG_DIR`, else `~/.claude`) and of its global configuration
    (`.config.json` there when it exists, else `.claude.json` in
    `CLAUDE_CONFIG_DIR`, else in the home) over the environment it starts
    with, `--safe-mode` notwithstanding: a `GH_TOKEN` there would replace
    the placeholder, a `CLAUDE_ENV_FILE` the cleaner. So the launch is
    refused, before anything is charged
    (`ClaudeCodeProvider.integration_preflight`, also run by `doctor`),
    while either sets a variable that would undo the launch's isolation
    (`child_env.undoes_isolation`: a forbidden name, one the controller
    sets, `HOME`, a `BASH_FUNC_*` function, or one of the adapter's own),
    or while either exists and cannot be read. The refusal names the file
    and the variables, never a value; the way out is to remove the entry (a
    variable Claude Code itself needs can be set in the environment
    AutoForge is started from instead).
  - OpenCode 2.0.26 has no such flag, so every launch sets
    `OPENCODE_CONFIG_DIR` to an empty directory of its own (removed when
    the launch ends), `OPENCODE_CONFIG_PROJECT_DISABLE` and
    `OPENCODE_DISABLE_PROJECT_CONFIG` to `1`, and `OPENCODE_CONFIG` and
    `OPENCODE_CONFIG_CONTENT` to empty, over whatever the operator's shell
    passed through `OPENCODE_*`. No `opencode.json(c)` or `plugin(s)/`
    directory is read, neither the operator's global one nor one in or
    above the worktree (OpenCode's project search does not stop at the
    worktree, so it would reach the operator's checkout and home), and no
    explicit or inline document is. An organization connected to OpenCode
    (a `.well-known/opencode` integration) is loaded ahead of all of them
    from OpenCode's database, which also holds the model login a launch
    needs, and no variable or flag keeps it off: without a credential for
    it the cached copy is loaded (`opencode auth logout` removes only the
    credential). So the launch is refused instead, before anything is
    charged (`OpenCodeProvider.integration_preflight`, also run by
    `doctor`), while the database the launch would open (`OPENCODE_DB`,
    else each `opencode*.db` in `$XDG_DATA_HOME/opencode` or
    `~/.local/share/opencode`) lists an organization, while the legacy
    `auth.json` there has a `"wellknown"` entry (imported into every
    database OpenCode creates), or while either cannot be read. The
    refusal names the file and a count, never an origin; the way out is an
    `OPENCODE_DB` of AutoForge's own (or `:memory:` with an API-key
    variable).
  - Pi's `--no-extensions` and the rest of #132's policy are unchanged.

### Credential routes

Each route an agent or a repository command could use, how it is closed,
and the test that shows it (tests in `tests/test_child_env.py` unless
named otherwise), or why it has none.

| Route | Closed by | Test |
|---|---|---|
| gh token in the environment | names refused at load and never inherited; `GH_TOKEN` / `GH_ENTERPRISE_TOKEN` set to the placeholder | `test_config.py` refusals; `test_the_child_environment_carries_no_operator_credential_route`; fake `gh` |
| gh `hosts.yml` (`GH_CONFIG_DIR`, `$XDG_CONFIG_HOME/gh`, `~/.config/gh`) | `GH_CONFIG_DIR` names an empty per-launch directory | fake `gh` reading `hosts.yml`: `test_an_agent_finds_no_credential_route_the_controller_finds_every_one` |
| gh keyring: Secret Service over the session bus (Linux), the keychain (macOS) | bus variables withheld; the placeholder token means `gh` never asks the keyring | fake `gh` with a same-UID "keychain": `test_gh_without_the_placeholder_would_reach_the_keychain`. A real keyring is not tested: CI has none, and a test must never touch the operator's |
| git credential helper in system, global, repository, worktree or included configuration, URL-specific included | global replaced; command-scope `credential.helper=` reset; repository and worktree scope refused by the preflight | `test_an_agent_finds_no_credential_route_the_controller_finds_every_one`, `test_a_helper_in_the_worktree_configuration_is_reset_too`, the preflight tests. System scope is shown through the same command-scope reset: a test cannot write `/etc/gitconfig` |
| git configuration from the environment (`GIT_CONFIG_*`) | refused at load and never inherited; the controller sets its own | `test_config.py` refusals, construction test |
| askpass (`GIT_ASKPASS`, `core.askPass`, `SSH_ASKPASS`) and the terminal prompt | names withheld; `core.askPass=` at command scope; `GIT_TERMINAL_PROMPT=0` | an askpass in the common dir never answers: `test_an_agent_finds_no_credential_route_the_controller_finds_every_one` |
| SSH agent | `SSH_AUTH_SOCK` withheld; `IdentityAgent=none` | fake `ssh` records argv and environment |
| SSH default keys (`~/.ssh/id_*`, found through the password database, not `HOME`) | `core.sshCommand` at command scope: `-F /dev/null`, `IdentityFile=none`; `GIT_SSH`, `GIT_SSH_COMMAND` withheld | fake `ssh` that succeeds only with a default key or the agent |
| a credential in a URL (`remote.*.url`, `pushurl`, `url.*.insteadOf`) | global replaced; system, repository and worktree refused by the preflight | `test_the_preflight_refuses_a_credential_in_the_repository_configuration`; doctor: `test_doctor_refuses_a_token_bearing_origin_and_never_prints_it` |
| `http.extraHeader` | generic reset at command scope; any non-empty one refused by the preflight | preflight tests |
| `~/.netrc` (libcurl, `curl -n`, `requests`) | a word any reader may take for a GitHub machine or `default` refused by the preflight: quoted, escaped or upper-case, in a password, comment or macro body | `test_a_netrc_entry_for_github_refuses_the_launch`, `test_a_netrc_entry_for_github_in_any_readers_syntax_refuses_the_launch`; the real `curl -n` (loopback) and Python's `netrc` take the credential of each disguised entry: `test_the_disguised_netrc_entries_are_ones_a_real_reader_uses`; `test_engine.py::test_a_netrc_entry_for_github_blocks_the_premerge_verification`; `test_doctor.py::test_doctor_refuses_a_netrc_entry_for_github` |
| `NETRC` (curl 8.16 and later, `requests`, Go) | refused at load, never inherited | `test_config.py` refusals; the real `curl` sends the credential of the operator's `NETRC` file for the controller and none from a launch: `test_a_launchs_curl_reads_no_netrc_the_operators_netrc_variable_names` (curl 8.16 and later) |
| curl's configuration (`~/.curlrc`, `$XDG_CONFIG_HOME/curlrc` or `.curlrc`, `$CURL_HOME/.curlrc`), read by every plain `curl` | `CURL_HOME` names an empty `.curlrc` of the launch's, found first; `CURL_HOME` refused at load | the real `curl` against a loopback server, header sent for the controller and not from a launch: `test_a_launchs_plain_curl_sends_no_header_of_the_operators_configuration`; fake `curl`: `test_an_agent_finds_no_credential_route_the_controller_finds_every_one` |
| wget's configuration (`~/.wgetrc`, `~/.config/wget/wget2rc`), read by every plain `wget` | `WGETRC` and `WGET2RC` name an empty file of the launch's; both refused at load | the real `wget` (1.x or wget2) the same way: `test_a_launchs_plain_wget_sends_no_header_of_the_operators_configuration`; fake `wget` |
| `~/.git-credentials`, `$XDG_CONFIG_HOME/git/credentials` | read only by the `store` helper, which is reset | covered by the helper tests; a deliberate read is an absolute-path read (not guaranteed) |
| Claude Code hooks, installed plugins and MCP servers (user, project, `.mcp.json`, plugin) | `--safe-mode` and `--strict-mcp-config` (with `--setting-sources=user`, below); integration flags refused in `extra_args`; `doctor` requires all three flags | `test_providers.py::test_claude_argv_shape`, `test_claude_refuses_extra_args_that_load_an_integration`, `test_config.py::test_a_claude_profile_whose_extra_args_load_an_integration_is_refused`, `test_doctor.py::test_doctor_fails_a_claude_without_the_isolation_flags`. A test cannot run a real Claude Code; the flags were checked against 2.1.296 with marker hooks and MCP servers (`providers.py`) |
| Claude Code's Bash tool: the functions and aliases of `~/.bashrc` or `~/.zshrc` (its shell snapshot) and of the login files of a login shell (without one) | `CLAUDE_ENV_FILE` names the launch's cleaner, run before every command, which removes every function and alias or refuses to run the command; `CLAUDE_CODE_SHELL` a bash or zsh; `CLAUDE_CODE_SHELL_PREFIX` empty | a fake `claude` that writes its snapshot and runs a command as 2.1.296 does, with the real bash and zsh, behind the real adapter and executor; the same launch without the adapter's isolation hands `gh` the wrapper's token: `test_providers.py::test_claudes_bash_tool_runs_no_function_or_alias_of_a_startup_file` (function and alias, snapshot and login shell), `test_claudes_bash_tool_cleaner_works_on_the_one_shot_path`, `test_a_startup_file_that_shadows_the_cleaner_runs_no_command`, `test_claudes_bash_tool_runs_in_no_program_of_the_operators`, `test_the_claude_shell_environment_file_lives_as_long_as_the_launch`. A test cannot run a real Claude Code; the real 2.1.296 was seen, against a loopback API, handing a fake `gh` the token of a `gh` function or alias in `~/.bashrc`, `~/.zshrc` and, without a snapshot, `~/.bash_profile`, and none under the adapter (`providers.py`) |
| Claude Code settings `env`, set over the launch's environment: the user's settings and the global configuration, and the project's and the local settings of the worktree | `--setting-sources=user`; the launch refused while the user's settings or the global configuration set a variable that would undo its isolation, or cannot be read | `test_providers.py::test_the_repositorys_claude_settings_cannot_undo_the_cleaner`, `test_claude_configuration_that_sets_a_variable_over_the_launch_refuses_it`, `test_claude_configuration_without_such_a_variable_launches`, `test_claude_configuration_that_cannot_be_checked_refuses_the_launch`, `test_the_claude_configuration_files_are_the_launchs`; `test_child_env.py::test_a_variable_set_over_a_launch_that_would_undo_its_isolation`; `test_doctor.py::test_doctor_fails_a_claude_whose_settings_set_a_variable_over_the_launch`. The real 2.1.296 was seen taking `GH_TOKEN` and `CLAUDE_ENV_FILE` from each of these files under `--safe-mode`, and neither from the project's or the local settings with `--setting-sources=user` |
| OpenCode MCP servers and plugins (global, project and above, `OPENCODE_CONFIG`, `OPENCODE_CONFIG_CONTENT`) | `opencode_isolation_env`: an empty global configuration directory, no project configuration, no explicit or inline document | a fake `opencode` that resolves its configuration as 2.0.26 does, behind the real executor: `test_providers.py::test_an_opencode_launch_loads_no_mcp_server_or_plugin_of_any_configuration`. A test cannot run a real OpenCode; the variables were checked against 2.0.26's resolved configuration (`providers.py`) |
| OpenCode organization configuration (`.well-known/opencode`: fetched, or cached in OpenCode's database, ahead of every file) | the launch refused while the database it would open lists an organization, `auth.json` has a `"wellknown"` entry, or either cannot be read | the fake `opencode` loads the cached organization configuration from a 2.0.26-shaped database: `test_providers.py::test_an_opencode_launch_is_refused_while_an_organization_is_connected` (cached, not yet cached, legacy `auth.json`, `OPENCODE_DB`), `test_an_opencode_launch_with_no_organization_proceeds`, `test_opencode_data_that_cannot_be_read_refuses_the_launch`; before anything is charged: `test_engine.py::test_an_integration_the_provider_cannot_keep_off_refuses_the_launch_and_leaves_state`; `test_doctor.py::test_doctor_fails_an_opencode_connected_to_an_organization`. The real 2.0.26 was seen starting a cached organization's MCP server under `opencode_isolation_env` and none with `OPENCODE_DB=:memory:` or a database of its own (`providers.py`) |
| Pi extensions, skills, prompt templates, themes, context files | #132's flags (`--no-extensions` and the rest) | `test_pi_rpc.py`, unchanged |
| a provider's permission mode or approval | irrelevant by the authority rule: it grants no credential | — |
| zsh startup files (`.zshenv` on every start, `.zprofile`, `.zshrc`, `.zlogin`), found through `ZDOTDIR` or `HOME` | `ZDOTDIR` names an empty directory of the launch's; `ZDOTDIR` refused at load | the real zsh, `-c`, `-l -c`, `-i -c` and a zsh shebang, with the operator's files exporting a token: `test_a_launchs_zsh_reads_no_startup_file_of_the_operators` (the same launch without `ZDOTDIR` merges); R3-F1's report through the real OpenCode adapter and executor, a fake `opencode` choosing its shell as 2.0.26 does: `test_an_opencode_shell_tool_merges_nothing_through_the_operators_zshenv`; `test_zsh_startup_files_in_the_home_are_no_problem` |
| `BASH_ENV` (every non-interactive bash) and `ENV` (an interactive sh) | refused at load, never inherited | the real bash and sh: `test_a_launchs_bash_and_sh_run_no_startup_file_of_the_operators_for_a_command` (with the operator's `BASH_ENV`, `bash -c` merges) |
| the shell an agent CLI starts (`SHELL`): fish and the csh family read their configuration on every start | `SHELL` set to the operator's POSIX shell, else `bash` or `/bin/sh` | `test_a_launchs_shell_is_one_that_reads_no_startup_file`; construction test (a fish `SHELL` becomes `bash`) |
| login-shell startup files (`/etc/profile`, `~/.bash_profile`, `~/.bash_login`, `~/.profile`, read through `HOME`) | the login-shell probe refuses a launch whose shells' startup files change a probed variable | the real bash and sh: `test_a_login_startup_file_that_restores_a_route_is_named_never_its_value`, `test_what_is_withheld_as_defence_in_depth_alone_is_no_startup_problem`, `test_a_startup_file_that_ends_the_shell_could_not_be_checked`, `test_a_probe_without_an_answer_could_not_be_checked`; `test_engine.py::test_a_login_shell_startup_file_that_exports_a_credential_refuses_the_launch`, `test_a_shell_startup_file_that_exports_a_credential_blocks_the_premerge_verification`; `test_validation_evidence.py::test_a_startup_file_planted_by_the_producer_refuses_its_validation_and_blocks`, `test_engine.py::test_a_startup_file_planted_before_candidate_validation_blocks_the_producer`; `test_doctor.py::test_doctor_refuses_a_login_shell_startup_file_that_exports_a_credential` |
| validation and pre-merge commands | the same variables and preflight, the login-shell probe included | `test_validation_evidence.py::test_a_helper_planted_by_the_producer_never_reaches_its_validation_command`, `test_engine.py::test_verification_commands_get_the_launch_isolation` |

End to end, `tests/test_integration.py` runs the whole REMOTE lifecycle with
an operator shell holding every route above and checks each launch while it
runs, for the scripted providers and for an all-Pi run (the RPC child and
its auth preflight alike). A write an agent makes anyway, through a
credential that slipped through, is not journaled and blocks the run
(`test_engine.py::test_a_write_through_a_leaked_credential_is_unjournaled_and_blocks`).

### The agent read path

An agent with no credential reads GitHub through **controller-supplied read
context** only (ADR 0004 D10). Before each REMOTE launch the engine reads,
through the typed client, what that phase's prompt names: the issue and its
comments (`ANALYZE_EXECUTE`, `REVIEW`, `FIX`, `REPLAN_REEXECUTE`); the PR,
its comments and the check summary of its head commit (`REVIEW`; a
summary GitHub refused the controller says so, #291); the
previous round's review comment (`REVIEW`) or the verified one (`FIX`); the
follow-up issues handed over (`REVIEW`, `FIX`); the EPIC and the state and
title of each issue it references (`UPDATE_EPIC`); the source PR with its
comments, the issue and the EPIC (`REPLAN_REEXECUTE`). It writes them,
through the state directory's capability, into
`<state dir>/read-context/<run id>/<phase>-<attempt>-<token>/`, one
Markdown file per item and a `README.md` index, and the prompt names the
directory by absolute path.

- **Bounded and redacted** (`src/autoforge/read_context.py`): a body, a
  comment, the number of comments (the last 200), each file and the whole
  directory have bounds; a cut is an explicit `[AutoForge omitted ...]`
  line with the counts. Every text goes through the run-log redactor and
  every piece of project text is quoted in a fence nothing inside it can
  close.
- **Never authority.** The controller never reads the files back; every
  decision is still taken on its own reads. An item that cannot be read
  becomes a file that says so (naming the error type), and the launch
  proceeds.
- **The diff** is agent-local `git diff` over the revisions the controller
  fetched. A dry run writes nothing and the plan shows a placeholder.

### Threat model

**Actors.** Trusted: the operator and the controller. Untrusted: the agent
(its output, and anything a prompt injection in project data makes it do),
repository-defined validation and pre-merge commands (the PR's code), and
humans acting concurrently on GitHub. GitHub is the source of truth.

**Guaranteed** (Wave 1):

1. **No delegated authority.** Agents and repository-defined commands
   launched by AutoForge receive no GitHub write authority through anything
   AutoForge controls: the environment; `gh` or git configuration (global,
   system, repository or worktree); credential helpers and askpass
   programs; the SSH agent; provider integrations that AutoForge
   configures.
2. **No steering of the controller.** Repository state an agent can write
   (configuration, hooks, `origin`, include directives, replacement refs,
   grafts, the commit-graph) cannot steer, or run code in, the controller's
   own git network operations and `gh` calls (D7.3, D7.5; no git a `gh`
   starts finds a repository, #283).
3. **Every write is accounted for.** Every Wave 1 externally visible write
   is a typed controller effect with a durable record; anything else with a
   Wave 1 identity is detected and blocks, and is never adopted.
4. **Agent text is bounded data.** It is published only inside controller
   framing and cannot forge a marker, close or link an issue, mention a
   user or team, or carry a credential-shaped string.

**Not guaranteed** under the default `sandbox.mode: disabled`. ADR 0005's
outer sandbox owns the first four; under `sandbox.mode: required` it closes
the first three for every enforced launch, and a sandboxed agent's objects
reach the shared store only through the strict import, with the residuals
of the [Outer sandbox threat model](#outer-sandbox-threat-model-adr-0005):

- **Absolute-path reads.** A same-UID process that deliberately reads a
  credential file, keychain or socket by absolute path
  (`~/.config/gh/hosts.yml`, `~/.git-credentials`, `~/.ssh/`, a keyring over
  the session bus), or runs `ssh` or a credential helper of its own choosing.
- **Absolute-path writes.** A same-UID process that deliberately modifies
  the operator's own `gh` or git configuration, which the controller's own
  `gh` reads. An agent's ordinary `gh` and `git config --global` writes
  land in the controller-supplied locations instead.
- **Shared local repository state.** An agent can still create, move or
  delete shared refs and edit the configuration and hooks of the common
  dir; the preflight refuses the credential carriers among them before the
  next launch, and D7.3 and D7.5 bound what the rest can do to the
  controller.
- **Network egress.** An agent can send whatever it can read.
- **Exposure outside the boundary.** A credential the operator exposes
  elsewhere: a token in a worktree file, in a service the agent can reach,
  or in a variable the operator forwards under a name that is not a
  recognised route.
- **The committed tree** is not filtered; `REVIEW` and the merge gate own it.
- **Integrity of the shared object store** (D7.5, #171).

**Residuals of the provider integrations.** Claude Code still applies
managed settings an administrator installed (`--safe-mode` and
`--setting-sources` leave policy settings in force, their `env` included);
the operator cannot change them either, and they are not detected. OpenCode's organization configuration is not a residual: a
launch that would load it is refused (above).

**Residuals of shell startup.** `HOME` is passed, so a shell that reads its
configuration from `HOME` still does when something starts it that way:
an interactive `bash` reads `~/.bashrc`, and a `fish` or csh-family shell
started by name reads its configuration; neither is the `SHELL` of a
launch, and an agent that starts one deliberately could as well read the
file. Claude Code sourcing `~/.zshrc` or `~/.bashrc` itself for its Bash
tool is not one: no function or alias of them, or of a login shell's
files, is left when a command runs (above). A startup file that changes a
variable only under a condition the probe does not meet (a terminal, a
time of day) is not detected. Under `sandbox.mode: required` the home a
launch sees is a private, empty one (ADR 0005 D8.1), so none of the
operator's startup files exists inside.

**What the agent loses.** The operator's global git configuration beyond
the identity is not read by an agent: no commit signing (`commit.gpgSign`,
`user.signingKey`, an SSH-agent signer), no global Git LFS filter, no
aliases or `core.excludesFile`. Tools that need `XDG_RUNTIME_DIR` (a session
bus, a Wayland socket) do not get it, and curl and wget get none of their
configured defaults (a proxy among them; the proxy variables still pass).
Claude Code runs without the user's or the project's skills, custom agents
and commands and CLAUDE.md auto-discovery, and without the repository's
settings (`.claude/settings.json` and `settings.local.json`: their
permission rules, `env` and model); its Bash tool runs commands without the
operator's shell functions and aliases and without Claude Code's own (its
`rg` when ripgrep is not installed, a `pkill` that spares Claude Code, and
`find` and `grep` where it substitutes them; its Grep tool is unchanged).
OpenCode runs without the settings in its configuration files (custom
providers, permission rules, agents, instructions) and without loading
`AGENTS.md` itself. Every
phase prompt tells the agent to read the repository's `AGENTS.md` and
`CLAUDE.md`, and the model credentials (Claude Code's login and API-key
variables, OpenCode's `opencode auth login` and API-key variables) are
untouched.

### Outer sandbox threat model (ADR 0005)

ADR 0005 D2.1, as #168 implements it. The actors are those above;
repository-defined validation and pre-merge commands are the change's own
code and are untrusted like the agent.

**Guaranteed in every mode** (D6.3–D6.5, `src/autoforge/agent_worktree.py`).
The redirect these close needs no sandbox, only the `.git` file every agent
can write, so they hold under `sandbox.mode: disabled` too:

- **The candidate is addressed through the common dir.** The controller
  reads an issue worktree's `HEAD` as `worktrees/<id>/HEAD` with
  `git --git-dir=<git common dir>`, after `lstat` found a regular file
  there and real directories above it up to the common dir (git would
  follow a link to a file the agent named outside the store), and never
  through the worktree's `.git` file, the admin dir's
  `commondir` or `gitdir`, or `git -C <worktree>`. A `.git` rewritten to
  name a decoy repository cannot move the candidate: the decoy's commit,
  reported as `head_sha`, is refused as not the worktree's `HEAD`
  (`test_engine_sandbox.py::test_the_candidate_is_read_through_the_registry_not_the_worktrees_git_file`).
  An attached `HEAD` is told by `symbolic-ref --no-recurse`: the
  reference it names, which may be a link the agent planted in its admin
  dir to any host file, is never opened (#223).
- **The worktree is bound through the registry.** It is used only while
  exactly one registry entry's `gitdir` names `<worktree>/.git` and that
  `.git` is a regular file naming the entry; otherwise the step refuses,
  naming `git worktree repair`. Rewriting the pointers can make the check
  refuse, never make it name another worktree. Whether it is registered at
  all, and which other worktrees a sandbox denies, is read from the
  registry's `gitdir` files without git, never by `git worktree list`,
  which resolves every listed worktree's `HEAD` through a planted link
  (#223;
  `test_engine_sandbox.py::test_a_planted_link_to_a_fifo_stalls_no_controller_read`).
  The registry directory is opened without following a link: one an agent
  replaced with a link is refused, never listed, until the operator puts
  it back
  (`test_engine_sandbox.py::test_a_registry_replaced_by_a_link_is_refused_until_the_operator_repairs_it`).
- **The agent's objects are imported strictly.** Before every REMOTE
  launch and after every one that exited cleanly, the objects the
  candidate reaches that the shared store lacks are copied out of the
  issue's private object directory: the `rev-parse` that reads the commit
  `HEAD` names, `rev-list --objects` and `pack-objects` read it through
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`, for those three processes only, as
  one double-quoted entry (a `:` in its path
  does not split it), and `index-pack --stdin --fix-thin --strict`
  writes each object under the id it computes from its bytes. The commit
  is the object id `HEAD` holds, read by the controller after the same
  `lstat` as the candidate read: a `HEAD` that is a link is refused before
  any git process runs, so nothing is packed or imported and the
  candidate is refused with it, and an attached `HEAD` imports nothing
  (`test_sandbox_routing.py::test_a_head_link_written_inside_is_never_followed_by_the_import`).
  The directory is walked first without following a link; a directory that is
  not a real one reached from the common dir through real directories (a
  link, another file type, one that cannot be checked), an alternates file,
  or an entry that is neither a directory, a regular file nor an overlay
  whiteout, blocks the run, naming it, with nothing read. Under `disabled`
  agents write the shared store directly, so ADR 0004 D7.5's re-hash of
  every commit a decision reads stays required in both modes.

**Guaranteed for an enforced launch** (`sandbox.mode: required`: every
agent process, candidate validation command and pre-merge command runs
inside the sandbox or is refused):

1. **G1. Nothing of the controller is reachable.** The state directory,
   the run logs, the lock, the controller's checkout and every other
   registered worktree are absent; the read context is read-only.
2. **G2. No credential the boundary hides is reachable by path or by local
   IPC.** The operator's home is a private, empty one, so the gh and git
   credential files, `~/.ssh` and file keyrings are absent, and so are the
   SSH agent socket, the session bus and every path-named socket under
   `/run` and `$XDG_RUNTIME_DIR`; no host abstract socket can be
   connected, and neither the operator's SysV IPC objects nor the session
   keyring are reachable. Symlinks and `..` resolve inside the sandbox's
   own mount tree, and a new hard link to a file outside fails.
3. **G3. No host process is visible,** and none is signallable but the
   launch's own outer `bwrap`.
4. **G4. Writes land only in the launch's writable projections and private
   mounts:** the issue worktree, its admin dir, the private object
   directory and the cache; the exported tree.
5. **G5. Shared repository state is read-only.** No shared ref,
   configuration, hook, alternates file or shared object can be written; a
   agent's new objects reach the shared store only through the
   strict import.
6. **G6. Nothing outlives the invocation,** inside the namespace included.

**Not protected by an enforced launch:**

- the model credential the provider is given, readable by every tool in
  the same sandbox (D7.4,
  [Secrets and logging](secrets-and-logging.md#the-outer-sandbox-and-credentials-adr-0005-d74));
- the repository, and the operator's unpushed work in the shared git
  directory: unpushed and unreachable objects, the main worktree's
  `index`, `refs/stash`, the reflogs, in-progress operation state (D6.7);
- the network an agent shares with the host: loopback TCP services, the
  local DNS stub and general egress. A loopback service that acts on
  request (a debugger or container-daemon port, a development server with
  an execute endpoint) is an escape, not only an exposure.
  `sandbox.validation_network` and `premerge_network: isolated` take it
  away from repository commands, never from agents (D9.2);
- pre-existing hard links in a writable workspace (D4.4);
- credential-shaped values in repository configuration that #165's
  preflight does not refuse, since the common dir is read-only as a whole;
- a writable provider credential directory (`dir-rw`): a persistent write
  channel shared by every launch that projects it, never the provider's
  own configuration root (D7.2).

**Out of scope:** kernel or `bwrap` exploits, a malicious operator, network
egress, resource exhaustion beyond ADR 0002 and the tmpfs bounds, side
channels, credentials the operator deliberately projects, the operator's
own unsandboxed tools, and every launch under `disabled` (#169 added the
capability gate before anything is saved, and `doctor`'s capability,
hard-link, sandbox-directory and loopback-listener reports:
[Usage: outer sandbox](../usage.md#outer-sandbox)). `preferred` means
`required` (ADR 0009 D7): no launch falls back to logical isolation.

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
`GitHubClient`, with no prompt and no agent invocation. It never passes
`--delete-branch`, whatever `merge.delete_branch` says: that flag deletes
the local branch too (below).

`READY_FOR_MERGE` is entered for an open PR only in the write that issues
the round's review certificate, or by an `unblock` while that certificate
applies (ADR 0006 D12.3, #174; [workflow.md](workflow.md#candidate-validation-gates)).
The certificate is not a merge permission (D1.2): it records what the
controller observed of one clean review of one exact validated candidate,
it is not a GitHub approval or a CI result, and it is never shown to an
agent or published (D1.3). Opening the gate still takes both halves above,
and the gate re-reads every fact it decides on from GitHub. The certificate
is one more prerequisite of each gate pass (D13, #175; below), never a
reason to merge on its own, and it removes no other check. A merged PR not
yet counted is reconciled without one (D12.4): counting a merge already
made is not a merge decision.

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
  draft, no change to any `safety.protected_merge_paths` entry, a status
  rollup that holds at least one check (an empty one is `BLOCKED` under
  every configuration, #289), every check in it passed (all checks, not
  only required ones) and at least one of them concluded `SUCCESS` (a
  rollup of only `NEUTRAL` or `SKIPPED` entries is `BLOCKED` under every
  configuration, #289),
  `mergeable = MERGEABLE`, `mergeStateStatus` in `CLEAN`/`HAS_HOOKS`, no
  auto-merge armed, and no merge queue on the base branch (`gh pr merge`
  would otherwise arm auto-merge or enqueue instead of merging, leaving an
  asynchronous merge the controller does not own). Every required check must
  also pass the definition comparison, and every
  `merge.verification_commands` command must pass (both below).
- Conclusive negatives (closed PR, conflict, an empty status rollup, a
  rollup in which nothing succeeded, a failing check, branch protection, queue, a recorded review comment GitHub
  no longer backs, a failing local command) -> `BLOCKED`.
- Inconclusive data (checks still running, the base branch's own run still
  running, `mergeable = UNKNOWN`, an unfetchable reviewed commit, or the
  PR / changed-file / merge-queue read itself failing transiently: timeout,
  connection error, 5xx, rate limit) raises and keeps the phase so
  `resume --allow-merge` (or `step --allow-merge`) re-checks, one attempt per
  invocation, at most `merge.max_verification_attempts` times (default 5),
  then `BLOCKED`.
- A read that fails conclusively (bad credentials, missing permissions, a
  PR that no longer resolves) is `BLOCKED` immediately: re-running would not
  change it. So is a checks read GitHub refuses this credential (#291): the
  reason names the credential and its remedy, and the refusal is never
  taken as an empty rollup.
- HEAD, base or merge-base drift -> `REVIEW`; a PR found already merged in
  `READY_FOR_MERGE` -> `MERGE`, to reconcile and count it once.
- With the PR, HEAD, base and merge base confirmed, the round's review
  certificate must apply to them and to the validation set and policy in
  force (ADR 0006 D13.2): its digest verifies, it names `current_pr_url`
  and the PR GitHub returned, the HEAD, base ref and merge base just read,
  the review comment `last_review_comment_url` names, and PASS records that
  are still in state; and the configured `validation.commands` and policy P
  (`sandbox.mode` and its template, the environment allow-list names,
  `execution.command_timeout_seconds`; [Configuration](../configuration.md#candidate-validation))
  digest to the values it states. This runs before the #94 re-read
  of the review comment, before the readiness reads and before any
  `merge.verification_commands` command, so a PR without an applicable
  certificate never runs its code locally. A refusal is conclusive
  `BLOCKED` in one write, nothing merged or counted, and is never coerced:
  - none held (a state from before certificates, or one that reached the
    gate without the clean round that issues one): "no applicable
    ReviewCertificate for HEAD <sha>; unblock to run a new validated REVIEW
    round", and `unblock` decides `REVIEW`;
  - the set or the policy changed since issuance: the reason names the
    digest that changed. Restore the configuration and `unblock` (it decides
    `READY_FOR_MERGE`, and the next pass re-verifies the PR on GitHub), or
    `unblock` under the new configuration (it decides `REVIEW`: a fresh
    round validated under it). No round is relaunched by itself;
  - any other mismatch names the requirement that failed, and `unblock`
    decides `REVIEW`.

  The check consults state, the configuration and the facts this pass just
  read; it reads nothing more from GitHub and writes nothing on success.
  `MERGE` repeats it: a saved `READY_FOR_MERGE -> MERGE` write followed by a
  configuration edit and a restart is refused the same way.

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
`false`, or leaving `required_checks` empty, skips the comparison; an empty
status rollup, or one in which nothing succeeded, is still refused (#289).

### Controller-run verification commands

`merge.verification_commands` (argv lists, empty by default) run on the
operator's machine after every GitHub-side fact above has passed, in a fresh
temporary export of the reviewed HEAD (`autoforge-premerge-*` in the system
temporary directory, mode `0700`), so no worktree or branch is created, the
operator's checkout is untouched and there is no `.git` for a command to
reach; the export is deleted afterwards.

The export is byte-exact (`src/autoforge/materialize.py`, ADR 0006 D6, #171).
The commit and every blob it names must be in the shared object store. Its
tree is listed with `git ls-tree -r -t -z --full-tree` and every blob is
streamed with `git cat-file --batch` and written as its committed bytes:
regular files `0644`, executables `0755`, symlinks as their link text (never
followed), gitlinks as empty directories, every entry created exclusively
below the directory made for its parent. No index, `checkout-index`,
attribute, eol conversion, `ident`, filter driver, `core.autocrlf`,
`info/attributes` or `core.attributesFile` takes part, whether the candidate
or the operator configured it, so no host filter program runs on candidate
content. An entry named `.`, `..` or `.git` (any case), or a name holding
`/`, is refused before anything is written. Before any command runs, the
controller's own walk re-hashes the directory without following a link:
blob ids from the bytes and modes on disk, tree ids bottom up in the
repository's object format, each gitlink an empty directory standing for the
commit the tree records, and the root must be the commit's tree with no
entry extra or missing. A mismatch (a case-folding filesystem, a full disk,
a legacy `100664` mode, which `ls-tree` reports as `100644`) is a
materialization failure, inconclusive like any other unexportable commit.

The commands therefore see the committed bytes:

- checkout-time eol conversion, `ident` expansion and filter drivers are
  not applied;
- LFS pointers stay pointers;
- git metadata and submodules are absent.

A command that needs git metadata (`git describe`, `setuptools-scm` and
other version stamping), populated submodules, LFS content or converted
line endings fails in the export, so keep those out of the list or make
them tolerate a plain tree.

A commit that is not local yet is fetched from `origin` as
`refs/pull/<n>/head`, objects only, without creating a local ref. A non-zero
exit or a timeout
(`execution.command_timeout_seconds`, wall clock) is `BLOCKED` with the redacted output
tail; an unfetchable or unexportable commit is inconclusive and re-checked by
`resume --allow-merge`. A pass is persisted with the HEAD and the command
list, so a resume at the same HEAD does not rerun it and a changed command
list or a new HEAD does; it is cleared when the run moves to the next issue.
Each run is logged like an agent invocation. Dry-run runs nothing and lists
the commands in the plan; `autoforge doctor` reports what is configured
without running it. The commands run under the same allow-listed
environment as an agent (`execution.env_allowlist`), not the operator's
whole shell, with the same
controller-set variables and preflight ("Agents hold no GitHub credential"
above): a preflight refusal blocks the merge like a failing command.
Under `sandbox.mode: required` each command runs in the outer sandbox with
the exported tree as the only part of the repository it sees, no
persistent cache, and the host network unless `sandbox.premerge_network:
isolated` (ADR 0005 §2.5, D9.2); a command the sandbox refuses is not run
and blocks the merge like a failing one.

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

### Deleting the merged head branch

With `merge.delete_branch: true` (default `false`) the controller deletes
the merged PR's head branch on GitHub, and only there (#283). It is part
of the merge's own mechanism (ADR 0004 D5.4), not an effect kind (no kind
deletes a ref, D6.3):

- **When.** Only once the merge is verified and counted as above; a PR
  left open, merged at another HEAD or into another base deletes nothing.
- **What.** One `DELETE repos/<owner>/<repo>/git/refs/heads/<branch>`
  (`GitHubClient.delete_branch_ref`) in the run's own repository, of the
  branch GitHub names as the PR's head. It is skipped when the head is in
  another repository (a fork, or one GitHub no longer names), when it is
  not the branch the run bound, when it is the PR's base or the
  repository's default branch as GitHub reports it now (the client refuses
  the default branch again before anything starts), and when the exact
  ref read (`git/ref/heads/<branch>`) finds it gone or at a commit other
  than the merged HEAD: a push after the merge is work the merge did not
  include. A push between that read and the `DELETE` is not seen, as with
  `gh`'s own deletion: the branch is deleted, and the pushed commit is
  left only in the pusher's clone.
- **At most once.** The save that counts the merge is its checkpoint,
  made in `MERGE` before the `DELETE` is sent. A crash before it counted
  nothing, so the `MERGE` re-entry recovers the merge without repeating
  it and deletes the branch then; a re-entry that finds the merge counted
  never sends the `DELETE` again, it only reads the branch back. That
  re-entry runs even when the checkpoint spent the last step of
  `workflow.max_total_steps`: it merges and deletes nothing, and blocking
  it would report the counted merge as one that never happened
  ([workflow.md](workflow.md#loop-bounds)).
- **Read back, never blocking.** The outcome is the ref read again, never
  `gh`'s exit status, and it is a note in the step's message: deleted, not
  deleted and why, or unknown when the read fails. A read or a `DELETE`
  that fails, a `gh` that cannot even be started included, is such a
  note, never the step's error. The merge is verified and counted either
  way, and the run moves on.

No local branch is deleted or checked out. `gh pr merge --delete-branch`
does both (gh 2.103.2, `pkg/cmd/pr/merge/merge.go`, `deleteLocalBranch`):
when the directory it runs in has the head branch, it checks out the base
(or creates it), runs `git pull --ff-only` with `gh` as the credential
helper, removes a linked worktree that has the branch checked out, and
runs `git branch -D`. Even without the flag, every `gh pr merge` asks git
there whether the branch exists (`git rev-parse --verify
refs/heads/<branch>`), which is why every `gh` the controller starts runs
in an empty directory of its own ([Controller-owned effects](#controller-owned-effects)).

### Doctor's branch-rule check

`doctor` verifies that the default branch still requires the CI check. The
pre-merge gate trusts "every check on the PR passed" and refuses a
rollup with no check at all or no success in it (#289), but which check
*had* to exist is a branch rule: with the ruleset gone, a PR whose only
check is some other green one passes the gate. That ruleset lives in
repository settings, outside version control, so `autoforge doctor` reads
the effective rules of the default branch
(`gh api repos/{owner}/{repo}/rules/branches/{branch}`, every page) and
reports which contexts a `required_status_checks` rule names, whether each of
`safety.required_checks` (default `ci`) is among them, whether the ruleset's
enforcement is `active`, and whether it has bypass actors. A repository
still on classic branch protection is checked through that instead
(`enforce_admins` standing in for "no bypass actors"). Any of those
missing, a gap the answer shows, names the settings URL and the rule to
add, and is a `FAIL` while the configuration's half of the merge gate is
open (`safety.allow_merge: true`). With the gate closed it is a `WARN`
(#259): only the merge gate relies on the rule, a run with it closed stops
at `READY_FOR_MERGE` for a human to merge, and a fresh repository without
CI yet would otherwise fail its first `doctor` on a row its first run does
not need. The warning says so and asks for the rule before the gate is
opened; opening it turns the row back into a `FAIL`, and the gate's own
checks are the same either way. GitHub reporting no default branch at all
is a `FAIL` whatever the gate. Rules it cannot verify are not shown to
require anything (#289), and how that is reported depends on the merge
gate:

- GitHub's plan offers no rulesets or branch protection on the repository
  (GitHub Free's private repositories: `HTTP 403 "Upgrade to GitHub Pro or
  make this repository public to enable this feature."`, to every caller,
  the owner included). The client types that answer
  (`GitHubPlanLimitedError`) and never classifies it as access denied, and
  the row names the plan, says no token permission changes it, and names
  the ways out (a public repository, or GitHub Pro, Team or Enterprise).
  Each branch-rule read (effective rules, a ruleset, classic protection)
  is classified this way; the rulesets listing is a hint only.
- The token may not read the rules (no credentials, a non-admin token
  reading classic protection, not found).
- GitHub could not be read at all (a transient failure): nothing was
  verified; running doctor again answers it.
- `gh` itself could not be started for the read (#259), even when the
  earlier `gh available` row passed: nothing was read, so with the gate
  open this is the same `FAIL`, never a `SKIP` that lets doctor pass.
- GitHub returns a ruleset's `bypass_actors` only to a token with write
  access to it, and this answer withholds them *and* omits
  `current_user_can_bypass`: whether the controller's own login may skip
  the rule is unverified.

Each is a `FAIL` while the configuration's half of the merge gate is open
(`safety.allow_merge: true`): an unattended merge would rest on a
requirement nobody verified. The text names the way out for its cause
(for the plan: a public repository or a plan that offers rules, never a
token permission), or closing the gate. With the gate closed it is a `SKIP`
that says why. A hidden bypass list is a `SKIP` either way when GitHub does
say `current_user_can_bypass: never`: the required checks were read and the
controller's login cannot bypass them, though "no bypass actors" is
unverified (a token that GitHub says may itself bypass the rule is a gap
the answer shows: a `FAIL` with the gate open, a `WARN` with it closed).
The row is never an unverified `OK`.
`doctor --json` carries a skip as `"skipped": true`. The check is read-only
and runs in `doctor` only: the `READY_FOR_MERGE` gate itself does not yet
consult branch rules.

### Doctor's checks-read check

`doctor`'s "pull request check runs readable" row (#291) tells an operator
before a run whether the merge gate's checks read will be refused. It uses
the controller's `gh` command and login and never reads, prints or stores a
token. It lists the most recently updated open pull requests with the
number of check results on each head (one GraphQL query that reads
`contexts.totalCount` only, which GitHub answers even to a token it refuses
the check runs), then makes the checks read (`get_pr_with_checks`) on up
to three of those with at least one result:

- **PASS** (`OK`) only when the read returned at least one check run, the
  result GitHub was seen to refuse (#257). Whether GitHub refuses such a
  token a pull request whose results are commit statuses only was not
  tried, so a read of one proves nothing about check runs: the next
  candidate is read, and if none holds a check run the row is UNKNOWN,
  never PASS. A refusal of any candidate's read is DENIED.
- **DENIED** when GitHub refused the read: the checks refusal, with the
  credential's remedy, or a refusal of the pull request itself. It is a
  `FAIL` while the configuration's half of the merge gate is open
  (`safety.allow_merge: true`; every merge would block) and a `WARN`
  otherwise (`REVIEW` and `FIX` proceed without the checks).
- **UNKNOWN** (`SKIP`) when no recently updated open pull request has a
  check result, when no candidate's results held a check run, and when
  GitHub's answer was a plan limitation, a not-found or a transient
  failure: none of those says what this login may read. It is never a
  PASS.

`doctor` as a whole is read-only apart from creating the state directory
if it is missing and a probe file it creates and removes there; its GitHub
reads (`gh repo view <owner>/<repo>`, `gh api` GETs of the default branch's rules, the
checks row's GraphQL query and `gh pr view`) never write.

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

## EPIC dependency graph

The parallel EPIC scheduler (ADR 0008 D3, D4) decides which children of an
EPIC are admissible from GitHub's native graph alone
(`src/autoforge/epic_graph.py`, #179). Its caller is the scheduler
(`src/autoforge/scheduler.py`, #182), holding L0: a snapshot at most once
per poll interval while admission is open, and the re-read before each
claim. These four reads are all the GitHub the scheduler itself uses: every
write of an issue (its branch, PR and comments) is that issue's worker
engine's, under the same effect records and gates as a standalone run, and
no worker writes another issue's objects. Sequential `UPDATE_EPIC`
selection (`_verify_issue_selectable`) takes no snapshot: it reads the
selected issue's node with `get_issue_node` and holds it to the same
membership rule, and a `null` reads the EPIC's children through the
snapshot's own `read_children` ("After UPDATE_EPIC" above, #287). Its read
context also uses `list_titled_sub_issues`, the `.../sub_issues` listing
with each row's title kept; a title is never an eligibility fact and never
reaches a node, a snapshot or a digest.

- **The reads.** Four typed, read-only `GitHubClient` methods, each a plain
  `gh api` GET with the transient retry of every read:
  `get_repository_id` (`GET /repos/{o}/{r}`), `get_issue_node`
  (`GET .../issues/{n}`), `list_sub_issues` (`.../sub_issues`) and
  `list_blocked_by` (`.../dependencies/blocked_by`). Each listing uses
  `per_page=100` and reads every page (`--paginate --slurp`). A page short
  of 100 rows before the last page is a GitHubError, because rows went
  missing between page reads. Rows decode strictly into `IssueNode` /
  `BlockerNode`: id, number, HTML URL (a `/pull/` URL is refused), the
  repository (`repository_url` must agree with it), state, `state_reason`,
  `parent_issue_url` and the two summaries. GitHub omits `parent_issue_url`
  for an issue without a parent (observed live), so an absent field reads
  as no parent. A sub-issue row without one is still refused, and a
  re-read child without one is stale. No raw dictionary leaves
  `github.py`. Only direct sub-issues are listed. Nested EPICs are never
  traversed, `blocking` is never read, and prose (`Depends on #n`,
  checklists, the roadmap) is never authority.
- **Complete or refused.** `build_epic_snapshot` returns an immutable
  `EpicSnapshot` or raises `SnapshotRefused` with a reason. It never
  returns a partial graph. It refuses when:
  - a read fails (`read-failed`, `transient` when it may succeed later);
  - the EPIC URL's repository no longer has the verified `repository_id`;
  - the EPIC is not open;
  - the distinct child listing differs from `sub_issues_summary.total`;
  - a queried blocker listing differs from `total_blocked_by`;
  - a bound is exceeded: 100 sub-issues per parent (GitHub docs, "Adding
    sub-issues") or 50 blockers per issue (GitHub changelog 2025-08-21,
    "Dependencies on issues");
  - the answer contradicts itself (one id with two sets of facts, two ids
    for one issue, a listed child whose parent is not the EPIC);
  - the open children contain a cycle, a self-edge included. The message
    names one cycle canonically, starting at its lowest number. No agent
    is ever asked to resolve it.

  Entries are de-duplicated by issue `id`. A closed child's blockers are
  not read, and neither are those of a child whose summary reports none.
- **The pinned fields.** Two counts carry the completeness check:
  `sub_issues_summary.total` counts closed children, and
  `issue_dependencies_summary.total_blocked_by` counts closed blockers
  (`blocked_by` counts only open ones). The REST reference settles
  neither. Both were confirmed live on 2026-10-19 against EPIC #176 of this
  repository. Its listing holds all seven children, four of them closed.
  #179 reports no open blocker and two in all, and lists both closed
  blockers. The captured answers are the fixtures under
  `tests/fixtures/github_graph`, and their README records how they were
  captured. If GitHub counted otherwise, a listing holding a closed child
  or blocker would be longer than its count, and the snapshot would be
  refused, never accepted incomplete.
- **Satisfaction is completed-only.** A blocker is satisfied only when it
  is closed with `state_reason` `completed`. Closed as `not_planned`, as a
  duplicate, with no reason or with an unknown reason, it is
  `closed-unsatisfied` and the verdict shows the reason. An open or
  reopened blocker blocks. This is stricter than GitHub's display on
  purpose: the operator removes the native edge when that is intended.
  A blocker in another EPIC or another repository counts by its live state.
  A blocker the token may not read blocks its child (`unreadable`).
  GitHub documents no shape for one: a row naming only an `id` is read as
  unreadable. A blocker left out of the listing makes it shorter than
  `total_blocked_by`, and that refuses the snapshot.
- **Shapes.** A child with sub-issues of its own is
  `unsupported:nested-parent`. A child in another repository is
  `unsupported:foreign-repository`. A closed child is `closed` and never
  ready.
- **The ready set.** `evaluate(snapshot, local_facts)` is pure. Verdict
  precedence is `closed` > `unsupported` > `blocked` > `owned` >
  `held:<reason>` > `ready`. `local_facts` is `epic_graph.LocalFact`, the
  type #181's reconciliation returns. Order is descending count of open
  children that transitively depend on the child, then ascending issue
  number. Both keys come from the snapshot alone, never from API order,
  dictionary or set iteration, or time.
- **Eligibility facts and digests.** Only ids, numbers, states,
  `state_reason`, repository, parent and sub-issue count reach a verdict,
  the order or a digest. Titles, bodies, labels, assignees, milestones
  and timestamps are never read into a node, so a hostile title cannot
  change scheduling. The snapshot digest and the node digests are SHA-256
  over canonical JSON of exactly the facts of ADR 0008 D4.4. #181 records
  them in a claim's `admission`. Diagnostics name issue numbers, states
  and reasons, never a title or a body. They are redacted and bounded.
- **The admission re-read.** `reread_node(client, snapshot, issue)`
  re-reads one child and its complete blocker list and compares them with
  the snapshot's node digest. The result is `unchanged`, or `stale` with
  each difference:
  - the child is not open;
  - its id differs;
  - its parent is no longer the EPIC;
  - it now has sub-issues;
  - a blocker was added, removed or changed state;
  - the blocker listing is incomplete.

  A read that fails raises its GitHubError, so it is never `unchanged`. The
  snapshot is never changed.
- **No GitHub write.** The scheduler writes nothing to GitHub: no label,
  comment, edge or EPIC body. `epic_graph` is typed against a
  `GraphReader` protocol of the four reads. A structural test
  (`tests/test_epic_graph.py`) asserts that the module reaches no other
  client method, no write method, and no process, file or lock module.

---

## Related contracts

- Crash recovery around a side effect that completed before local state was
  persisted, and the transient-versus-conclusive failure classification, are
  in [state-and-recovery.md](state-and-recovery.md).
- The destructive close in `REPLAN_REEXECUTE` has its own transaction
  contract: [replan-transaction.md](replan-transaction.md).
