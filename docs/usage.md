# Running the remote workflow

How to drive an EPIC and its issues through AutoForge's GitHub workflow:
the commands, where a run stops and what to do there, and how to recover.
For the GitHub-free workflow see [Local mode](local-mode.md); for the config
file see [Configuration](configuration.md). Installation and prerequisites
are in the [README](../README.md#prerequisites).

The commands are written `uv run autoforge ...`, the form inside a
development clone of AutoForge. With the command installed
([Installation](../README.md#installation)), drop the `uv run`.

## Commands

```bash
uv run autoforge doctor                 # environment checks (writes nothing but a state-dir probe)
uv run autoforge doctor --json

# preview without touching anything (no subprocess, no gh call, no state file)
uv run autoforge run --epic https://github.com/owner/repo/issues/1 \
               --issue https://github.com/owner/repo/issues/2 --dry-run

# create a run and drive it until READY_FOR_MERGE / BLOCKED / FAILED
uv run autoforge run --epic https://github.com/owner/repo/issues/1 \
               --issue https://github.com/owner/repo/issues/2

uv run autoforge status            # human summary
uv run autoforge status --json     # machine-readable; `replan_journal` says whether the
                                   # raw `replan_transaction` beside it is readable,
                                   # `acceptance_actions` lists the acceptance actions,
                                   # `operator_actions` the operator actions and
                                   # `review_certificate_status` the review certificate
uv run autoforge status --action A-0123456789abcdef   # the exact payload bytes of
                                   # an acceptance action or an operator action (X-…)
                                   # (exit 1 for an unknown id)

uv run autoforge step              # exactly one phase step
uv run autoforge step --dry-run    # preview the next step

uv run autoforge resume            # continue until a stop phase / --max-steps
# --max-steps bounds one invocation; workflow.max_total_steps (config) bounds
# the whole run and is not reset by resume

# step, resume and unblock refuse a configuration whose profile routing or
# agent worktree root differs from the one the run was recorded under, or
# whose sandbox.mode is weaker than the one it is pinned to; to change it
# deliberately (recorded in the run log):
uv run autoforge --config autoforge.yaml resume --accept-config-change

# only with safety.allow_merge: true in config — the controller verifies on
# GitHub and merges itself (agents never merge); re-run to re-check
# inconclusive GitHub data (bounded by merge.max_verification_attempts)
uv run autoforge resume --allow-merge

# leave BLOCKED explicitly: the controller re-inspects GitHub and re-enters
# the one phase that inspection supports, or refuses and stays BLOCKED;
# then `resume`. --dry-run previews the decision without writing anything
uv run autoforge unblock --reason "reran the flaky check; it is green"

# a FIX acceptance action awaits you (safety.acceptance_actions: approve):
# approve exactly that action id, or decline it (FIX runs the fixer again);
# then `resume`. Mutually exclusive; --dry-run works here too
uv run autoforge unblock --approve-action A-0123456789abcdef --reason "checked the text"
uv run autoforge unblock --decline-action A-0123456789abcdef --reason "overstates coverage"

# FIX operator actions await you: you post each comment yourself (the
# controller never does), or bind a comment already there, or withdraw them
# (repeatable); then `resume`
uv run autoforge status --action X-0123456789abcdef | \
  gh issue comment https://github.com/owner/repo/issues/16 --body-file -
uv run autoforge unblock --reason "posted the note on #16"
uv run autoforge unblock --bind-evidence X-0123456789abcdef \
  https://github.com/owner/repo/issues/16#issuecomment-123 --reason "the note is there"
uv run autoforge unblock --decline-action X-0123456789abcdef \
  --decline-action X-fedcba9876543210 --reason "not this issue's job"
```

Pass a config file with the global option, before the subcommand:
`uv run autoforge --config autoforge.yaml run ...`. `run --force` discards an
existing non-terminal run (an unreadable state file is moved aside rather
than deleted; see [Troubleshooting](#troubleshooting-and-recovery)).
`--full-prompt` prints the whole rendered prompt in a dry run.

To run several issues of an EPIC at once, see
[Running an EPIC in parallel](#running-an-epic-in-parallel).

URLs must be HTTPS GitHub issue URLs, and EPIC + issue must be in the same
repository as the current working directory (cross-repo runs are rejected).
`run`, `step` and `resume` must be run inside a git checkout of that
repository, because the repository lock lives in its git directory (dry-run
does not need one).

## What a run does

```text
Issue → ANALYZE_EXECUTE (Claude Code) → controller pushes, opens PR → REVIEW (OpenCode)
       → clean ───────────────────────────────▶ READY_FOR_MERGE
       → findings ────────────────────────────▶ FIX (Claude Code) → REVIEW …
       → excessive / low-finding stagnation ──▶ REPLAN_REEXECUTE (OpenCode)
                                                    → replacement PR → REVIEW round 1
```

Every phase ends with the controller checking the agent's claims against
GitHub before the state advances; an agent claim that cannot be verified
never advances the state machine. In brief:

| Phase | Who | What the controller checks |
|---|---|---|
| `INITIALIZING` | controller | the checkout's `origin` (read locally, named as GitHub names it) is the issue's repo, the issue is in this repo, is not the EPIC, exists and is OPEN |
| `ANALYZE_EXECUTE` | Claude Code (`analyze_execute`), which makes no GitHub write | an open PR already carrying the issue's `ai-implementation` marker is adopted without running the agent (two block); the agent commits on its worktree's detached HEAD, and the controller checks that HEAD itself (the reported SHA, descent from the default branch head and from the branch head, every commit message), pushes it to `autoforge/<n>`, opens the PR with the agent's title and body (or adopts the open unmarked PR on that branch), journalling each write before it is sent, and reads the PR back at exactly the pushed HEAD; a marker PR or a branch head it did not write blocks |
| `REVIEW` | OpenCode (`review_round_1`, `review_round_2_5`, `review_round_6_plus`), which makes no GitHub write | the round is bound to the PR, HEAD, base and merge base read from GitHub, and a comment already carrying that round's marker blocks (the controller never adopts one it did not post); the reviewer's round and HEAD must match the binding and `needs_fix_round == (len(findings) > 0)`; the controller renders the review comment from the result, journals it, posts it and reads it back, so a crash or `resume` never posts a second one; then controller policy picks `FIX`, `READY_FOR_MERGE`, `REPLAN_REEXECUTE` or `BLOCKED` |
| `FIX` | Claude Code (`fix`), which makes no GitHub write | the PR is still at the reviewed revision (else back to `REVIEW` without running the fixer); the open issues already marked as a finding's follow-up are handed to the fixer to reuse, and one the controller did not hand over or create blocks; the fixer commits on its worktree's detached HEAD and resolves every open finding exactly once, and the controller checks that HEAD itself (the reported SHA, descent from the reviewed HEAD, every commit message, a commit for each `fixed` finding); it creates the follow-up issues the fixer asks for, appends the finding markers to the handed-over issues it defers to, then pushes the commit last, journalling each write before it is sent, and reads the follow-ups and the PR head back, so a crash or `resume` never creates or pushes twice; a push it did not make sends the PR back to `REVIEW` |
| `REPLAN_REEXECUTE` | OpenCode (`replan_reexecute`), which makes no GitHub write | one durable transaction; the agent commits on its worktree's detached HEAD at the default branch head the controller recorded, and the controller checks that HEAD itself (the reported SHA, descent from that base, every commit message), pushes it to `autoforge/<n>-replan-<transaction id>`, creates the replacement PR with the agent's title and body followed by the controller's markers, journalling each write before it is sent, and binds the PR only at exactly the pushed HEAD, so a crash or `resume` never pushes or creates twice; a replacement branch or a marker PR it did not write rejects the transaction; the old PR is closed by the controller only while both PRs still match their checkpoints |
| `READY_FOR_MERGE` | nobody | holding state; with the merge gate open, the full pre-merge verification runs before `MERGE` is entered |
| `MERGE` (gated) | controller, never an agent | `gh pr merge --match-head-commit <reviewed HEAD>`, counted only once GitHub reports `MERGED` at that HEAD into the reviewed base |
| `UPDATE_EPIC` | OpenCode (`update_epic`), which makes no GitHub write | the controller (not the agent) posts the progress comment and writes the roadmap section, journals each write before performing it and reads it back, so a crash or `resume` never posts a second comment; a marker comment it did not post blocks; the next issue gets the `INITIALIZING` checks; `null` -> `DONE` |

The complete per-phase verification, including what each phase reads
*before* it launches an agent so a re-entry never duplicates work, is in
[GitHub safety](agent-guides/github-safety.md); the transitions and
review-round routing are in [Workflow](agent-guides/workflow.md); the replan
transaction is in [Replan transaction](agent-guides/replan-transaction.md).

### Loop bounds and replanning

The REVIEW/FIX loop is bounded by the controller, never by prompt wording:
a cap on review rounds per PR (`workflow.max_review_rounds`, default 20),
stagnation detection (`workflow.stagnation_*`), and a cumulative step budget
for the whole run (`workflow.max_total_steps`, default 300, measured on the
persisted step count so `resume` continues it). Failed agent invocations
consume neither a review round nor the history. Every bound ends in
`BLOCKED` with the reason; findings and the PR stay for a human.

Replanning is controller policy, not reviewer advice. After a verified
review with findings, `review.replan` may decide to abandon a
patch-accumulated PR: it preserves compact finding metadata and selected
review comments, then asks the separate `replan_reexecute` high-effort
profile to independently rebuild from the latest verified default branch.
At most two replans per issue are allowed by default
(`review.replan.max_replans_per_issue`); a further eligible trigger blocks
for human intervention. This retains failure knowledge while intentionally
discarding implementation anchoring; it does not guarantee convergence. The
exact triggers are in [Workflow: loop bounds](agent-guides/workflow.md#loop-bounds).

## Where a run stops

`run` and `resume` loop one step at a time until a stop phase:
`READY_FOR_MERGE`, `DONE`, `BLOCKED` or `FAILED`.

### READY_FOR_MERGE: a human merges, unless the gate is open

When the loop reaches `READY_FOR_MERGE` with the merge gate closed (the
default) the CLI prints a banner with the issue, PR, review round and reviewed
HEAD, and states that the merge gate is closed: a human merges the PR.
`resume` without the gate open re-prints the banner and runs nothing. The
banner's `Review certificate:` line says whether the round's certificate
applies, checked against the state and the configuration in force (GitHub
is not read): `applies (round N, ...)`, `does not apply (<reason>)` or
`none held`; in the last two cases the merge gate would refuse to merge.
With the gate open (`resume --allow-merge` / `step --allow-merge`) the
controller continues through its own pre-merge verification, `MERGE` and
`UPDATE_EPIC`, and checks the certificate against the PR GitHub returns as
part of it.

The gate opens only when both `safety.allow_merge: true` is set in config and
`--allow-merge` is passed on the CLI. Pre-merge verification fails closed:
conclusive negatives are `BLOCKED`, inconclusive GitHub data keeps the phase
for another `resume --allow-merge` (at most
`merge.max_verification_attempts` times, then `BLOCKED`), and a PR that
changes `safety.protected_merge_paths` (default `.github/workflows/`) is
never merged unattended. The gate needs the PR's check results: where
GitHub refuses them to the credential AutoForge runs with (a fine-grained
personal access token on a private repository whose pull requests have
check runs, #291), the run is `BLOCKED` at once with a reason naming the
credential and the remedy (`gh auth login`, or a classic token with the
`repo` scope), never merged and never re-checked; change the credential,
then `unblock`. `autoforge doctor`'s "pull request check runs readable" row
finds this before a run. A PR whose status rollup holds no check at all,
or no successful one (only `NEUTRAL` or `SKIPPED` entries), is `BLOCKED`
too, whatever `safety.required_checks` and `safety.verify_check_definition`
say (#289): make CI report a success on the PR's head, then `unblock`, or
merge it yourself. Every gate pass also requires the round's review
certificate to apply to the live PR and to the configuration in force,
before the review comment is re-read or any local command runs; a refusal
is `BLOCKED`, never retried (see
[Candidate validation evidence](#candidate-validation-evidence) for the
`unblock` that follows). The full gate, including the check-definition
comparison and the optional controller-run `merge.verification_commands`,
is in [GitHub safety: merge safety](agent-guides/github-safety.md#merge-safety).

### BLOCKED: fix the cause, then unblock

A run in `BLOCKED` is terminal for `step` and `resume`. `autoforge status`
shows the `block_reason`. After fixing the cause on GitHub (closing a
duplicate PR, rerunning a check, raising a bound in the config),
`autoforge unblock --reason "..."` asks the controller to decide again from
live GitHub: it re-runs the recovery inspection of the phase it would
re-enter and either re-enters it (`ANALYZE_EXECUTE`, `REVIEW`, `FIX`,
`READY_FOR_MERGE` or `UPDATE_EPIC`, through the same transition validation as
any other step) or refuses and leaves the run `BLOCKED`, exit code 1, when
the safe state still cannot be determined (two candidate PRs, a closed PR, a
merge no review decided on, a replan transaction on record, an exhausted
bound). An open PR at its clean-reviewed revision re-enters
`READY_FOR_MERGE` only while the round's review certificate applies; with
none, it re-enters `REVIEW` for a fresh round (refused past
`workflow.max_review_rounds`). The reason is recorded in the state file and the run log, `status`
shows the last one, and no agent runs until `resume`. A controller write
that blocked as a conflict (for example the `UPDATE_EPIC` progress comment
after its two attempts, or a marker comment the controller did not post)
names the effect in the reason: perform the write by hand or remove the
conflicting object, then `unblock`; the controller reconciles the write
against GitHub again before it issues anything. `ANALYZE_EXECUTE` blocks the
same way on anything it did not write: a PR carrying the issue's marker
that appeared while the agent ran (if it is the implementation, `unblock`
binds it), a push to, creation or deletion of `autoforge/<n>` it did not
record (inspect the branch; `unblock` reads it again and the agent continues
from its head), a closed or merged PR on that branch (reopen it to have it
adopted, or finish the issue by hand), an open PR on that branch the entry
did not record, such as one opened while the agent ran (`unblock` reads the
branch again and adopts it, so close it first if it is not the issue's), a
default branch renamed or switched before the plan was saved (`unblock`
starts a fresh entry against the new one), and a push whose branch moved after
the plan was saved (put the branch back, or delete it if it did not exist,
then `unblock`: the plan completes without launching the agent). A default
branch changed after the plan was saved blocks before the planned PR is
created: make the planned branch the default again, then `unblock`.
A run that an earlier version blocked on the controller's own PR, issue or
comment because its text held something like `\u001b` (#327: `gh` prints
that text as `\^[`, so the read-back saw another body) needs only
`unblock`: the read-back now reads the object's exact text from GitHub and
binds it, with nothing written again
([GitHub safety](agent-guides/github-safety.md#ghs-sanitizer-and-the-exact-read-327)).
`REVIEW` blocks on a comment carrying the round's review marker that it did
not post, whether it was there before the reviewer ran (a human's, or one a
reviewer of an earlier AutoForge version posted) or appeared while it ran:
the controller never adopts, edits or duplicates it. Delete the comment or
its marker, then `unblock`, and the round runs (or, when the reviewer's
result was already accepted, its comment is posted from the plan without
running the reviewer again). `FIX` blocks on follow-up issues it did not
write: two open issues carrying one finding's follow-up marker, or an
unreadable follow-up marker on any open issue (close or repair the extra
or unreadable issue, then `unblock`); a follow-up issue that appeared or
disappeared while the fixer ran (if it is the finding's follow-up,
`unblock` starts a fresh entry that hands it to the fixer; otherwise close
it or remove its marker first); an issue a deferral names that is no
longer open or has moved to another repository (reopen it, or `unblock`
to run the fixer afresh); an issue whose body now holds something shaped
like a credential, so the marker cannot be appended to it (remove it, then
`unblock`); after the plan was saved, a follow-up issue (reused, or one
the controller already created or appended to) that is no longer the one
open issue carrying its finding's marker, or an open issue carrying the
marker of a finding the plan resolved without a deferral (reopen the
controller's issue, or close or unmark the one it did not write, then
`unblock`); a PR whose head repository GitHub no longer reports, or whose
head moved to a fork or another branch, after the plan was saved; and a
write that conflicts after the plan was saved (reopen the follow-up issue
the controller created, put the issue body back, or leave the PR branch at
the reviewed HEAD or the planned commit, then `unblock`: the plan
completes without running the fixer again). A FIX acceptance action that
awaits you also blocks the run, and so do FIX operator actions; see the next
two sections. A push to the PR branch the controller did not make, before
or after the fixer ran, is not a block: the PR goes back to `REVIEW` of the
pushed HEAD; so does a PR retargeted to another base, or a base rewritten
under its name, at any point of the phase. A LOCAL
run cannot be unblocked. The decision table is in
[Workflow: leaving BLOCKED](agent-guides/workflow.md#leaving-blocked-the-operators-unblock).

### BLOCKED on an acceptance action

A finding or an acceptance criterion sometimes asks for text to be posted
on the issue itself (a summary of results, say). The fixer cannot post it:
it resolves the finding as `acceptance_action` with the comment's text, and
the controller publishes one comment on the run's issue, with its own header
line above the text and the finding's `ai-acceptance` marker below it. It
publishes nothing else of the `FIX` plan first: the follow-up issues and the
push of the fixer's commit wait for the comment.

With `safety.acceptance_actions: approve` (the default) the step blocks
before anything is sent. `status` shows an `Acceptance action A-…` block:
the target issue, the PR, the round and the finding, who authorizes it, its
state (`awaiting approval`, `approved, not yet published`, `published, not
yet verified`, `verified: <url>` or `verification failed: …`), the payload's
size and SHA-256, the push held back and the agent worktree that alone holds
the unpushed commit (do not reset or remove it), and the next steps. Read the
exact comment with `autoforge status --action A-…`, then either:

- **approve it:** `autoforge unblock --approve-action A-… --reason "..."`,
  then `resume`, which posts the comment, reads it back, and pushes;
- **post it yourself:** post exactly those bytes (marker included) on the
  issue, then `autoforge unblock --reason "..."` and `resume`, which adopt
  your comment and send no second one;
- **decline it:** `autoforge unblock --decline-action A-… --reason "..."`;
  the unsent plan is dropped and `resume` runs the fixer again with your
  reason in its prompt. The same comment for the same finding is refused if
  asked for again. After 16 declines in one review round the controller
  requests no further acceptance comment in that round: the fixer resolves
  the finding another way.

The id names the exact bytes and the target, so an id of another plan or a
changed payload never matches. A plain `unblock` with nothing on the issue is
refused: your reason is not evidence, and a comment without the marker, or
with other bytes, does not fulfil the action (a marked comment with other
bytes is a conflict: delete it, or edit it back to the payload). Approving or
declining is refused, with nothing written, after a push to the PR or a
change of its base; a plain `unblock` then re-enters `FIX`, which sends
nothing, records the action `stale` and goes back to `REVIEW`. A closed issue
holds the comment until it is reopened, and so does the exact payload already
posted on it: nothing else of the fix is published until then. With `auto` the
controller publishes the comment during the `FIX` step without stopping; with
`off` the fixer is told the resolution is unavailable and a result using it is
refused. The comment counts only after the controller read it back from
GitHub, and the next review still judges whether it resolves the finding.

### BLOCKED on operator actions

A finding sometimes asks for text on *another* issue: a coordination note
on an issue the run's issue references, or a status line on the EPIC. The
fixer cannot post it, and neither does the controller: it resolves the
finding as `operator_acceptance` with one to four `related_issue_comment`
actions (four at most per result too), each naming a target issue and the
comment's text, and **you** post each comment. The controller only plans
them, shows you the exact bytes and verifies afterwards what is on GitHub.

The allowed targets are fixed when `FIX` starts: the issues the run's issue
body references (`#N` or a URL of the run's repository; the first 60), plus
the EPIC, minus the run's own issue and PR. An edit of the issue body later
changes nothing for that `FIX` step. A target must be an existing issue of
the run's repository (open or closed), never a pull request. The text is at
most 16000 characters, with no image and no raw HTML, and links only to
`https://github.com/<the run's repository>/...`. A result may not mix
`operator_acceptance` with `acceptance_action`. A LOCAL run and
`safety.acceptance_actions: off` refuse the resolution (the fixer is told it
is unavailable); `approve` and `auto` both offer it, and it always waits for
you.

The step blocks before anything else of the plan is sent: the follow-up
issues and the push of the fixer's commit wait until every operator action
is verified. `status` shows an `Operator actions (FIX, review round N,
finding F …)` block with one `X-…` line per action (`pending (not yet
verified)`, `evidence bound, not yet verified: <url>`, `verified (exact):
<url>`, `verified (operator-bound; …): <url>` or `verification failed: …`),
why each target is allowed, the payload's size and SHA-256, the push held
back, the agent worktree that holds the unpushed commit (do not reset or
remove it), and the next commands. For each action, either:

- **post it:** `autoforge status --action X-… | gh issue comment <target>
  --body-file -` posts exactly the payload (its `ai-operator-comment`
  marker included) on the target; then `autoforge unblock --reason "..."`
  and `resume`. The comment counts as `exact` evidence when it is the one
  comment of the target carrying that marker and its bytes are the
  payload's.
- **bind a comment already there:** `autoforge unblock --bind-evidence X-…
  <comment URL> --reason "..."` (repeatable, one pair per action), then
  `resume`. The comment must be an issue comment of the run's repository on
  the action's target, in the target's complete comment listing, carry no
  controller marker (an earlier round's payload of this run for the same
  target may be bound, another action's payload of this plan may not), and
  be bound to, verified for or carry the payload of no other action of the
  round: one comment verifies one action. Every pending action's target is
  read first, and one that cannot be read refuses the binding. The
  binding records the comment's digest: the evidence is `bound`. If that
  comment is edited or deleted before `FIX` verifies it, `FIX` blocks again
  naming both digests: bind it again or post the payload.

`status` reads nothing from GitHub, so it shows what `FIX` last found. When
that verification failed (a bound comment edited or deleted, or a target
that cannot be read or is no longer an issue of the repository), the action
shows `verification failed at FIX's last check: …` (`state:
"verification_failed"` with its `reason` in `status --json`) until the next
`FIX` pass finds otherwise, or a new binding replaces the old one.

`resume` re-enters `FIX`, which reads every target and verifies every
action before anything else; it then publishes the rest of the plan. While
one is still missing it blocks again, naming what is verified so far. A
plain `unblock` is refused, with nothing written, unless something changed
since the block: a pending action is now on GitHub, or a recorded conflict
no longer reproduces (a new conflict is not progress). A target that cannot
be read (a permission lost, say) refuses the unblock, naming it; one that is
not found or answers as a pull request holds its action, and a conflict
recorded on it is not re-checked until it can be read. A conflict blocks
with the comments named; repair it, then `unblock`:

- the payload posted twice: delete one;
- the marker with other bytes (typically a paste in the web UI, which
  changes line endings or whitespace; text copied from a terminal can also
  lose trailing spaces and pick up the shell's end-of-line mark, as the
  web paste of the byte-fidelity capture in `docs/development.md` did):
  edit the comment back to the exact bytes, or delete it and post again
  through the pipe above;
- one action's payload posted on another action's target: delete it there
  and post it on its own target.

A payload posted on an issue that no action targets is neither read nor
detected: check the target in the `gh issue comment` line before you run
it. A comment edited or deleted after `FIX` verified it is not checked again
by the controller; the next review's `operator-evidence.md` shows what is
there now. An edit of the run's issue body after `FIX` started keeps the
actions bound to the targets `FIX` read, and `unblock` says the body
changed; a target transferred, deleted or no longer readable blocks naming
it (a target that is not found can be declined).

To withdraw them instead: `autoforge unblock --decline-action X-…
--reason "..."` (repeatable; name every action you decline). The unsent plan
is dropped: an action already verified stays verified, the named ones are
declined, the others are withdrawn, and `resume` runs the fixer again with
your reason in its prompt. A declined action is never requested again
unchanged, and verified work is never requested again. Declines count with
acceptance-action declines toward the same 16 per review round. An action in
conflict is repaired, never declined. Every pending action's target is read
first, named or not: a target transferred, in another repository or no
longer readable refuses the decline, naming it, while one that is not found
does not. `--approve-action X-…` is refused: there is nothing for the
controller to send.

A push to the PR or a change of its base while operator actions are pending
makes `unblock` re-enter `FIX`, which only reads: each action is recorded
`verified` or `stale`, and the PR goes back to `REVIEW`. The next review is
handed `operator-evidence.md`, the operator comments verified for the round,
and still judges whether they resolve the finding. `run` refuses to replace a
`BLOCKED` or `FAILED` run that holds operator actions without `--force`; the
comments stay on GitHub either way.

### DONE and FAILED

`DONE` is reached when `UPDATE_EPIC` reports no next issue. `DONE` and
`FAILED` have no outgoing edges; `autoforge run` starts a new run.

## Running an EPIC in parallel

`autoforge epic` runs up to `scheduler.max_parallel` issues of one EPIC at
once (1-4, default 2; `--max-parallel N` overrides it for one invocation),
each in a worker process that runs the single-issue workflow above on the
issue's own scheduler namespace and worktree (ADR 0008). The scheduler
decides which issue runs and when; each worker stops at its settlement
boundary (`READY_FOR_MERGE`, `BLOCKED`, `DONE`, `FAILED`) and never goes on
to `MERGE` or `UPDATE_EPIC`, whatever `safety.allow_merge` says, so a human
merges every PR. It is REMOTE only, and every `epic` command refuses
`--state-dir`: a namespace is found from the EPIC, never from a directory.
Every `epic` command also refuses an EPIC of another repository than the
one the checkout's `origin` remote names (exit 2), before it reads or
writes anything: the scheduler's files are this checkout's and are found
by EPIC and issue number, so another repository's EPIC would bind its
issues here, or stop or archive this checkout's EPIC of the same number.

```bash
# the scheduler's own checks too: the coordination root's filesystem, the
# scheduler root's permissions, and that gh can read the EPIC's graph
uv run autoforge doctor --epic https://github.com/owner/repo/issues/1

# the snapshot, every child's verdict and the admission plan; no lock, no
# namespace, no claim, no worktree, no worker
uv run autoforge epic run https://github.com/owner/repo/issues/1 --dry-run

# start a scheduler; it exits when no worker runs and nothing is admissible
uv run autoforge epic run https://github.com/owner/repo/issues/1 --max-parallel 3

# from another terminal, any time: lock-free and read-only
uv run autoforge epic status https://github.com/owner/repo/issues/1
uv run autoforge epic status https://github.com/owner/repo/issues/1 --json

# ask the scheduler to stop: a drain, or --now (Ctrl-C in its terminal)
uv run autoforge epic stop https://github.com/owner/repo/issues/1
uv run autoforge epic stop https://github.com/owner/repo/issues/1 --now

# after a crash, a kill, a reboot or a stop: classify, then continue
uv run autoforge epic resume https://github.com/owner/repo/issues/1

# release one settled or uncertain issue for a fresh run (never automatic)
uv run autoforge epic archive https://github.com/owner/repo/issues/1 7
```

### The lifecycle

- **Order.** The EPIC's children are its native GitHub sub-issues, and an
  issue's dependencies are its native "blocked by" links. A child is ready
  when it is open, supported, and every blocker is closed as completed; the
  scheduler admits ready children while a slot is free, those that block the
  most other children first, then the lowest number, and takes a new
  snapshot at most every `scheduler.poll_interval_seconds` while it may
  still admit. A blocker closed as not planned, as a duplicate or with no
  reason never unblocks anything. A child whose PR reaches `READY_FOR_MERGE`
  unblocks its dependents only once a human merged it and the issue is
  closed as completed.
- **One owner per issue.** The scheduler holds the repository lock for its
  whole life, so `run`, `step`, `resume` and a second `epic run` are refused
  while it runs (exit 2). Before a worker starts, the scheduler writes the issue's
  binding and opens a claim for it; the worker holds the issue's run lock
  while it runs. A claim stays open until the scheduler has seen the worker
  settle and closes it.
- **`epic run` vs `epic resume`.** `epic run` starts from what GitHub says
  and refuses (exit 2, nothing written) while any claim of the EPIC is
  open, naming each one: an earlier scheduler ended without closing it, and
  only `epic resume` decides what it means. `epic resume` first reconciles
  every namespace of the EPIC (a settled worker's claim is closed, a run
  that stopped mid-workflow is admitted again as a resume, anything it
  cannot classify is held as uncertain), then admits like `epic run`. While
  a worker of an earlier invocation still runs, it keeps the repository
  lock and `epic resume` exits 2: wait for it, or `epic stop` it.
- **`--force`** (on `epic run` only) moves an unreadable `scheduler.json`
  aside as `scheduler.json.corrupt-<timestamp>` and does nothing else: it
  never closes a claim, moves a state or skips the open-claim refusal.
- **Configuration.** Each worker re-reads the configuration file and
  refuses to start when it no longer matches the one the scheduler
  recorded: stop the scheduler before editing it. A resumed issue's run
  also refuses another profile routing or agent worktree root than the one
  it was recorded under. `epic run` and `epic resume` both admit a run that
  stopped mid-workflow (its claim closed) as a resume, but only
  `epic resume` takes `--accept-config-change`, which it passes to every
  resumed worker, as on `resume`
  ([Configuration](configuration.md#parallel-epic-scheduler)): a run that
  `epic run` resumes refuses such a change, and its issue is held for the
  rest of that invocation.
- **`--max-parallel N`** overrides `scheduler.max_parallel` for one
  invocation, within the same bounds (1-4): there is no unbounded mode.
- **Standalone commands.** The issue of the configuration's own standalone
  state (`.autoforge/state.json`, or `state_dir`) is held until that run is
  `DONE`; finish it with `run`/`resume`. A standalone `run`, `step`,
  `resume` or `unblock` refuses an issue a scheduler namespace owns
  (exit 2) until `epic archive` releases it. LOCAL runs are unaffected.

### `epic status`: one category per child

`epic status` takes no lock, writes nothing and never probes the
repository lock: it reads the snapshot from GitHub and each namespace's
claim, state and run lock, so it answers while a scheduler runs. Each child
gets exactly one category, decided only from the snapshot, the claims, the
state and the run-lock probe, with the next command where there is one:

| Category | What it means | What to do |
|---|---|---|
| `blocked-by-dependency` | an open blocker, or one closed other than as completed; the blockers are listed | nothing: it becomes ready when they are done |
| `ready` | open, supported, every blocker completed; no namespace state | nothing: the next admission takes it |
| `claimed` | an open claim and no live run lock: its scheduler ended without closing it | `epic resume` |
| `running` | a live process holds its run lock | wait; the claim's worker PID and process start time are shown as diagnostics and never signalled (a PID may be reused) |
| `ready-for-merge` | its PR is at `READY_FOR_MERGE` | merge it; dependents become ready once the issue is closed as completed |
| `blocked` | the run is `BLOCKED`; the reason is shown | fix the cause, then the printed `autoforge --state-dir <namespace>/state unblock --reason "<why>"` (while no scheduler runs: it takes the repository lock), then `epic resume` |
| `resumable` | a run stopped mid-workflow (a crash, a stop, a killed worker) | `epic resume` |
| `uncertain` | a claim, a state or a run lock that cannot be read or does not agree | repair the files by hand, or `epic archive` the issue, then `epic resume` |
| `settled` | `DONE`, `FAILED`, a hand-off or a pause; the scheduler launches nothing more for it | nothing if the issue is closed; otherwise `epic archive` for a fresh run |
| `held` | `standalone-state` (the standalone run's issue), `other-epic` (bound under another EPIC) or `spawn-failed` (its worker could not be started; held for the rest of that invocation) | finish the standalone run; use that EPIC's scheduler; the next `epic run`/`epic resume` tries again |
| `unsupported` | in another repository, or a parent of sub-issues itself | nothing: the scheduler never admits it |
| `closed` | closed on GitHub (and no settled state) | nothing |

It also shows the last scheduler invocation (started, ended and why),
admission (open, closed or degraded, and why), `max_parallel`, the last
recorded snapshot, a pending stop request, and namespaces of the EPIC the
snapshot no longer lists. An invocation recorded as not ended while no
worker runs says so: "not ended: if no `autoforge epic` process is running,
it ended without finalizing (crash or kill); run `epic resume`". Exit 0,
or 1 when the snapshot or `scheduler.json` cannot be read, `scheduler.json`
was written for another repository id than GitHub gives the EPIC's
repository (global uncertainty: `origin` re-pointed, or the name taken by
another repository; a rename keeps the id), a namespace (a child's, or one
the snapshot no longer lists) is `uncertain` (its binding, claim or state
cannot be read or classified, or its run lock cannot be probed) or bound
under another repository, or an entry of the scheduler's `issues/`
directory is not a namespace (a stray file, a link, a name that is no
issue number: never read as one, so move it out by hand). Each of these is
one of the listed problems.

`epic status --json` prints one document, schema `autoforge.epic-status/1`,
redacted. Its keys are fixed; a field without a value is `null`:

```text
schema            "autoforge.epic-status/1"
repository        "owner/repo"
epic              {number, url, issue_id}
scheduler         {state: absent | readable | unreadable, problem,
                   invocation {invocation_id, started_at, ended_at, end_reason, ended, hint},
                   admission {status, reason}, max_parallel,
                   last_snapshot {digest, observed_at, counts}}
stop_request      {present, mode: drain | now, invocation_id, requested_at, applies, problem}
snapshot          {taken, digest, observed_at, problem}
problems          [text]
children          [row], in admission order
other_namespaces  [row], namespaces of the EPIC the snapshot does not list
rejected          [{name, reason}], entries under issues/ that are not namespaces
                  (each is also one of problems)
row               {number, category, hold_reason, detail, verdict, blockers, phase,
                   settlement, classification, pr_url, state_dir, live, orphaned,
                   claim {claim_id, generation, kind, invocation_id, created_at,
                          worker {pid, process_start_time, spawned_at, log}},
                   next: [command]}
```

`category` takes the values of the table above (`null` only for a
namespace outside the snapshot that holds nothing) and `hold_reason` is set
for `held` only; `next` holds the commands in the order to run them. A new key or value is a
new schema version.

### `epic stop`

`epic stop` takes no lock: it publishes a stop request for the invocation
`scheduler.json` names and says what happened. The default is a drain:
admission closes at the scheduler's next tick and each worker returns at
its next step boundary. `--now` behaves like Ctrl-C in the scheduler's
terminal: the workers are interrupted and the scheduler waits
`scheduler.stop_grace_seconds` for them. A drain cannot be escalated by
another `epic stop`: press Ctrl-C in the scheduler's terminal. The outcomes
are distinct:

- delivered to the running scheduler (exit 0);
- delivered, but the invocation `scheduler.json` names has ended (exit 0):
  it lists the workers that still hold their run lock, which drain at their
  next step boundary (`--now` cannot signal them; nothing is ever signalled
  by a recorded PID), and the next scheduler removes the stale request;
- a request is already there, a drain blocking a `--now` among them, or the
  scheduler changed under the write (exit 1);
- no `scheduler.json` for the EPIC, or one of another repository: nothing
  to stop (exit 2, nothing created).

Every outcome that writes or finds a request lists the workers of the EPIC
that hold their run lock. A scheduler that crashed or was killed with
SIGKILL never records its end, so `epic stop` cannot tell its invocation
from a running one without trusting a PID: for an invocation that is not
ended, the listed workers come with the caveat that, if no `autoforge epic`
process of the EPIC is running, they survived it, a request reaches each
only at its next step boundary, and `--now` cannot signal them.

`scheduler.json` and each binding record the repository's numeric id and
its `owner/name` at the time; the name is for display only. A renamed
repository keeps its id under a new name, and a transferred or deleted
one leaves its name free for another repository, so `epic stop` and
`epic archive` read the repository's id from GitHub every time, whatever
name the file records: the same id is the same repository, and the command
goes on; another id, or no answer, refuses (exit 2, nothing written).

### `epic archive`

`epic archive <EPIC_URL> <ISSUE>` releases one issue for a fresh run: a
`settled` issue that is still open, or an `uncertain` one. It is never
done automatically. Under the repository lock and the issue's run lock
(refused while either is held), it closes the issue's open claim as
archived (an unusable claim file is moved aside), then moves
`state/state.json` aside as `state.json.archived-<timestamp>`, in that
order. It keeps the run logs, the worker logs, the worktree, the binding
and the claim history, and says so; the run's loop-bound evidence (review
rounds, fix attempts, the replan count) and its validation records then
live only in the archived state: the next admission starts the issue fresh
and adopts its marked PR. Re-running it completes an interrupted archive;
on an issue with nothing left it says "nothing left to archive". It reads
nothing from GitHub but the repository's id, which the binding's must be
(see `epic stop` above), and does not touch `scheduler.json`.

### Recovery and exit codes

Ctrl-C, a killed scheduler, a killed worker and a reboot all end the same
way: run `epic status`, then `epic resume`. Each crash window between the
scheduler's writes (ADR 0008 D14.5) is recovered by it, and no issue gets a
second worker or a second PR: a PR an interrupted run opened is adopted
([State and recovery](agent-guides/state-and-recovery.md#per-issue-scheduler-namespaces)).
`epic resume` never signals a process: a worker that survived its
scheduler keeps the repository lock until it exits, so wait for it (or
`epic stop` it) first.

| Exit | `epic run` / `epic resume` |
|---|---|
| 0 | nothing more is admissible and no claim is left open, or a drain finished |
| 1 | global uncertainty: admission degraded (the snapshot, `scheduler.json` or a namespace could not be trusted); see `epic status` |
| 2 | refused before anything ran: the repository lock is held (a scheduler, a standalone command or a surviving worker), open claims (`epic run`), `--state-dir`, an EPIC of another repository than the checkout's `origin` |
| 3 | it ended with claims of the EPIC still open (each is listed with its next command): `epic status`, then `epic resume` or `epic archive` |
| 130 | interrupted: Ctrl-C, a signal, or `epic stop --now` |

The codes are diagnostics for a human or a wrapper script, not a protocol
an automation should branch on: `epic status --json` is the machine-readable
answer.

## Watching an agent run

While an agent runs, `run`, `step` and `resume` (and `local run`) print live
progress lines to **stderr**, and each step's outcome to stdout as soon as
that step finishes, not after the whole loop. A progress line is prefixed
with the time since the launch, the phase and, in a remote run, the issue:

```text
[00:00:00 ANALYZE_EXECUTE #160] launching analyze_execute (claude, model fable, effort high), idle timeout 900s, max runtime unset, loop detection kill, attempt 1, worktree /repo/.git/autoforge/worktrees/160, log .autoforge/logs/<run-id>/001-analyze_execute-1
[00:00:04 ANALYZE_EXECUTE #160] agent started (claude-fable-5-1)
[00:00:31 ANALYZE_EXECUTE #160] thinking… ~12k tokens
[00:01:02 ANALYZE_EXECUTE #160] Read src/autoforge/engine.py
[00:01:09 ANALYZE_EXECUTE #160] Grep def _invoke_phase
[00:03:40 ANALYZE_EXECUTE #160] Edit src/autoforge/engine.py
[00:04:12 ANALYZE_EXECUTE #160] Bash Run the test suite
[00:06:12 ANALYZE_EXECUTE #160] still running, last activity 14s ago, 312 events
[00:09:55 ANALYZE_EXECUTE #160] agent exited 0, 841 progress events
[run-…] ANALYZE_EXECUTE -> REVIEW: pushed 3f2a9c1d0b7e to autoforge/160 and opened PR https://github.com/…/pull/161; ANALYZE_EXECUTE -> REVIEW
```

- **Before the launch**, one line names the profile, provider, model and
  effort, both agent limits (the idle timeout and the maximum runtime, see
  [Agent limits](#agent-limits)), the loop detection mode
  ([Loop detection](#loop-detection)), the attempt, the working directory
  and the step's log directory.
- **While it runs**: one line per tool the agent starts, with one
  allow-listed input (the file of a read, edit or write, the pattern of a
  search, the *description* of a shell command, never the command itself,
  a tool's result, thinking or the agent's text); thinking and writing are
  shown at most once every 30 s; a failed tool says so; when nothing has
  been printed for two minutes a heartbeat says how long ago the agent last
  did anything; and a `possible loop: …` line warns that the agent may be
  repeating itself.
- **After it returns**, one line says how it ended (exit status, the limit
  that killed it, or provider failure).

What a provider can report differs. Claude Code (with the default
`output_format: stream-json`) and Pi report each tool and their thinking;
OpenCode reports only that it is active, so its run shows the pre-launch
line, heartbeats that say when it last wrote output, and the end line. A
Claude profile with `output_format: text` reports nothing while it runs
([Configuration: Claude profiles](configuration.md#claude-profiles)).

Every line is stripped of escape and control characters, redacted and
bounded before it is printed. The same lines are appended to `progress.log`
in the step's log directory, created before the launch, so
`tail -f .autoforge/logs/<run-id>/<step>/progress.log` follows a step from
another terminal and the record survives a killed run. Progress never goes
to stdout and is never part of `status --json`; redirecting stderr
(`2>/dev/null`) hides it without affecting the run. A dry run launches no
agent and prints no progress.

## Agent limits

An agent is killed when it has made no progress, meaning it wrote nothing
to stdout or stderr, for its profile's idle timeout (default 900 s). Every
tool call Claude or Pi reports, and every line OpenCode prints, resets that
timer, so a long task is not killed for being long. Configure it per
profile; see [Configuration: Agent limits](configuration.md#agent-limits).

- **`max_runtime_seconds` is the only wall-clock bound on an active
  agent.** It is unset by default. Without it, an agent that keeps
  producing output while going in circles is killed by loop detection
  ([Loop detection](#loop-detection)), and one that keeps doing new things
  runs until the executor's one-week backstop. Set `max_runtime_seconds`
  on a profile when an unattended run must end by a known time, and always
  where loop detection only warns or is off: with `warn` or `off`, and by
  default on a Pi profile and in `UPDATE_EPIC`.
- **A silent tool call counts as no progress.** A test suite that runs for
  twenty minutes inside one shell command and prints nothing until it ends
  looks exactly like a hang. If your project has one, raise
  `idle_timeout_seconds` on the profiles that run it.
- **A Claude profile with `output_format: text`** writes nothing until it
  exits, so it has no idle timeout and must set `max_runtime_seconds`.
- **Only the agent's own output counts.** Nothing the controller does
  resets the timer, and neither will a wait for a human answer once agents
  can ask one (#126).

When a limit fires, the error names it, the agent's last output and the
configuration key that set it, for example `agent 'analyze_execute' made no
progress for 900s (last activity 08:31:02 UTC) and was killed. The limit is
set by execution.default_idle_timeout_seconds`, or `... reached its 7200s
maximum runtime and was killed. The limit is set by
profiles.analyze_execute.max_runtime_seconds`. The state is unchanged,
nothing is retried automatically, the error says where to look and how to
resume ([Troubleshooting](#troubleshooting-and-recovery)), and the step's
`execution.json` records `timeout_limit` (`idle` or `max_runtime`), both
configured values and `last_activity_at`: when the executor last saw
output on the agent's stdout or stderr before it decided to end the run.
What the CLI writes after that (Claude flushes a few records on SIGTERM,
Pi answers its `abort`) is drained from its pipes but not counted, and
only part of it is kept:

- `stderr.log` keeps the agent CLI's stderr, the shutdown's included.
- `stdout.log` keeps an OpenCode or text-mode Claude run's stdout, the
  shutdown's included.
- A stream-json Claude run and a Pi run write records on stdout, which
  their adapter reduces into `provider_summary` in `execution.json`. No
  record is logged: `stdout.log` holds only reply text, the last `result`
  text a Claude run received (an earlier turn's, or none, when it was
  killed inside a later one) or a Pi run's final reply, which a killed Pi
  run never has. The Claude adapter stops reading the stream once the run
  is to end (a limit, a loop stop), so the records written while the CLI
  is killed are not reduced either, and its summary is the stream as it
  stood at that moment. Pi's answer to the abort is read only to see the
  abort settle.

Both logs are bounded; `stdout_truncated` and `stderr_truncated` say when
one was cut. A record written before #343 counted the shutdown, so its
`last_activity_at` is a second or two before `finished_at`, and the
silence really began one idle timeout earlier.

### Reading an idle timeout

An idle timeout says that nothing reached the controller for that long,
not why, and the records do not always say what the agent did last. Find
where the silence was before raising a limit:

- **When the silence began.** `finished_at` minus `last_activity_at` in
  `execution.json` is how long the agent wrote nothing on either stream.
  In the step's `progress.log`, a heartbeat (`still running, last activity
  14m02s ago, 812 events`) is printed once no line has been printed for
  two minutes, and every two minutes after that. Its prefix is the time
  since the launch, so the prefix minus the age is when the last progress
  event came, and the event count stops growing once the agent is silent.
  `started_at`, the spawn, comes a little after the prefix's zero, so it
  puts that roughly on the clock of `last_activity_at`. An event is what
  the adapter reports, not every chunk of output, so the two can differ:
  Claude's stderr, and the notices it writes between turns, move
  `last_activity_at` but are no event.
- **What came last.** The last line printed before the heartbeats is the
  last event that was printed, not necessarily the last event. A finished
  tool and bare activity (a notice, a hook, every chunk of OpenCode's
  output) print nothing, and thinking and writing print one line per 30 s
  at most. When the heartbeat puts the last event well after that line,
  unprinted events came in between: a `Bash Run the suite` line can name a
  command that ended long before the silence, and nothing records what
  followed it. Only when the two agree, within those 30 s, is that line
  the last event. `progress.log` keeps its first MiB of lines; a run that
  printed more ends with a line counting the ones left out, and the lines
  around the silence are among those.
- **Where the stream stood.** `provider_summary` says what the adapter had
  counted when the run ended: a Claude run's `turns`, `results` and
  `open_tool_calls` (recorded from #343 on, and read with the caveats
  below), a Pi run's `tool_executions` and `loop_actions`. OpenCode reports
  no tool calls. Nothing records a tool's input beyond the one detail a
  progress line shows, or its output, so where these leave it open, what
  the agent did last is unknown.

These are the causes #343 found in the nine idle kills recorded on one
operator's machine between 2026-10-12 and 2026-10-26, and what helps with
each:

- **Claude waiting between turns** (`results` equal to `turns`,
  `open_tool_calls` 0; five of the nine). The agent ended its turn while
  something it had started was still pending (a background `Bash` task, a
  `Monitor`, a `ScheduleWakeup`), and the CLI stays alive and silent until
  that fires and the next turn begins. A background task's output goes to
  the task's own file, never into the stream, so it cannot reset the timer.
  In three of the five the agent's reply said it would report once the
  test suite it had started in the background finished, and the suite ran
  past the limit: past 900 s once, past 1800 s twice. This repository's
  serial suite took about 24 minutes on a busy 4-CPU workstation
  ([Development](development.md)). Raise `idle_timeout_seconds` on the
  profiles that run the suite (`analyze_execute`, `fix`) above the longest
  it takes on your machine, or make it finish sooner
  (`make test-parallel`). In the other two the reply was already final,
  with its `CONTROL_RESULT` block, and whatever was still pending kept the
  CLI alive anyway. The kill discards that reply and `resume` runs the
  phase again; a higher limit only waits longer for what is pending.
- **Claude inside a tool call** (`results` below `turns`,
  `open_tool_calls` above 0, unless a caveat after this list applies).
  The stream carries nothing while a tool runs. None of the nine was this:
  Claude's `Bash` tool ended every foreground command within about ten
  minutes, below the default limit. A tool that stays silent longer needs
  the profile's limit raised above it.
- **Pi inside `bash`** (one of the nine; `tool_executions` is one more than
  `loop_actions`). Pi 1.3.0 forwards a command's output while it runs, so a
  silent `bash` call is a command that printed nothing: a test run whose
  output goes to a file or through `tail`, or a command that hangs. The
  command is not recorded. Raise the profile's limit above how long that
  command stays silent, or have it print as it goes: `pytest -q` flushes a
  dot as each test ends, so only a single test longer than the limit is
  silent.
- **OpenCode after a dropped connection** (three of the nine). OpenCode
  printed `Transport: The socket connection was closed unexpectedly` on
  stderr and never exited. That is the hang the idle timeout exists for:
  `resume` relaunches it, and a higher limit only keeps it hanging longer.

The Claude counts are what the stream showed, not what the CLI was doing.
`results` equal to `turns` is every turn the stream opened having ended,
but anything else has more than one reading:

- **A background agent.** A turn that ends while an `Agent` call run in
  the background is still working gets its `result` only once no
  background agent is left, and the next turn opens all the same
  (verified on claude 2.1.293). So `results` below `turns` can be turns
  that are over and wait for their results, and the background agent's
  own tool calls count in `open_tool_calls` like the main conversation's:
  a conversation between turns whose background agent runs the test suite
  shows the counts of a foreground tool call. An `Agent` line in
  `progress.log` is the sign to look for; the summary does not tell the
  two apart.
- **No tool running.** `results` below `turns` with `open_tool_calls` 0
  and no background agent is a turn still open with no call in flight:
  after a tool's result, before the model's next record.
- **A skipped record.** A record past the per-line bound, such as a
  `tool_result` quoting a large file, is skipped and counted in
  `oversize_records`. Its call then stays open, so with `oversize_records`
  above 0 `open_tool_calls` can count calls that have ended, and a skipped
  `tool_use` is a call never counted.
- **The bound.** At most 1024 calls are tracked open at once, and a call
  without an id is not tracked; past the bound `open_tool_calls` stops
  growing.

`execution.command_timeout_seconds` (default 1800) is not an agent limit:
it bounds only the commands the controller runs itself
(`validation.commands`, the pre-merge and LOCAL validation commands), and
how long a GitHub CI run took says nothing about either. Whatever the
cause, a higher idle timeout also lets a hung agent hang that much longer,
so raise it only on the profiles that need it, and set
`max_runtime_seconds` when the run must still end by a known time. Do not
make the agent print just to keep the timer alive. A Claude tool's output
reaches the stream only once the tool ends, and a background task's never
does, so it changes nothing there; where it does reach the controller as
it is written (OpenCode's text, Pi's `bash` updates) it hides a hang from
the idle timeout. In OpenCode's output, loop detection's
repeated-lines signal catches a repeated line or a counter, but on Pi only
`max_runtime_seconds` would end it.

## Loop detection

An agent can keep producing output while it repeats itself: the same
command, the same file read, the same answer, round and round. Its output
resets the idle timer, so the idle timeout never fires. The controller
watches what the agent does and reports a loop when the same actions, with
the same inputs and the same results, keep coming back with nothing new in
between ([Configuration: Loop detection](configuration.md#loop-detection)).

From half a threshold the progress output warns, and at the threshold the
agent is killed:

```text
[00:06:40 FIX #160] possible loop: 2-step cycle (Bash, Read) repeated 4×
[00:09:14 FIX #160] agent killed: the same 2-step action cycle (Bash, Read) repeated 8 times over 9m14s with no new action, 1834 progress events
```

The warning repeats at most every two minutes while the loop goes on, and
the step's `execution.json` records it (`loop`, `loop_warnings`).

- **In `kill` mode (the default, except on a Pi profile and in
  `UPDATE_EPIC`)** the agent is killed at the threshold,
  exactly as on a timeout: its whole process group is ended, the state is
  unchanged and nothing is retried automatically. The error names the
  pattern, followed by where to look and how to resume
  ([Troubleshooting](#troubleshooting-and-recovery)):

  ```text
  agent 'fix' was killed: the same 2-step action cycle (Bash, Read) repeated 8 times over 9m14s with no new action
  State unchanged.
  ...
  ```

  The step's `execution.json` says `ended_by: loop` and `timeout_limit:
  loop`, with a `loop` entry naming the signal (`action_cycle`,
  `no_novelty`, `repeated_lines` or `retry_storm`), the cycle's period, its
  repeat count, the tool names and when the loop started and was cut,
  measured from the launch. It records no tool input, tool result or
  output line.
- **In `warn` mode** the agent runs on past the threshold, and the
  progress output says `loop detected, not killed (mode warn): …` instead.
  Watch for these lines, and stop the run (`Ctrl-C`) if the agent is
  stuck. A config that does not set `mode` runs a Pi profile and
  `UPDATE_EPIC` this way, because no real run has measured either yet;
  `mode: kill` kills there too. The launch line names the mode in force.

**After a loop kill**, look at the agent worktree and the PR the way you
would after a timeout: the agent may have pushed part of its work. Then
`resume` relaunches the same phase in the same worktree. A loop that comes
back on every attempt usually means the task, or a flaky command it keeps
re-running, needs a person: fix that before resuming. Raising the
thresholds is the last resort.

In every mode, `off` included, `provider_summary` in `execution.json`
records the `loop_*` calibration fields: how close the invocation came to
each threshold. The defaults were checked against those fields from real
runs (#199,
[Configuration: How the defaults were set](configuration.md#how-the-defaults-were-set)).

## Dry run

A dry run invokes no agent, runs no verification or validation command, writes nothing to
GitHub, git or the state directory (no state, no log), takes no lock and
creates no agent worktree. It prints the plan (phase, provider,
model/effort, agent limits, loop detection, round, template, variables,
command, expected transition),
redacted. What it reads depends on the command:

- `run --dry-run` previews a new run and launches no subprocess: no `git`,
  no `gh`.
- `step --dry-run` and `resume --dry-run` preview the existing run and make
  no `gh` call. Without `--state-dir` they first run two read-only
  `git rev-parse` calls to find which run is meant, a remote one under
  `.autoforge/` or a local one in the git directory; `--state-dir` skips
  that lookup.
- `unblock --dry-run` does the same lookup, then reads GitHub to report the
  decision it would make (with `--approve-action`, `--decline-action` or
  `--bind-evidence` too).

## Where things live

- **State and logs:** `.autoforge/` in the working directory by default
  (`--state-dir` or `state_dir` in config): `state.json` plus one log
  directory per run with the redacted prompt, output, and parsed result of
  every agent invocation, and `read-context/<run-id>/`, the GitHub data each
  REMOTE launch was handed. Layout:
  [State and recovery: runtime artifacts](agent-guides/state-and-recovery.md#runtime-artifacts).
  The directory must be on a filesystem with hard links: run logs are
  published by `link(2)`, so vfat/exFAT and some FUSE, SMB/CIFS and overlay
  mounts are refused before the first launch (and by `doctor`) with an
  error that says so; point `--state-dir` at another filesystem.
- **Repository lock:** `<repo>/.git/autoforge/controller.lock`, the same
  file for every `--state-dir`, subdirectory and linked worktree of one
  checkout. A second controller exits with `LockError`, whose message adds
  that an AutoForge scheduler or its surviving workers may hold it.
  `<repo>/.git/autoforge/git.lock` is held for a moment around each
  controller write to the shared git data (a fetch, the agent worktree's
  creation, the import of an agent's objects); a controller that waited too
  long for it reports that operation as failed, and `resume` tries again.
- **Scheduler namespaces:** `<repo>/.git/autoforge/scheduler/` belongs to
  the parallel EPIC scheduler (ADR 0008;
  [Running an EPIC in parallel](#running-an-epic-in-parallel)): one directory
  per issue, with its own state. `run`, `step`,
  `resume` and `local run` refuse a `--state-dir` or `state_dir` that
  resolves inside it (exit 2, before the lock is taken); `status` reads such
  a state directory and `unblock` acts on it. An issue whose namespace
  holds a state, or whose run lock a live worker holds, belongs to the
  scheduler: `run`, `step`, `resume` and `unblock` refuse to act on it
  (exit 2, nothing written) until `autoforge epic archive` releases it, and
  a sequential run never selects it as its next issue. Layout:
  [State and recovery: per-issue scheduler namespaces](agent-guides/state-and-recovery.md#per-issue-scheduler-namespaces).
- **Agent worktrees:** `<repo>/.git/autoforge/worktrees/<issue-number>` (or
  under `execution.worktree_dir`), added detached at the checkout's HEAD the
  first time an agent is launched for the issue and reused by every later
  phase of that issue. AutoForge never deletes it: remove it with
  `git worktree remove` once the issue is done.
- **Sandbox directories** (when the outer sandbox is enforced):
  `<repo>/.git/autoforge/sandbox/<issue-number>/` holds the issue's private
  object directory (`objects/`, where an agent's commits land until the
  controller imports them), the overlay's work directory and the cache its
  `ANALYZE_EXECUTE`, `FIX` and `REPLAN_REEXECUTE` launches share. They are
  created at the issue's first enforced launch and never deleted: remove
  them with the worktree
  ([State and recovery: runtime artifacts](agent-guides/state-and-recovery.md#runtime-artifacts)).
  A LOCAL run's cache is `<repo>/.git/autoforge/sandbox/local/cache`.
  `doctor` lists them with their sizes.

### How agents are separated from your checkout

Every REMOTE agent runs in that per-issue worktree, so its working directory
contains neither `.autoforge/`, the lock, nor the operator's uncommitted
work, and a `git clean -fdx` there cannot reach controller state. It starts
from an allow-listed environment: `execution.env_allowlist` (a documented
default of PATH, HOME, locale, temporary directory, XDG directories, git
author settings and the proxy and CA variables) plus `env_allowlist_extra`,
plus the names the provider adapter declares for its own CLI (`ANTHROPIC_*`
and `CLAUDE_*` for Claude Code; `OPENCODE_*`, `OPENAI_*`, `ANTHROPIC_*`,
`GEMINI_*`, `GOOGLE_*` for OpenCode); nothing else in the operator's
environment is inherited, and the names (never the values) are recorded in
the invocation's `request.json`, with the names the controller sets
(`env_set`). Before and after each invocation, including before every
relaunch within a step, the controller reads HEAD and the checked-out branch
of its own checkout; a change enters `BLOCKED` with the drift in the reason,
nothing is rolled back, and `unblock` reconciles the agent's GitHub work. Under
the default `sandbox.mode: disabled` this is process and filesystem-level
separation, not a sandbox: the agent still runs as the operator with network
access. `sandbox.mode: required` adds ADR 0005's outer sandbox to every
launch: the agent sees the system runtime, a private home and `/tmp`, its
worktree, the repository's git metadata read-only, its read context and
what its provider declares; your checkout, the state directory, the lock,
other worktrees and the rest of your home are absent. It still runs as your
UID on the host network, and a host that cannot enforce is refused before
the step rather than running it without the sandbox
([Outer sandbox](#outer-sandbox)).

The agent holds no GitHub or git credential (#165). The controller pushes,
opens the PR, posts the review comment, creates follow-up issues and
updates the EPIC itself; the agent writes the text and commits locally. No
variable `gh` or git reads a credential from can be allow-listed (a token
you forward under a name of your own, `MY_GITHUB_PAT` say, is not
recognised and reaches every launch: do not list one), and each launch
gets an empty `gh` configuration, a placeholder `GH_TOKEN`, a git
configuration holding only your author identity, and git settings no
repository configuration can override that disable credential helpers,
askpass and the SSH agent and keys
([Configuration: agent environment](configuration.md#agent-environment)).
What the agent needs from GitHub (the issue, the PR and its checks, the
review comment it acts on, the EPIC) the controller reads first and writes,
redacted and bounded, into `<state dir>/read-context/<run-id>/`; the prompt
names that directory. Before every launch the controller refuses to start
when a setting would still hand over a credential (a credential helper in
the repository's git configuration, a token in a remote URL, a GitHub entry
in `~/.netrc`, an `export GH_TOKEN=...` in `~/.bash_profile` or
`~/.profile`, an organization connected to OpenCode), naming the setting,
never its value; the run is left untouched, so remove the setting and run
the same command again.
`autoforge doctor` runs the same check. What this does not stop (a
deliberate read of `~/.config/gh/hosts.yml` by absolute path, for one) is
in [GitHub safety](agent-guides/github-safety.md#threat-model); ADR 0005's
outer sandbox (`sandbox.mode: required`, or `preferred` where the host can
enforce) closes it for every enforced launch
([GitHub safety: outer sandbox threat model](agent-guides/github-safety.md#outer-sandbox-threat-model-adr-0005)).

### Outer sandbox

`sandbox.mode` (ADR 0005, #168, #169) chooses between three postures, and
every surface names the one in force:

| `sandbox.mode` | the host can enforce | the host cannot enforce |
|---|---|---|
| `disabled` (default) | logical isolation only | logical isolation only |
| `required` | every launch enforced | refused before the step: nothing saved, charged or launched |
| `preferred`, REMOTE | every launch enforced | refused, exactly as `required` |
| `preferred`, LOCAL | every launch enforced | every launch with logical isolation only, said at each launch |

*Logical isolation* is what every mode has: the per-issue worktree, the
environment allow-list and the controller's own `gh`, git and shell
configuration. The *outer sandbox* is the OS-enforced boundary on top. A
launch is *enforced* only when it ran inside it; a `preferred` fallback is
never called sandboxed.

- **Before a run:** `autoforge doctor` (or `autoforge local doctor`, which
  makes no GitHub call) shows the capability probe's result, one real
  minimal sandboxed launch that names the first thing the host lacks
  (`bwrap`, an unprivileged user, PID or IPC namespace, a new procfs, the
  sandbox's bind mounts, an overlay mount, Landlock ABI 6), and what the
  mode does with it: `disabled` is a WARN ("logical isolation only", a
  risk); `required` or REMOTE's `preferred` on a host that cannot enforce is
  a FAIL; LOCAL's `preferred` there is a WARN that launches will fall back.
  It also shows the policy each launch class gets; for each provider its
  declaration (paths and credential classes, never a value) and, when the
  sandbox is enabled and the host can enforce, its `--version` run inside
  with the environment its launches get (the provider's own configuration
  isolation, and path-valued variables spelled where they lead inside);
  a keyring login, or a `dir-rw` login inside the provider's own
  configuration root, with the supported alternatives (a login file, a
  dedicated credential directory, an API key variable); a declaration that
  reaches what no launch may see (the run's state directory, the lock,
  another worktree), refused as each of its launches would be; hard-linked files
  in the trees an agent writes (each still aliases an inode outside: remove
  the REMOTE worktree or sandbox directory, or break the link); the sandbox
  directories and their sizes; the TCP listeners on loopback and wildcard
  addresses, which every launch can reach (an X11 port, 6000–6063, is a
  WARN); and `sandbox.read_only_paths` a run would refuse.
- **The dry run** shows the mode and its outcome and the compiled policy by
  launch class. It runs no probe, so it reads the same on every host.
- **`status`** shows the mode and what it means, without probing.
- **At a step:** under `required`, and under REMOTE's `preferred`, a step
  that may launch first runs the probe (once per controller process). A
  host that cannot enforce is a configuration error, exit 2, before the
  step saves, charges or launches anything; the state is byte-identical.
  Under LOCAL's `preferred` each launch that falls back prints "this agent
  runs with logical isolation only, NOT inside the outer sandbox: ..." (or
  "validation command ...") on stderr, and its execution record says
  `"sandbox": {"enforced": false, "mode": "preferred", "isolation":
  "logical isolation only", "fallback": "<reason>"}`.
- **Across commands (#280):** a run is pinned to the mode it was created
  under, and `status` shows the pin. `step`, `resume` and `unblock` refuse
  a weaker mode before any phase executes, a command run without the run's
  `--config` (the built-in `disabled`) included, unless repeated with
  `--accept-config-change`, which writes the change to the run log first
  and pins the weaker mode. A stronger mode is pinned without asking
  ([Configuration: outer sandbox](configuration.md#outer-sandbox)).
- **Where the configuration lives (#280):** under `required` or
  `preferred`, every command, `doctor` and a scheduler's worker refuse a
  `--config` file inside the checkout, any worktree of the repository or a
  directory a provider is given read-write (a dedicated
  `CLAUDE_CONFIG_DIR`, `PI_CODING_AGENT_DIR` or OpenCode database
  directory), or reached through a link into one, passing through one
  before a `..`, or with a second hard link, since an enforced launch could
  rewrite it. Keep it in, for example, `~/.config/autoforge/`, and run a
  run's commands from the same environment: a provider directory given
  only under another one is not known.

AutoForge never commits to the default branch, never deletes a worktree it
created, and never creates or cleans up local branches or any other
worktree: leaving old local work untouched is safer than trying to infer
ownership or discard uncommitted user changes.

## Troubleshooting and recovery

- **The process was interrupted** (`Ctrl-C`, a killed process, a reboot):
  run `autoforge resume`. State is written atomically, and every phase that
  launches an agent reads its journal and GitHub first, so work already
  done (an open PR, a review comment the controller posted, a pushed fix)
  is adopted or completed rather than repeated. See
  [Workflow: re-entering a phase](agent-guides/workflow.md#re-entering-a-phase).
- **An agent failed, timed out or returned a malformed result:** a malformed
  `CONTROL_RESULT` from an agent that exited 0 is retried with a correction
  prompt (`execution.max_correction_attempts`, default once). The controller
  reads the block only from the end of the agent's final reply, never from
  a command's output or a file, and the correction prompt quotes the end of
  the refused reply (its last 4,000 characters, redacted, fenced as
  untrusted data) after the controller's diagnosis, so the new session can
  see what the previous one said it had done; it still checks that against
  the worktree. The correction's `request.json` records the quote's sizes
  (`metadata.correction_reply`), its `prompt.md` the quote as sent. A
  `resume` after a crash or a spent correction budget quotes nothing. No
  correction is launched when the refused launch may have left a process
  running: then the error says so
  (`Not re-invoked: a process the launch started may still be running`,
  followed by which one); stop it before `resume`. The one failed
  exit relaunched automatically is a launch its adapter reports as transient
  (today an OpenCode `openai/*` run whose errors are all `WebSocket closed
  with code 1000`, `WebSocket closed with code 1008` or OpenAI's generic
  `server_error: An error occurred while processing your request. ...`,
  even when the session recovered from it and printed a result: OpenCode
  exits 1 once it has printed one, and a result from a failed exit is not
  read; and a Claude Code `stream-json` run whose turn ended on a model API
  error with status 408 or 5xx once the CLI's own retries were spent, 529
  overloaded among them, but never another 4xx such as a 429): a REMOTE run
  relaunches it at most twice per step, after
  re-reading GitHub, and when it stops the error says why ("not relaunched:
  ..."). Any other non-zero exit, a timeout or a verification failure is not
  retried automatically and leaves the phase unchanged for `resume`. Inspect
  each invocation under `<state dir>/logs/<run-id>/`; a transient failure's
  `error.txt` says `(transient: ...)`. A timeout says which limit fired and
  the key that set it; see [Reading an idle timeout](#reading-an-idle-timeout)
  before raising one.

  Every agent failure that leaves the phase unchanged (a timeout or loop
  kill, a failed exit or provider failure, an agent that could not be
  started, a malformed result that is not corrected) ends the same way:

  ```text
  autoforge: error: agent 'analyze_execute' made no progress for 1800s (last activity 08:31:02 UTC) and was killed. The limit is set by profiles.analyze_execute.idle_timeout_seconds
  State unchanged.
    worktree:  /home/me/code/.worktree/159   (3 uncommitted paths)
    logs:      .autoforge/AutoForge-issue-159/logs/af-20261006T101500Z-3f2a9c/001-analyze_execute-1/
    resume:    autoforge --config autoforge.yaml --state-dir .autoforge/AutoForge-issue-159 resume
  Inspect the real Git/GitHub state first; 'resume --dry-run --max-steps 1' with the same flags shows the profile, limits and worktree it will use.
  ```

  - `worktree` is the issue's agent worktree, with the number of paths
    `git status` reports there as changed or untracked: what the attempt
    left behind. The count is left out when that read fails. A LOCAL run
    prints no `worktree` line: its agent works in your checkout. AutoForge
    leaves those changes where they are, and the next launch of a phase
    that writes (`ANALYZE_EXECUTE`, `FIX`, `REPLAN_REEXECUTE`) is told
    about them: its prompt names the worktree's branch and `HEAD` and lists
    each change's status code and path (at most 50, never file content),
    and asks the agent to inspect them and then continue from them or
    discard them deliberately, saying which. The invocation's
    `request.json` records what was found (`metadata.worktree_state`), or
    why the read failed, in which case the prompt describes nothing.
  - `logs` is the failed launch's step directory (its progress, output and
    error).
  - `resume` repeats the `--config` and `--state-dir` this command was
    started with, as you typed them; run it from the same directory. The
    CLI does not look for a configuration file, so a bare `resume` after a
    run started with them finds no run (no `--state-dir`) or would run on
    the built-in defaults (no `--config`): another model, other limits and
    another worktree. A REMOTE run refuses that: see
    [the configuration a run was recorded under](#the-configuration-a-run-was-recorded-under).
- **"the agent asked for permission (...)" or "an answer to a question"**
  in a failure: see
  [An agent asked for permission or input](#an-agent-asked-for-permission-or-input).
- **"run ... was recorded under ..., and this command's configuration
  differs from it"** (exit 2): see
  [the configuration a run was recorded under](#the-configuration-a-run-was-recorded-under).
- **The run is `BLOCKED`:** read `autoforge status`, fix the cause, then
  `autoforge unblock --reason "..."` and `autoforge resume` (see above),
  with the `--config` / `--state-dir` the run was started with; `unblock`
  prints that `resume` command. A LOCAL run cannot be unblocked: start a new
  `local run`. A run blocked because a checkout moved (the checkout the
  controller is run from, or a LOCAL run's repository: an agent committed,
  reset or switched branches) ends with where to look and what to run,
  already carrying those flags:

  ```text
    checkout:  /home/me/code/AutoForge
    unblock:   autoforge --config autoforge.yaml --state-dir .autoforge/AutoForge-issue-159 unblock --reason '<what you found>'
    resume:    autoforge --config autoforge.yaml --state-dir .autoforge/AutoForge-issue-159 resume
  Inspect the checkout and the agent's GitHub work first: 'unblock' re-inspects GitHub and re-enters a phase, and only then does 'resume' continue the run.
  ```

  A LOCAL run prints a `new run:` line (`autoforge ... local run <the
  feature's absolute path>`) instead of `unblock` and `resume`.
- **"refusing to launch: a process AutoForge launches would be handed a
  GitHub or git credential":** the message names the setting (for example
  a credential helper in the repository's git configuration, a remote URL
  with a token, a GitHub entry in `~/.netrc`, or the startup files of a
  login shell that change a variable such as `GH_TOKEN`). Nothing was recorded:
  remove the setting (`autoforge doctor` lists it too) and run the same
  command again. A config that fails to load because `env_allowlist` names
  a variable `gh` or git reads a credential from: delete that entry
  ([Configuration: agent environment](configuration.md#agent-environment)).
- **"refusing to launch opencode: it would load the configuration of an
  organization connected to it":** OpenCode would load an organization's
  configuration, which may name an MCP server with a GitHub credential of
  its own, and nothing turns it off. Nothing was recorded: give AutoForge's
  OpenCode launches a database of their own (`OPENCODE_DB`, see
  [Configuration: agent environment](configuration.md#agent-environment))
  and run the same command again.
- **"this host cannot enforce the outer sandbox: it cannot provide ..."**
  (`SandboxCapabilityError`, exit 2; `sandbox.mode: required`, or
  `preferred` in REMOTE): the pre-launch gate refused the step. Nothing was
  launched, saved or charged. `autoforge doctor` (or `local doctor`) shows
  the same capability row; make the host capable (install `bwrap`, allow
  unprivileged user namespaces, a kernel with Landlock ABI 6 that booted
  with Landlock enabled; the row's reason says which of the two a kernel
  lacks, see [Compatibility](compatibility.md#outer-sandbox)), or set
  `sandbox.mode: disabled` (logical isolation only) or, in LOCAL,
  `preferred`, and run the same command again.
- **"the configuration file ... lies in ..., a tree that launches inside
  the outer sandbox write"** (exit 2; also "is reached through ...", "has
  N hard links" or "opens another file than its lookup walk was followed
  to"; "the sandbox declaration of profile ... cannot be read" when a
  provider's declaration could not be asked): under `required` or
  `preferred` the `--config` file is where an enforced launch could
  rewrite it, or is not shown not to be (#280). Nothing was run, launched or
  written. "Is reached through" names a directory of such a tree the path
  looks a name up in: a symbolic link leads there, or the path passes
  through it before a `..`, which is followed as the command opens it,
  never tidied. When the directory is a provider's (the message names
  `$CLAUDE_CONFIG_DIR`, `$PI_CODING_AGENT_DIR` or `$OPENCODE_DB` and the
  profiles whose launches write it), every enforced launch of that
  provider writes it, wherever it lies. Move the file outside the
  checkout, every worktree of the repository and every such directory,
  for example to `~/.config/autoforge/`, and pass its own path; or give
  the provider another directory; or set `sandbox.mode: disabled`.
  `doctor` reports the same in its config row and runs none of the
  commands the file names.
- **"run ... is pinned to sandbox.mode ..., and this command's
  sandbox.mode, ..., contains its launches less"** (exit 2): the command
  runs under a weaker mode than the run was created under, often because
  it was run without the run's `--config`. Nothing was written. Run it with
  a configuration that sets the pinned mode, or repeat it with
  `--accept-config-change` to continue under the weaker one (recorded in
  the run log).
- **"sandbox.read_only_paths[N] ... overlaps ..., which no launch may see":**
  an operator read-only path names, contains or lies inside the state
  directory, the lock, `<git common dir>/autoforge`, another worktree or (in
  REMOTE) your checkout. Nothing was launched; narrow or remove the entry.
- **"the sandbox cannot be established ..."** (`SandboxLaunchRefusedError`,
  the sandbox enforced): the launch did not run, and nothing ran
  without the sandbox. The message names the cause: a backend that failed
  to set the sandbox up after the gate passed, a sandbox directory that is a link, or a path and the
  policy rule it breaks ("sandbox policy refuses <path>: <rule>"), such as
  a `CLAUDE_CONFIG_DIR`, `OPENCODE_DB` directory or `PI_CODING_AGENT_DIR`
  inside the provider's own configuration directory
  ([Configuration: provider credentials](configuration.md#provider-credentials-under-required)).
  For an agent the phase is unchanged: fix the cause, or set
  `sandbox.mode: disabled`, and `resume`. A refused validation command
  leaves the phase unverified; a refused pre-merge command blocks the merge
  like a failing one. "the agent was not launched: the strict import ...
  failed" is the same refusal, in either mode, for objects an earlier
  enforced launch left in the issue's private object directory that the
  controller cannot import; inspect the directory before resuming.
- **"the agent worktree ... is not bound to the repository":** the
  worktree's `.git` file and the repository's worktree registry disagree
  (the worktree or the repository was moved, or something rewrote the
  pointer). The controller reads a worktree only through the registry entry
  that names it, so nothing was launched. Run `git worktree repair` in the
  repository (or `git worktree repair <worktree>`), then `resume`.
- **`BLOCKED`: "the private object directory ... has been tampered with":**
  an `info/alternates` file, a link or another entry no object store holds
  appeared in `<repo>/.git/autoforge/sandbox/<n>/objects`, or that
  directory, or one above it up to `<repo>/.git`, is not a real directory
  (a link, say) or cannot be checked. Nothing was read
  from it and nothing was published. Inspect it, remove what does not
  belong there, then `unblock` and `resume`. To look at an agent's commits
  while they are still only in that directory, read them through it
  without writing anything:
  `GIT_ALTERNATE_OBJECT_DIRECTORIES='"<repo>/.git/autoforge/sandbox/<n>/objects"' git -C <worktree> log`.
  The inner double quotes keep git from splitting a path that contains a
  `:`; inside them, write a `"` or a `\` of the path as `\"` or `\\`. The
  outer single quotes are the shell's, and end at a `'`: write a `'` of the
  path as `'\''` (close the quotes, an escaped `'`, reopen them), after
  git's escapes.
- **Merge verification keeps saying "inconclusive":** checks still running,
  mergeability unknown or a transient GitHub failure; re-run
  `autoforge resume --allow-merge`. After `merge.max_verification_attempts`
  attempts the run is `BLOCKED`.
- **The state file is unreadable:** `run`, `resume`, `step` and `status`
  exit 2 and never overwrite it. `run --force` moves it aside as
  `state.json.corrupt-<timestamp>` (never deletes it) and starts a new run;
  see [State and recovery](agent-guides/state-and-recovery.md#persistent-state).
- **`LockError`:** another controller holds the repository lock (a
  standalone command, or a parallel EPIC scheduler or one of its surviving
  workers), or a lock entry under `.git/autoforge/` is not a regular file
  with a single name; see
  [State and recovery: locking](agent-guides/state-and-recovery.md#locking).
- **A replan was interrupted:** the phase is still `REPLAN_REEXECUTE`.
  `autoforge status` prints the transaction id, stage and both PRs, and
  `resume` continues the transaction from its persisted stage. Once the
  replacement's push and PR are journaled, `resume` completes them without
  running the agent again; before that it runs the agent again on the same
  base. A transient GitHub failure inside the transaction leaves it
  resumable the same way.
- **The run is `BLOCKED` with a replan transaction on record** (a rejected
  replacement or source, or a journal the controller could not read): this
  run is over. `resume` exits 1 as for any `BLOCKED` run, `unblock` refuses
  while the journal exists, and repairing the replacement PR does not reopen
  a rejection. `autoforge status` shows the block reason and the journal's
  stage and both PRs; a rejection's reason also says what the transaction
  did to the source PR. Inspect those PRs on GitHub and decide by hand
  which one, if either, carries the issue forward. To continue, start a new
  run; it replaces `state.json`, journal included, so read `status` first.
  Its `ANALYZE_EXECUTE` adopts the one open PR carrying the issue's
  `ai-implementation` marker and blocks on two, so close the PR you do not
  keep. See [Replan transaction](agent-guides/replan-transaction.md).

### An agent asked for permission or input

An unattended run has no one to ask. When an agent wants a permission a
rule only grants on approval, or an answer to a question, the agent CLI
refuses the request itself and, on the versions below, tells the agent to
go on without it. AutoForge cannot answer either until #126 (and the SDK
providers, #151 and #152) land.

| CLI (version; how this is known) | A permission that needs approval | A question |
|---|---|---|
| Claude Code (2.1.312; run against a loopback stand-in of the Messages API, no model called) | With `options.permission_mode: bypassPermissions`, the default, nothing was asked, a write under `.git/` or `.claude/` included. Under another mode (`manual`, `dontAsk`, ...) a call the mode does not allow, or one a deny rule of your Claude Code user settings names, is refused at once. The agent is told to go on without it, and the run exits 0. | `AskUserQuestion` is not offered under `-p`. A call to it anyway fails ("No such tool available") and the run goes on. |
| OpenCode (2.1.4; run against the same stand-in, and read from its build) | Without `options.auto_approve`, a rule that asks prints `! permission requested: <permission> (<patterns>); auto-rejecting` on stderr and is rejected. With the empty configuration every launch gets, reading `.env` asked; reading outside the worktree did not. The agent is told to go on without it, and the run exits 0. | The `question` tool is offered, and a question is cancelled: `✗ Asked 1 question failed`, then an error line telling the agent to assume and say so. The run exits 0. The question's text is not printed. |
| OpenCode (2.0.26; read from its build, not re-verified) | Exits 1 with no error line once it has refused one. Whether it printed the `permission requested` line is not recorded. | Not recorded. |
| Pi (1.3.0; read from its build and docs) | Pi has no permission rules: tools run with your account's permissions. | No question tool. Under `--no-extensions` and `--no-approve`, which every launch passes, nothing opens a dialog. One that did would be cancelled and fail the run, naming its method (`pi: an extension asked for input (...)`). |

What AutoForge does with it:

- **A run that ends usable is left to its result.** The agent went on
  without what it asked for, and its `CONTROL_RESULT` decides, as before.
  The step's `execution.json` records how many requests were refused and
  the first one (`provider_summary.refused_requests`,
  `refused_request`).
- **A refused request that is all that failed the run** (OpenCode exiting
  1 with no error line of its own after a `permission requested` or
  cancelled-question line) fails the step as
  `agent '<profile>' failed: opencode: the agent asked for permission (read:
  .env), which an unattended run cannot give; ...`, with what to do and the
  usual "State unchanged" lines. It is not corrected and not relaunched,
  since a relaunch would ask again (ADR 0007 D7.6).
- **Any other failed run** keeps its own reason, exit status and
  relaunch behaviour, and its text adds `during the run the agent asked
  for ..., which <CLI> refused`. The refusal may not be why it failed.
- **A CLI that goes silent** after a request is killed by the idle limit
  like any silent CLI ([Agent limits](#agent-limits)).
- **When nothing in the output identifies a request**, the failure is the
  generic one: Claude Code with `options.output_format: text`, which prints
  no stream, and an OpenCode exit 1 with no error line and no request line.
  AutoForge does not guess from other text.

What to do:

- **A permission:** keep the issue's work inside the worktree, or change
  the issue so the agent does not need it. For OpenCode,
  `options.auto_approve: true` approves every permission no rule denies,
  reading a `.env` file of secrets included, so it is dangerous. For Claude
  Code, `bypassPermissions` (the default) asks for nothing, and a deny rule
  of your user settings (`~/.claude/settings.json`, or the one under
  `CLAUDE_CONFIG_DIR`, which every launch reads) refuses what it names
  whatever the mode.
- **A question:** change the issue so the agent need not ask; it cannot be
  answered yet.
- `resume` starts a fresh agent session with the same prompt, so it may
  ask again.

### The configuration a run was recorded under

When `run` creates a REMOTE run it records the configuration the run is on.
That covers every profile it routes to (the six required ones and
`update_epic`): the provider, the model, the effort, and the idle and
maximum-runtime limits the agent runs under (the profile's own, else the
`execution` default). It also covers the resolved agent worktree root:
`execution.worktree_dir`, or `<git common dir>/autoforge/worktrees`.

The state file holds the record only within bounds it checks on every
load: a provider, model or effort of at most 512 characters, limits that
are positive integers, and paths of at most 4096 characters. A
configuration outside them is refused, naming the key, before anything is
written. `run` exits 2 before it creates the run, and `run --dry-run` and
`doctor` already refuse a profile outside them, `update_epic` included
(otherwise checked only when the run reaches it). `step`, `resume` and
`unblock` likewise refuse to record such a configuration, or to accept it
with `--accept-config-change`.

Before any phase executes, `step`, `resume` and `unblock` (from `BLOCKED`)
compare this command's configuration with the recorded one. On any
difference the command exits 2. It has launched nothing and written nothing
to the state, the run log or GitHub, and it says what changed and how to go
on:

```text
autoforge: error: run af-20261006T101500Z-3f2a9c was recorded under --config /home/me/code/AutoForge/autoforge.yaml (created 2026-10-06T10:15:00.512837+00:00), and this command's configuration differs from it, so 'resume' refuses before any phase executes; nothing was written. Changed (recorded -> this command):
  profiles.analyze_execute.model: "opus" -> "fable"
  agent worktree root (execution.worktree_dir): "/home/me/code/.worktree" -> "/home/me/code/AutoForge/.git/autoforge/worktrees"
To continue under the recorded configuration: autoforge --config /home/me/code/AutoForge/autoforge.yaml --state-dir /home/me/code/AutoForge/.autoforge/AutoForge-issue-159 resume
To continue under this command's configuration instead, repeat it with --accept-config-change: the change is written to the run log and becomes the run's configuration.
```

- **To keep the run's configuration**, run the command the message prints.
  It names the recorded `--config` file and the state directory that found
  the run, both by their absolute paths. The state directory is named even
  when this command took it from its configuration's `state_dir` rather
  than from `--state-dir`: the recorded configuration may name another one.
  A run recorded from the built-in defaults gets no `--config`. Sometimes
  this command already loads the recorded file.
  Then the message says that the file was edited since, or that the
  command runs from another directory or repository than the run's. A
  relative `execution.worktree_dir` and the default worktree location are
  resolved from where the command runs. Undo the edit, or run the command
  from the run's directory.
- **To change it deliberately**, repeat the command with
  `--accept-config-change`. Typical reasons are a limit raised after a
  timeout ([Agent limits](#agent-limits)) or another model for the
  remaining reviews. The change is written to the run log first: a
  `<PHASE>-config-change` record in `logs/<run-id>/events.jsonl` with each
  changed key, the recorded file and this one. Only then does this
  configuration become the run's, and later commands are checked against
  it. An `unblock` that goes on to refuse keeps the accepted configuration.
  A new worktree root moves the issue's agent to another, empty worktree.
  The old worktree keeps whatever the agent left there.

Everything else in the configuration may change between commands without a
refusal: the loop bounds and step budget, the merge gate, `env_allowlist`,
the `sandbox` section, and a profile's `command`, `extra_args` and `options`.
The one exception is a weaker `sandbox.mode`: every run, LOCAL included, is
pinned to the mode it was created under, and the same commands refuse a
weaker one, with the same `--accept-config-change`
([Outer sandbox](#outer-sandbox)); a refusal that has both lists both. The
record holds none of them, and no value that could carry a secret. The
validation evidence does depend on some of them: `validation.commands`,
the allow-list, `execution.command_timeout_seconds` and the `sandbox`
section are what candidate validation runs under, so editing one after a
clean review is not refused here but makes the merge gate refuse
([Candidate validation evidence](#candidate-validation-evidence)).

`--dry-run` makes the same comparison and writes nothing. It refuses as the
command would, or, with `--accept-config-change`, says what it would accept.
Outside a git checkout it cannot resolve the default worktree root, so it
compares the routing only and says so.

`autoforge status` shows the configuration the run is on, for example
`Config:     --config /home/me/code/AutoForge/autoforge.yaml (created 2026-10-06T10:15:00.512837+00:00)`.
A state file written before this check (protocol 8) has no recorded
configuration. It loads and resumes unchecked, and the first `step`,
`resume` or `unblock` records the configuration it runs on (`upgraded`). A
LOCAL run is not checked this way: its
[run contract](local-mode.md#the-run-contract) defines it.

### Candidate validation evidence

`validation.commands` ([Configuration](configuration.md#candidate-validation))
are run by the controller against one exact commit, each in a fresh export
of its tree, and each run leaves a record in the state file
([State and recovery](agent-guides/state-and-recovery.md#validation-evidence)).
They gate every REMOTE candidate (#173; [Workflow](agent-guides/workflow.md#candidate-validation-gates)):

- `ANALYZE_EXECUTE` and `FIX` validate the exact commit the agent hands
  back before anything is pushed or published. A failure publishes nothing:
  the step reports it (`... did not pass ... This is failure 1 of 3`), and
  the next step (of the same `run` or `resume`) launches the agent again
  with the failure in its prompt. The third failure of the phase enters `BLOCKED`, naming the
  commit, the definition index and the record. A `REPLAN_REEXECUTE`
  replacement that fails is rejected, as a replacement whose own tests
  failed is.
- `REVIEW` requires evidence for the PR head before the reviewer launches,
  reusing the producer's records or validating a head someone else pushed,
  and shows the reviewer the outcomes. A reviewer cannot return a clean
  result over a failing record: it is asked to correct it, and at the
  correction bound the step fails with nothing posted.
- A record is reused, never run again, for the same commit, definition and
  policy: a `PASS`, and a `FAIL` or `TIMEOUT` too. When the failure was the
  environment's rather than the change's (a missing tool, a full disk),
  repair it and run `autoforge unblock`: an applied unblock sets the
  validation override watermark (`validation_override_seq`, shown in its
  message, its run-log record, the dry-run plan and `status`) to the
  newest record, so every failure recorded so far is historical and the
  next gate runs the same commit again. Nothing else sets it.

What you can inspect:

- `autoforge doctor` lists the definitions, redacted, and the sandbox mode
  their records are made under (`candidate validation (validation.commands)`,
  informational), and runs none of them;
- the dry-run plan lists the set's digest, policy P and each definition,
  and says that under `sandbox.mode: disabled` every validator runs
  unsandboxed as your UID; it runs none of them;
- `autoforge status` shows a `Validation evidence:` block: the policy line,
  a pending attempt marker if a crash left one, the override watermark, the
  current phase's validation failure count, the round the REVIEW binding is
  for, and the records of the current candidate
  (`#<seq> [<index>] '<argv>': <outcome> (...)`, with the output tail of a
  `FAIL` or `TIMEOUT`). A pending marker is reported,
  never converted: the next `step` or `resume` records it `INTERRUPTED`
  and runs the definition again, and the third interruption of one
  definition blocks the run. A run that reviewed a candidate clean adds
  the review certificate the controller issued for that round (#174): its
  kind, candidate, round, set and policy digests and issue time, whether
  it applies to the state and configuration now (`yes`, or `no:` and the
  first mismatch), and the note that it is not a merge permission. `status
  --json` carries the same as `review_certificate_status` (`null` when
  none is held), beside the raw `review_certificate`.

A review certificate is the controller's own record of one clean review of
one exact validated candidate (ADR 0006 D1.2): it is not a GitHub approval,
a CI result or permission to merge, and it is never shown to an agent or
published. `REVIEW -> READY_FOR_MERGE` (and LOCAL `REVIEW -> DONE`) is
entered only in the write that issues it; a clean round whose certificate
cannot be issued exactly as recorded blocks instead. If the validation set
or policy changed after the reviewer was presented its evidence, the
certificate is still issued from what was presented and the run blocks
naming the change: restore the configuration and `unblock` (the certificate
then applies and `READY_FOR_MERGE` is re-entered), or `unblock` under the
new configuration, which decides a fresh `REVIEW` round validated under it.

The merge gate requires the certificate too (#175): every pass, in
`READY_FOR_MERGE` and again in `MERGE`, checks that it applies to the PR,
HEAD, base and merge base GitHub returns and to the validation set and
policy in force, before the review comment is re-read or any
`merge.verification_commands` command runs. Every other check is kept, and
`merge.verification_commands` still runs (and is cached) as before. A
refusal is `BLOCKED` with nothing merged or counted:

- **None held** ("no applicable ReviewCertificate for HEAD <sha>; unblock
  to run a new validated REVIEW round"): a state from before certificates,
  or one that reached the gate without the clean round that issues one.
  `autoforge unblock` decides `REVIEW`; the new round validates the head and
  its clean result issues the certificate the gate needs.
- **The validation set or policy changed since the clean review**: editing
  `validation.commands`, the timeout, the environment allow-list or the
  sandbox mode (or, with the sandbox on, `sandbox.validation_network` or
  `sandbox.read_only_paths`) changes the set or policy digest the
  certificate states, and the reason names which.
  Restore the configuration and `unblock`: the certificate applies again,
  `READY_FOR_MERGE` is re-entered and the next `resume --allow-merge`
  re-verifies the PR on GitHub. Or keep the new configuration and `unblock`:
  it decides `REVIEW`, a fresh round validated under it. Nothing reruns the
  review by itself.
- **Any other mismatch** (a hand-edited state, a certificate for another
  PR, base, merge base, comment or round) names what failed and is never
  coerced; `unblock` decides `REVIEW`.

A push to the PR, a retarget or a rewritten base still goes back to
`REVIEW` before the certificate is consulted, and the new round certifies
the new revision; a base that only advanced keeps it. A PR already merged
on GitHub is counted once without one, as before.

LOCAL runs show the same block for their `local.validation_commands`
records ([Local mode](local-mode.md#validation-commands)).

## Logging and redaction

Every agent invocation is logged under `<state dir>/logs/<run-id>/`. Logs
and CLI output pass through baseline secret redaction (`GITHUB_TOKEN`,
`GH_TOKEN`, `*_API_KEY`, `Authorization: Bearer` / `Basic`, `ghp_*`,
`github_pat_*`, `sk-*`, Google API keys `AIza...`, JWTs, the
`user:password@` part of a URL, …),
state-derived output included: `status` and `status --json` redact what they
print (a `block_reason` echoes agent text; a journal can be hand-edited)
without rewriting `state.json`. No environment dump is ever written.
Baseline only, with no claim of completeness. See
[Secrets and logging](agent-guides/secrets-and-logging.md).
