# Deterministic recovery MVP: evaluation and go/no-go (#375)

- **Status:** decided. The maintainer recorded a go on #375
  (https://github.com/iceboundrock/AutoForge/issues/375#issuecomment-6988458600),
  approved the roll-forward and the rollback of §8, accepted the remaining
  risks §8 lists, and signed off for operators (§8). They read this record
  at the head of #375's pull request, `ddff447a`. The record was written at
  `main` `d6283cc` (2026-11-18), where every reference below was read. §4.1
  holds two corrections to the package's advice that review round 2 of the
  pull request found after the decision.
- **Evaluates:** EPIC #354's deterministic MVP as `main` holds it, with this
  pull request's correction of the package's `claim-ambiguous` advice (§4,
  `ambiguous-prs`, and §4.1). That MVP is ADR 0011's typed stop
  classification and `stop:` lines (#369, `84df820`), the saved stop code,
  the recovery package and `status --live` (#374, `4a95c6d`), the bounded
  automatic relaunch of ADR 0012 D11 (#434, `143ac17`) and the acceptance of
  a delivered result of ADR 0012 D12 (#479, `32f5317`).
- **Inputs:** #375's body and its comments, which the controller handed
  to the agent that wrote this record, and the repository at `d6283cc`.
  That agent held no GitHub credential and read nothing else of GitHub. It
  did not read #354's review report, the discussions of #370 to #374, or any
  operator's run history. What this record says of those comes from the
  ADRs and the commit messages in the repository. #375's second comment is
  the maintainer's decision, which §8 cites as it reads.
- **Measured here:** an offline matrix of 17 cases on the real engine
  (§3 to §5). `tests/test_recovery_evaluation.py` computes every row, and
  fails when this document says otherwise.
- **Not measured here:** the review report's corpus (51 runs and 15
  unblocks, as the report gives them), human diagnosis time, and stops from
  outside AutoForge (§7).

## 1. The question

#375 asks whether the controller, with #369's typed stop reasons and #374's
deterministic package, gives an operator at a stop what they need to act:
the right cause, the right next command, nothing unsafe, and nothing lost.
It must do so better than the original status and error text did, and
without changing a controller decision. If it does, #354 can close its
deterministic MVP whatever #370's advisor gates decided.

The measure is the handoff, not the rate of automatic unblocks. Two cases
of the sample resolve without an operator: a stop that no longer happens
(#327's fix), and #434's one relaunch of an idle-timed-out agent. #479's
acceptance of a result delivered before a failed launch end is a third
such path, which needs a provider's typed proof and is not sampled here.
Every other stop needs a human decision by design (ADR 0012 D3). A
measure that counted only automatic recoveries would score the MVP on
exactly the cases it chose not to automate.

## 2. The two arms

Both arms are read on the same stop.

- **Before** is what an operator had without #369 and #374: the phase a
  later `status` printed, the text of the saved `block_reason`, and the
  error the failing command printed. It is read on today's texts, which
  carry later corrections: #369 rewrote the seven `BLOCKED` reasons that
  told the operator to `resume` (ADR 0011 §1), and later changes reworded
  others. So this arm is an upper bound on what an operator had before
  #369. The historical texts were not measured. The K9, K10 and K12 reasons
  are not among those corrections: each has spelled its commands since the
  change that introduced its wait (#211, #243 and #423), and a test holds
  the K10 one
  (`tests/test_recovery_package.py::test_a_k10_stops_reason_names_the_sequence_unblock_accepts`).
- **After** is the before arm plus the `stop:` line the stopping command
  prints (ADR 0011) and the recovery package of a later `status --json`
  (#374).

## 3. The cases and their gold resolutions

### 3.1 The sample

#375's strata, and the cases that stand for each:

| Stratum (#375) | Cases |
|---|---|
| #291 regression | `checks-refused-291` |
| #327 regression | `no-stop-327`; `false-conflict-314`, the stop a controller before #327 saved |
| K9, K10 and K12, an operator's coordination | `k9-acceptance-action`, `k10-operator-actions`, `k12-pr-body-update`, `ambiguous-prs` |
| crash recovery, an effect whose outcome is unknown | `power-loss-after-push`, `github-unavailable` |
| LOCAL blocked | `local-blocked` |
| FAILED | `agent-failure` |
| unchanged-phase errors and timeouts | `idle-timeout-twice`, `github-unavailable`, `idle-timeout-relaunched` |
| the bounds | `review-round-cap`, `step-budget` |
| a stop saved without a code | `no-saved-code` |
| an agent that asks a human | `agent-blocked` |
| stops from outside AutoForge | none: none was available |
| the review report's runs | not read (§7) |

Each case is a scenario the suite already holds where one exists (the
fixtures of ADR 0011 D8 and #374's scenarios), driven through the real
engine on `FakeGitHub` and a bare origin. The cases were chosen to cover
every stratum #375 names that can be built offline. They were not drawn
from a population, so the matrix says how the package behaves on each kind
of stop, not how often each kind occurs.

### 3.2 The gold resolutions

Each case's gold resolution is written by hand in the harness, from ADR
0012 D2 (who leaves each outcome, and how) and D3 (which authority each
action needs), never from the package. It is the operator's act outside
AutoForge, the command that leaves the stop, the phase the controller must
read back after that command, and what the next workflow step must save:
after an `unblock`, the `resume --max-steps 1` that follows it; a `resume`
or a `run` is that step itself. The harness performs it, on a fresh copy of
the case when the package's steps did not resolve the stop, and fails if a
command exits with anything but 0, if the saved state shows another phase
after the command, or if the step after it saves anything but what the
gold names (the **Gold read-back** column). Every gold label in §4 passed
that check, so each is held by the controller's own read-back. Nothing an
agent or a model said counts as an outcome.

Two cases are resolved by the controller with no operator command; their
gold resolution reads "none: the controller recovers". The other fifteen
need one. The package is guidance only and performs nothing, so none of
those fifteen counts as an automated recovery.

### 3.3 The columns

- **Gold stop:** what ADR 0011 says the stopping command's `stop:` line
  should name (`outcome/code`). `none` means nothing stopped, and `—` means
  a process that died and printed nothing.
- **Before: a stop:** a later `status` shows that the run stopped:
  `BLOCKED`, `FAILED` or a refusal. A resumable error and a crash leave a
  phase that looks like one in progress.
- **Before: command:** the saved reason, or the error the failing command
  printed, spells the gold command whole: every word in order, except the
  values the operator supplies (a reason, a URL). This is a text match,
  made by the evaluation only. Nothing that decides anything reads a
  reason's text.
- **After: stop line:** what the stopping command's `stop:` line names.
- **After: later status:** `recovery.outcome/code` of a later
  `status --json`, or the refusal it exits with.
- **After: command:** a step of the package spells the gold command whole.
  For a refusal, which gets no package, its error text is read instead.
- **Gold read-back:** what the controller must save after the gold
  command, and, after an `unblock`, after the `resume` that follows it. For
  a case the controller resolves itself, the phase its own steps saved.
- **After: unsafe:** displayed steps that ADR 0012 D2 denies for the stop,
  or that D3 grants nobody, and steps without a command whose act, as the
  text reads, breaks what the gold keeps. The commands counted are: a
  `resume` or `step` before the run has left `BLOCKED`; an `unblock` of a
  run that is not `BLOCKED`; an approval of an action that awaits none; a
  decline or bind of an action the plan does not hold; an open merge gate;
  an accepted configuration change; and any command that is neither the
  controller's nor the documented `gh` pipe of an operator action. An
  alternative step stands in place of the steps before it, so it is checked
  against the run as it stopped. A step without a command is performed as
  it reads (below), and it is unsafe when it closes or unmarks a PR the gold
  keeps open and marked: the PR the operator chose, the PR the run is bound
  to, or one whose marker names the run.
- **After: steps resolve:** the package's steps are run in order as an
  operator would type them, without the alternatives. An alternative whose
  text says when it is taken in place of the plain `unblock` is taken when
  that holds as GitHub then reads (`CHOICES`). The one such text is the
  take-over of a marked PR kept from two, "where you kept a marked PR headed
  in this repository". Placeholders are
  filled (a reason with text, `<PR URL>` with the PR the operator chose),
  and `doctor` is skipped, since it checks the host and a case does not
  stand in for one. The column is `yes` when every command the steps ran
  exited 0, the gold command was among them and saved the gold phase, and
  the step after it, the first `resume` or `run` the steps show, ran with
  `--max-steps 1`, exited 0 and saved what the **Gold read-back** names. A
  package that shows no such step, or a step that fails with the phase
  unchanged, is `no`
  (`tests/test_recovery_evaluation.py::test_a_package_that_omits_the_next_step_does_not_resolve`,
  `tests/test_recovery_evaluation.py::test_a_resume_that_fails_with_the_phase_unchanged_does_not_resolve`).
- **After: preview:** before the steps run a plain `unblock`, the decision
  `status --live` reports is the one that `unblock` then takes.
- **Plan kept:** for a stop with a saved plan, three checks. At the stop,
  the package names the journal and the push's commit as data not to touch,
  and that commit is the relevant candidate: the plan's only push, the
  commit the agent worktree holds, a descendant of the head the push's
  precondition read (the reviewed head for FIX, the default branch head for
  ANALYZE_EXECUTE) and of every fixed finding's commit, not yet on the
  PR's branch when the push was never sent, and on it when the push was
  observed. After the steps, while the run is still on the plan, every
  record of the stop is in the journal with the same identity, target and
  payload, at a stage no earlier than before. Once the phase went on, which
  drops its records (ADR 0004 D2.4), every record's effect is read where it
  was written: the origin's branch, the PR head GitHub reports and the
  state's head are all the candidate; exactly one open PR is the one the
  plan created and the run is bound to; exactly one comment carries each
  comment's payload; the PR description is the update's payload
  (`tests/test_recovery_evaluation.py::test_a_plan_whose_effect_is_lost_or_doubled_is_not_kept`,
  `tests/test_recovery_evaluation.py::test_a_pr_head_moved_off_the_candidate_is_not_kept`,
  `tests/test_recovery_evaluation.py::test_a_push_that_is_not_the_relevant_candidate_is_not_kept`).

A step without a command is the operator's own act, so the harness performs
it as its text reads, literally. Each text the package shows in the sample
has a reading written by hand in the harness (`READINGS`): reading or
inspecting changes nothing; "raise workflow.max_review_rounds" raises it, to
the value the case gives; "a credential that may read check runs" grants
that read; "repair or remove it if it is not the run's own" closes a marked
PR whose marker names another run or none. A text that goes by what the
reason names is read clause by clause, and each clause acts only on its own
kind of object. "Two or more PRs with the issue's marker ...: decide
whether one of them is the issue's" closes every marked PR but the one the
case has the operator choose, and only where two or more are marked. "A
marked PR headed in a fork or in another repository: close it" closes such
a PR. "Review comments of one round" deletes every round comment when the
case's operator found them all stray, and none when one is another
controller's run's. Deleting such a comment is unsafe, as closing a kept PR
is. The detail of a `status --live` preview is read the
same way, since the operator reads it there. A step that no reading covers
fails its case: the gold's act never stands in for a text
(`tests/test_recovery_evaluation.py::test_a_manual_step_with_no_reading_fails_the_case`).
An operator's act must leave the state directory unchanged.

`—` means the column does not apply to the case.

Every `status` the harness runs is also held to three things. It launches
no agent: the provider fails the case if it is called. It sends nothing to
GitHub: every write the fake can record is compared before and after. And
it leaves the state directory unchanged: the kind, inode, modification
time and bytes of every entry.

## 4. The matrix

| Case | Stratum | Gold stop | Gold resolution | Gold read-back | Before: a stop | Before: command | After: stop line | After: later status | After: command | After: unsafe | After: steps resolve | After: preview | Plan kept |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `k10-operator-actions` | K10 | blocked/operator-actions | post each payload on its issue, then `unblock --reason …` | FIX, then `resume`: REVIEW | yes | yes | blocked/operator-actions | blocked/operator-actions | yes | 0 | yes | yes | yes |
| `k9-acceptance-action` | K9 | blocked/acceptance-action | `unblock --approve-action <id> --reason …` | FIX, then `resume`: REVIEW | yes | yes | blocked/acceptance-action | blocked/acceptance-action | yes | 0 | yes | — | yes |
| `k12-pr-body-update` | K12 | blocked/acceptance-action | `unblock --approve-action <id> --reason …` | FIX, then `resume`: REVIEW | yes | yes | blocked/acceptance-action | blocked/acceptance-action | yes | 0 | yes | — | yes |
| `checks-refused-291` | #291 | blocked/github-checks-refused | a credential that may read check runs, then `unblock --reason …` | READY_FOR_MERGE, then `resume`: READY_FOR_MERGE | yes | no | blocked/github-checks-refused | blocked/github-checks-refused | yes | 0 | yes | yes | — |
| `no-stop-327` | #327 | none | none: the controller recovers | REVIEW | — | — | — | — | — | — | yes | — | — |
| `false-conflict-314` | #327 | blocked/effect-conflict | `unblock --reason …` | ANALYZE_EXECUTE, then `resume`: REVIEW | yes | no | blocked/effect-conflict | blocked/effect-conflict | yes | 0 | yes | yes | yes |
| `ambiguous-prs` | coordination | blocked/claim-ambiguous | keep the issue's PR, close the other, then `unblock --take-over <PR kept> --reason …` | ANALYZE_EXECUTE, then `resume`: REVIEW | yes | no | blocked/claim-ambiguous | blocked/claim-ambiguous | yes | 0 | yes | — | — |
| `review-round-cap` | bounds | blocked/review-round-cap | raise workflow.max_review_rounds, then `unblock --reason …` | FIX, then `resume`: REVIEW | yes | no | blocked/review-round-cap | blocked/review-round-cap | yes | 0 | yes | yes | — |
| `step-budget` | bounds | blocked/step-budget | raise workflow.max_total_steps, then `unblock --reason …` | REVIEW, then `resume`: FIX | yes | no | blocked/step-budget | blocked/step-budget | yes | 0 | yes | yes | — |
| `no-saved-code` | no code | blocked/review-round-cap | raise workflow.max_review_rounds, then `unblock --reason …` | FIX, then `resume`: REVIEW | yes | no | blocked/review-round-cap | blocked/unknown | yes | 0 | yes | yes | — |
| `agent-blocked` | agent | blocked/agent-blocked | answer the agent's question, then `unblock --reason …` | ANALYZE_EXECUTE, then `resume`: REVIEW | yes | no | blocked/agent-blocked | blocked/agent-blocked | yes | 0 | yes | yes | — |
| `agent-failure` | FAILED | failed/agent-failure | `run --force --epic <EPIC> --issue <issue>` | ANALYZE_EXECUTE | yes | no | failed/agent-failure | failed/agent-failure | yes | 0 | yes | — | — |
| `idle-timeout-relaunched` | timeouts | none | none: the controller recovers | REVIEW | — | — | — | — | — | — | yes | — | — |
| `idle-timeout-twice` | timeouts | resumable_error/execution-timeout | `resume` | REVIEW | no | yes | resumable_error/execution-timeout | in_progress/in-progress | yes | 0 | yes | — | — |
| `github-unavailable` | unknown effect | resumable_error/github-unavailable | `resume` | REVIEW | no | yes | resumable_error/github-unavailable | in_progress/in-progress | yes | 0 | yes | — | yes |
| `power-loss-after-push` | crash | — | `resume` | REVIEW | no | no | — | in_progress/in-progress | yes | 0 | yes | — | yes |
| `local-blocked` | LOCAL | refused/state-invalid | `run --force --epic <EPIC> --issue <issue>` | ANALYZE_EXECUTE | yes | no | refused/state-invalid | refused (exit 2) | no | — | — | — | — |

What the rows that differ show:

- **`ambiguous-prs`** is the row this pull request changed, and the one
  where the first version of this record was wrong. Two open PRs carry the
  issue's marker and the run recorded neither. #374's package said, as its
  first step, to "close, delete or unmark every one that is not the run's
  own, then 'unblock'". Neither PR is the run's own, so that text, read as
  it stands, closes both, the PR the issue needs among them. The first
  harness did not read it: it performed the gold's act (close the stale PR)
  at the first step without a command, and so counted no unsafe step. Read
  as it stands, that step is unsafe
  (`tests/test_recovery_evaluation.py::test_a_manual_step_that_closes_the_pr_the_gold_keeps_is_unsafe`),
  and even with only the stale PR closed, the plain `unblock` it named
  refused the one left: a marked PR this run has no record of is never
  bound by finding it (ADR 0004 D9.4a). So the package now tells a run that
  recorded no PR to go by what the reason names. For two marked PRs, that
  is: decide which PR is the issue's, keep that one open with its marker,
  close or unmark each other one and never the one kept, then take it over
  with `unblock --take-over <PR URL>`. The take-over is shown as the
  alternative to the plain `unblock` "where you kept a marked PR headed in
  this repository" (§4.1 says why). It names no PR: the choice stays the
  operator's (K11 under #414), and `unblock` refuses the take-over while a
  second PR still carries the marker. Followed as typed, the steps now keep
  the PR the operator chose, take it over in place of the plain `unblock`,
  and the `resume` after it reaches `REVIEW` on it. The take-over has no
  live preview, which covers a plain `unblock` only, so the row's preview is
  `—`.
- **`no-saved-code`** is the review cap saved with no code, as every stop
  saved before protocol 16 loads. The later `status` says `unknown`
  explicitly and never derives a code from the reason's text. Its own step
  only says to read the reason and the run log. It still offers the live
  preview and the plain `unblock`, and the preview's detail tells the
  operator to raise `workflow.max_review_rounds`; performed as it reads, the
  steps resolve it. The first harness raised the bound at the step that
  says to read the reason, which no text there asked for.
- **`idle-timeout-twice`**, **`github-unavailable`** and
  **`power-loss-after-push`** never entered `BLOCKED` or `FAILED`, so no code
  is saved for them (ADR 0011 D5, as #374 amended it, saves one only with
  those phases). A later `status` names `in_progress` and the right route,
  `resume`, and not why the command failed. The failing command's own
  `stop:` line and error name the cause. A power loss prints nothing at
  all, which is why the before arm has nothing for it.
- **`local-blocked`** gets no package. Every command refuses the state
  (exit 2), and `status` prints only the loader's refusal. A command that
  ends in the refusal, `resume` for instance, prints the `stop:` line,
  `refused/state-invalid` with "a new run" among its routes. It does not
  spell `run --force` with the run's EPIC and issue. Nothing offers an
  `unblock` (`tests/test_stop_classification.py::test_a_local_state_is_refused_never_given_an_unblock`).
- **`k9-acceptance-action`**, **`k10-operator-actions`** and
  **`k12-pr-body-update`** are the cases where the before arm already
  spells the command: those reasons have done so since their waits were
  introduced. What the package adds there is structure: the pending action,
  its exact id and target, its payload's digest, and the approvals it
  forbids.

### 4.1 Two more `claim-ambiguous` stops, outside the matrix

Review round 2 of #375's pull request found two more stops whose
`claim-ambiguous` advice was wrong once the `ambiguous-prs` correction
above was made. Both were found after the maintainer's decision (§8). They
are corrected, and each is followed as the matrix's cases are, by a test
outside the matrix. The matrix's rows and §5's counts are unchanged by
them.

- **A single marked PR headed in a fork.** ANALYZE_EXECUTE's probe of the
  issue's PRs names `claim-ambiguous` for every PR it cannot work on, not
  only for two candidates. A marked PR headed in a fork is never adopted
  (ADR 0004 D9.4), and its reason says to close it or remove its marker,
  then `unblock`. The corrected advice told the operator to keep "that PR"
  and take it over, which `unblock` refuses for a fork's PR. Nothing typed says
  which of the probe's causes stopped the run, and a code is never read
  from a reason's text. So the advice now goes by what the reason names:
  two marked PRs as above; a PR headed in a fork or in another repository
  closed or unmarked; a failed read fixed. The step in sequence is the plain
  `unblock`, right for every cause once that is done. The take-over is the
  alternative taken in its place where a marked PR headed in the
  repository was kept. Followed as typed, the steps close the fork's PR,
  skip the take-over, and the plain `unblock` and the `resume` after it reach
  `REVIEW` on a PR the run opens itself
  (`tests/test_recovery_evaluation.py::test_a_fork_headed_pr_followed_as_displayed_is_closed_and_the_plain_unblock_resolves`,
  `tests/test_recovery_package.py::test_a_fork_headed_pr_is_closed_or_unmarked_never_kept_or_taken_over`).
  The same displayed steps still take the kept PR over in `ambiguous-prs`
  (`tests/test_recovery_evaluation.py::test_the_pr_kept_of_two_is_taken_over_in_place_of_the_plain_unblock`).
- **Two review comments of one round that the run did not journal.** With
  a PR bound, the corrected advice said to keep the run's own object, or
  else the one the operator chooses. The controller never adopts a round
  comment it did not journal (ADR 0004 D9.6), so after the one kept and an
  `unblock`, the next `REVIEW` step stopped again with `unjournaled-write`.
  The advice now goes by the kind of object, and for round comments says
  what REVIEW's reason says. If every comment is stray or foreign, delete
  each, then `unblock`, and the reviewer runs again. If one is another
  controller's run's review, delete none and do not `unblock`: it is that
  run's record, and its FIX may be working from it. Followed as typed, with
  both comments stray, the steps delete both, and the `resume` after the
  `unblock` launches the reviewer, which posts the round's one comment and
  reaches `READY_FOR_MERGE`. With one comment another run's, the step
  deletes none, and a text that deleted it would be counted unsafe
  (`tests/test_recovery_evaluation.py::test_two_unjournaled_round_comments_followed_as_displayed_run_the_reviewer_again`,
  `tests/test_recovery_evaluation.py::test_another_runs_round_comment_is_never_deleted_by_the_displayed_steps`,
  `tests/test_recovery_package.py::test_two_unjournaled_round_comments_are_deleted_or_left_never_kept_one_of_two`).
  Two follow-up issues of one finding and two progress comments on the
  EPIC are told what their reasons say, to leave at most one, and remain
  unsampled.

## 5. What the matrix shows

`tests/test_recovery_evaluation.py` recomputes this table from §4.

| Measure | Before | After |
|---|---|---|
| A later `status` shows that the run stopped | 12 of 15 | 12 of 15 |
| The stopping command names the gold outcome and code | 0 of 14 | 14 of 14 |
| A later `status` names the gold code | 0 of 15 | 10 of 15 |
| A later `status` says the code is unknown | 0 of 15 | 1 of 15 |
| The text, or the package, spells the gold command | 5 of 15 | 14 of 15 |
| Unsafe displayed steps, those without a command performed as they read | not measured | 0 in 14 packages |
| The displayed steps, followed as typed, resolve the stop, and the next step succeeds | — | 14 of 14 |
| A plain `unblock`'s live preview was its decision | — | 7 of 7 |
| The saved plan was named, its push was the candidate, and every record held | — | 6 of 6 |
| Resolved by the controller with no operator command | 2 of 17 | 2 of 17 |

**Classification.** Every stop line names the gold outcome and code (14
of 14; the power loss prints no line). A later `status` names the gold
code for every `BLOCKED` or `FAILED` stop that saved one (10). It says
`unknown` for the one that saved none, rather than guessing. For the other
four it says what the state can prove: `in_progress` with the `resume`
route for the three that never stopped, and the loader's refusal for
LOCAL. No case was classified wrongly.

**Handoff.** The package spells the command that resolves the stop in 14
of 15 cases. The text an operator had before spells it in 5. The gains are
where the before arm had little or nothing: an agent's own stop, whose
reason is the agent's text; the bounds, identity and GitHub stops, whose
reasons say what to fix but not the command (several end in "then
'unblock'"); two marked PRs, whose reason says to close the stale one and
`unblock`, which then refuses; a FAILED run, whose route is a new run; and
a crash, which prints nothing. The one it misses is `local-blocked`, which
gets no package.
In all 14 cases with a package, following its steps as typed resolves the
stop: every command exits 0, the gold phase is saved, and the step after it
exits 0 and saves what the gold names. Before this pull request's
correction, `ambiguous-prs` did not: its steps closed the PR the operator
wanted, and its `unblock` was refused.

**Safety and cost.** No package displayed an unsafe step, counting the
steps without a command as performed. No `status` launched an agent, sent
anything to GitHub or changed the state directory, in any of the 15 stops.
The live preview matched the decision of every plain `unblock` that
followed it (7 of 7). Every saved plan was named as data not to touch, its
push was the candidate the worktree held, and every record held: in all six
the run went on, and each record's effect was read back exactly once from
GitHub and the origin, with the candidate, where the plan had one, at the
PR's head (6 of 6). The
package costs no model launch and no GitHub write. `status --live` reads
GitHub as `unblock --dry-run` does, and reports the decision it prints
(`tests/test_recovery_package.py::test_the_live_preview_is_the_decision_unblock_dry_run_prints`).
The CPU time of one `status --json`
over a case's state was 4 ms to 30 ms, median 16 ms, over the 15 stops, in
one serial run on an Intel Core i7-8700K under CPython 3.14.10 (recorded as
each case's `status_cpu_seconds` in `--junitxml`, with
`-o junit_family=xunit1`, since pytest's default family drops a test's
properties). That is a measurement of one host, and no test bounds it.

## 6. The safety regressions

#375 requires each of these to hold. Each one names the tests that hold it
on `main` or in this pull request, and every case of §4 adds its own checks:
no `status` writes, sends or launches anything, no displayed step is unsafe,
no operator's act writes the state, and no saved plan loses a record.
`tests/test_recovery_evaluation.py` fails if a test named here no longer
exists. Whether each one passes is the suite's business, since they all run
in it.

- **No unauthorized GitHub write, unblock or approval.** `status`,
  `status --live` and `unblock --dry-run` write nothing and take no lock,
  and nothing that decides imports the package or reads a route. An action
  awaiting the operator sends nothing until it is approved, and an operator
  action is never sent by the controller
  (`tests/test_recovery_package.py::test_status_live_writes_nothing_takes_no_lock_and_launches_nothing`,
  `tests/test_unblock.py::test_unblock_dry_run_reads_github_and_writes_nothing`,
  `tests/test_recovery_package.py::test_the_package_module_reads_nothing_and_decides_nothing`,
  `tests/test_stop_classification.py::test_only_the_sinks_and_the_cli_import_the_classification`,
  `tests/test_stop_classification.py::test_the_engine_only_names_codes_and_never_reads_a_route`,
  `tests/test_effects.py::test_an_acceptance_comment_awaiting_the_operator_is_held_with_nothing_sent`,
  `tests/test_operator_actions.py::test_operator_actions_block_with_the_plan_saved_and_nothing_sent`,
  `tests/test_recovery_package.py::test_the_package_never_carries_an_environment_credential`).
- **No lost unpushed commit or pending effect receipt.** A FIX candidate is
  named as data not to touch whether or not its plan was saved. A crash at
  each window of a plan completes it from the journal, with each effect
  written once. An audit that landed before a crash is adopted, never posted
  again
  (`tests/test_recovery_package.py::test_a_fix_that_stopped_before_its_plan_protects_the_committed_candidate`,
  `tests/test_recovery_package.py::test_a_saved_fix_plan_names_its_candidate_and_no_unknown_one`,
  `tests/test_recovery_package.py::test_an_agents_blocked_fix_offers_a_rereview_and_protects_its_unsaved_candidate`,
  `tests/test_engine.py::test_analyze_crash_after_the_plan_save_completes_from_the_journal`,
  `tests/test_engine.py::test_fix_crash_window_resumes_from_the_journal_and_writes_each_effect_once`,
  `tests/test_auto_recovery.py::test_an_audit_that_landed_before_a_crash_is_adopted_never_posted_twice`,
  `tests/test_auto_recovery.py::test_a_crash_after_the_decision_spends_the_allowance`).
- **No bypass of exact-candidate validation, the review certificate, CI, or
  the merge and ownership locks.** A commit that is not in the store is
  refused before any launch, and a validator's crash is never a pass. An
  `unblock` decides `READY_FOR_MERGE` only with an applicable certificate.
  Refused checks are never a pass. The merge needs both gates, and no worker
  merges. A PR another run owns blocks before any launch, and a refused
  take-over writes nothing. A state is read again under the lock, a
  scheduler's issue is refused to a standalone command with nothing
  written, and a dry run never takes the lock
  (`tests/test_validation_evidence.py::test_a_commit_not_in_the_store_is_refused_before_any_launch`,
  `tests/test_validation_evidence.py::test_a_crash_after_the_validator_exited_is_never_a_pass`,
  `tests/test_review_certificate.py::test_unblock_of_an_open_pr_without_an_applicable_certificate_decides_review`,
  `tests/test_review_certificate.py::test_an_edited_certificate_is_refused_at_load_never_reissued`,
  `tests/test_stop_classification.py::test_fixture_checks_refused_291_is_typed_never_a_pass`,
  `tests/test_recovery_package.py::test_refused_checks_291_are_previewed_and_never_claimed_passing`,
  `tests/test_engine.py::test_merge_phase_requires_both_gates`,
  `tests/test_epic_commands.py::test_with_the_merge_gate_open_no_worker_merges_or_writes_the_epic`,
  `tests/test_engine.py::test_a_fresh_run_blocks_on_a_pr_another_run_owns_and_names_the_owner`,
  `tests/test_unblock.py::test_unblock_take_over_is_refused_with_nothing_written`,
  `tests/test_cli.py::test_a_state_that_loads_before_the_lock_is_read_again_under_it`,
  `tests/test_ownership.py::test_refuse_names_the_scheduler_commands_and_writes_nothing`,
  `tests/test_engine_locking.py::test_dry_run_never_takes_the_lock`).
- **No altered default controller decision.** `unblock` takes no
  classification. A route follows the outcome, never the code. The saved
  code is written and cleared only by the sinks and `unblock`. The
  automatic recovery rule reads no classification, and relaunches only its
  allowlist. No recovery reads the run log
  (`tests/test_stop_classification.py::test_unblock_takes_no_classification`,
  `tests/test_stop_classification.py::test_routes_follow_the_outcome_never_the_code`,
  `tests/test_stop_classification.py::test_the_saved_stop_code_is_written_and_cleared_only_by_the_sinks_and_unblock`,
  `tests/test_auto_recovery.py::test_the_rule_reads_no_stop_classification`,
  `tests/test_auto_recovery.py::test_only_the_allowlist_is_ever_relaunched`,
  `tests/test_runlog.py::test_runlog_recovers_sequence_from_step_names_never_from_the_journal`).
- **Unknown and ambiguous stay explicit.** No code is ever read from a
  reason's text, and a saved code classifies only as itself. Two candidate
  PRs are never chosen between: the package names neither, tells the
  operator to keep the one they choose, and its take-over names no PR. A
  PR the controller never adopts is not offered as one to keep and take
  over, and two round comments the run did not journal are never kept one
  of two (§4.1). An
  inconclusive read refuses `unblock`, and a transient one writes nothing.
  An unknown view gets only the routes both views share. A result delivered
  before a kill is never discarded
  (`tests/test_stop_classification.py::test_a_persisted_reason_is_never_read_for_a_code`,
  `tests/test_stop_classification.py::test_a_saved_code_classifies_only_as_the_code_it_is`,
  `tests/test_recovery_package.py::test_true_ambiguous_prs_are_never_chosen_between`,
  `tests/test_recovery_package.py::test_the_pr_kept_of_an_ambiguity_is_taken_over_as_the_package_says`,
  `tests/test_recovery_package.py::test_an_ambiguity_with_a_pr_bound_keeps_the_runs_own_and_unblocks_plainly`,
  `tests/test_recovery_package.py::test_a_fork_headed_pr_is_closed_or_unmarked_never_kept_or_taken_over`,
  `tests/test_recovery_package.py::test_two_unjournaled_round_comments_are_deleted_or_left_never_kept_one_of_two`,
  `tests/test_unblock.py::test_unblock_refuses_a_pr_that_cannot_be_read_conclusively`,
  `tests/test_unblock.py::test_unblock_propagates_a_transient_github_failure_and_writes_nothing`,
  `tests/test_stop_classification.py::test_an_unknown_view_gives_only_the_routes_both_views_share`,
  `tests/test_auto_recovery.py::test_a_result_delivered_before_the_kill_is_never_discarded`).
- **LOCAL gains no `unblock` edge.**
  (`tests/test_stop_classification.py::test_a_local_state_is_refused_never_given_an_unblock`,
  `tests/test_recovery_package.py::test_a_local_blocked_state_gets_no_package_and_no_unblock`).
- **Crash and JSON backward compatibility.** A protocol-15 file loads in
  every phase with no stop code, and is relabelled. A stop saved without a
  code is diagnosed `unknown`, and says why. A newer label is refused before
  its fields are read. The `recovery` keys of `status --json` are stable,
  and two `status` runs over one state print the same
  (`tests/test_state.py::test_a_protocol_15_file_loads_in_every_phase_with_no_stop_code_and_is_relabelled`,
  `tests/test_recovery_package.py::test_a_stop_saved_with_no_code_is_diagnosed_unknown_and_says_why`,
  `tests/test_protocol_compat.py::test_a_newer_label_is_refused_before_its_phase_and_fields_are_read`,
  `tests/test_recovery_package.py::test_the_json_keys_are_stable`,
  `tests/test_recovery_package.py::test_two_status_runs_over_one_state_print_the_same`).

This change makes no controller decision differently. Its one change
outside the evaluation is the advice of the package for `claim-ambiguous`:
its two diagnoses, which now go by what the reason names, and, with no PR
bound, a take-over offered as the alternative to the plain `unblock`.
Nothing that decides reads the package
(`tests/test_recovery_package.py::test_the_package_module_reads_nothing_and_decides_nothing`),
and `unblock --take-over` re-checks everything itself. The change runs no
real model, changes no CI threshold, runs no `autoforge epic run`, and
changes no default.

## 7. What this cannot say

- **The matrix is internal consistency, not effectiveness for a person.**
  The cases are the suite's own scenarios, chosen by the author to cover
  #375's strata. The package's texts were written, and reviewed, against
  many of these same scenarios. So a row shows that the package agrees with
  the controller's own decisions on that kind of stop. It does not show
  that an operator reads it correctly, or how often each kind occurs. Each
  case is one deterministic run, so there is nothing to put a confidence
  interval on.
- **Human diagnosis time and steps were not measured.** No person took
  part. The displayed steps a case runs are commands, not minutes. A
  comparison of time needs operators, a labelled set of stops and an
  approved protocol.
- **The review report's corpus was not read.** #354's review reports 51
  local dogfooding runs and 15 applied unblocks, which this record has not
  verified (ADR 0011 D7.5). The agent could not reach any operator's state
  directories. When a maintainer runs it, this is the protocol, fixed here
  before anyone has seen the result:
  1. Run `uv run python -m tests.stop_corpus --json PATH...` over the state
     directories, and post its summary on #375 with the limitations it
     prints.
  2. For each run that stopped, a reviewer other than its operator labels
     the action that actually resolved it, from GitHub and the run log,
     before looking at the package. The label also says whether that
     action was the controller's or the operator's.
  3. Only for a run saved under protocol 16 or later, read the package with
     `status --json` against a copy of its state directory, and score the
     columns of §3.3 that history allows. The `stop:` line was never saved.
     A run saved before protocol 16 classifies `unknown` by design.
  4. Report every run that could not be scored, and why, beside the scored
     ones. LOCAL runs are `undecidable` (ADR 0011 D7.5).

  Such a corpus can show coverage and gaps on real stops. It cannot show
  diagnosis time either.
- **A step without a command is read by the evaluator.** What a text tells
  an operator to do is a reading written by hand, one per text
  (`READINGS`), not something the harness can derive. It is checked by
  performing it and by what it breaks, but a reading that is kinder than
  the text would hide an unsafe step, so the readings deserve a reviewer's
  eye. The first version of this record shows the cost: it performed the
  gold's act in place of the text and missed that `ambiguous-prs` closed
  both PRs. The same holds for the reading that takes an alternative
  (`CHOICES`).
- **No stops from outside AutoForge were available.**
- **The before arm is an upper bound** (§2): it reads today's reasons,
  which #369 and later changes corrected.

## 8. Gates D and A, and the go/no-go

**Gate D, an advisor's shadow diagnosis: no-go,** as ADR 0012 D4 decided.
The maintainer accepted that record on 2026-11-03, on #370. This
evaluation does not reopen it. D4 item 4 measures an advisor against the
deterministic package. This record measures that package, and finds two
gaps in the matrix (`ambiguous-prs` and `local-blocked`) and two more
`claim-ambiguous` stops after it (§4.1). In each the controller already
decides the route without a model: the reason it saves or the refusal it
prints names it, as the package now does, and a LOCAL state's route is a
new run. Which PR is the issue's, and whose a comment is, is the operator's
decision, never a model's (ADR 0012 D3). None of these gaps is a diagnosis
a model would add.

**Gate A, autonomous recovery actions: no-go,** as ADR 0012 D5 decided,
except for the one class #434 approved (D11): one relaunch per step of an
allowlisted, typed launch failure. `idle-timeout-relaunched` is that class.
This evaluation does not reopen A, and names no new class.

**#371 to #373** are not needed for this decision. Nothing here depends on
them.

**Decision: go**, recorded by the maintainer on #375
(https://github.com/iceboundrock/AutoForge/issues/375#issuecomment-6988458600),
for EPIC #354's deterministic recovery MVP as `main` holds it, together
with this pull request's correction of the `claim-ambiguous` advice. The maintainer read
this record at the pull request's head `ddff447a` (§4, §5 and this
section), after review round 1's corrections. Without the correction the
decision would be a no-go: #375 requires zero unsafe suggested actions, and
the text #374's package gave two marked PRs that the run recorded neither
of, read as it stands, closes the PR the operator needs (§4). This record
recommended the same go; the decision is the maintainer's.

The decision relies on what §4 and §5 measure offline, on 17 cases
through the real engine. No case was classified wrongly, and a later
`status` says `unknown` for the stop that saved no code rather than
guessing. The package spells the resolving command in 14 of 15 stops,
against 5 for the text an operator had before. Counting the steps without
a command as performed, the packages display no unsafe step, and following
the displayed steps as typed resolves all 14 stops that get a package:
every command exits 0, and the next step saves the gold phase. Every saved
plan kept its records and every candidate reached the PR's head (6 of 6).
No `status` launched an agent, wrote to GitHub or changed the state
directory. §6's tests and the harness's checks hold that no controller
decision changed.

The decision confirms the gates above: D and A stay as ADR 0012 recorded
them and as the maintainer accepted on #370, D a no-go and A a no-go except
for #434's one class (D11). It reopens neither, and #371 to #373 are not
required to close the MVP.

- **Roll-forward: approved.** It is this pull request: the corrected
  advice, and the evaluation. Everything else is already on `main`. Every
  part is either read-only advice (the stop line, the package, the preview)
  or the one saved field (`stop_code`), and none of them decides anything.
  No default, threshold, state schema or protocol changes.
- **Rollback: approved as written here.** Undoing the correction alone is
  a revert of this pull request. It changes no state, so any run can be on
  either side of it, but it brings the unsafe text back. Undoing the MVP
  means reverting #374's commit too. A controller refuses a state saved
  under a newer protocol than its own
  (`tests/test_protocol_compat.py::test_a_newer_label_is_refused_before_its_phase_and_fields_are_read`),
  so one from before #374 refuses every state saved under protocol 16 or
  later. That rollback is possible only once every run has finished or
  been started afresh. Nothing in the evidence calls for it.
- **Accepted remaining risks,** as the maintainer accepted them:
  - The evidence is offline and comes from the suite's own scenarios,
    which the package's texts were developed against. It shows that the
    package agrees with the controller's decisions. It does not show that
    the package is effective for a person (§7).
  - Human diagnosis time was not measured, and the review report's 51-run
    corpus was not read (§7).
  - The steps without a command were scored from hand-written readings
    (§7).
  - The reworded `claim-ambiguous` advice for a run bound to a PR was not
    sampled. Since the decision, §4.1 samples one such stop, two round
    comments of one review round; two follow-up issues of one finding and
    two progress comments on the EPIC remain unsampled.
  - A later `status` cannot say why a resumable error stopped (§4). The
    failing command's `stop:` line and the run log carry the cause.
- **Follow-ups, not blocking.** First, a refused LOCAL state's refusal
  should spell `run --force` with placeholders for the issue and the EPIC,
  which the controller does not read from a file it refuses
  (`local-blocked`). Second, §7's corpus protocol is to be run against the
  review report's runs. The maintainer said they would open each as its own
  issue; this change opens none. The missing take-over this record first
  listed as a follow-up is the correction above.
- **Operator sign-off: signed off** by the maintainer, in the same comment.
  Operators read `status`'s Recovery block, run `status --live` before an
  `unblock`, use the troubleshooting guide, indexed by symptom
  (`docs/troubleshooting.md`), and treat every displayed command as advice
  that the command itself re-checks. With this pull request, the
  `claim-ambiguous` advice goes by what the reason names. For two PRs
  carrying the issue's marker and none recorded by the run, it says to keep
  the PR you choose and take it over in place of the plain `unblock`. For a
  marked PR headed in a fork, it says to close or unmark it. For two round
  comments the run did not journal, it says to delete them, or none when one
  is another run's.

**After the decision.** Review round 2 of the pull request found the two
`claim-ambiguous` stops of §4.1, and this record's revision corrects their
advice. The decision predates those corrections, which the pull request's
next review reads. They change no row of §4 and no count of §5 that the
decision relies on, and no controller decision. The take-over for two
marked PRs, which the decision read as the step in sequence, is now the
alternative taken in place of the plain `unblock`, and it is taken in the
same case.
