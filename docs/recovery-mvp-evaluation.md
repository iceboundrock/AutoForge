# Deterministic recovery MVP: evaluation and go/no-go (#375)

- **Status:** proposed with #375's pull request, at `main` `d6283cc`
  (2026-11-18), where every reference below was read. It recommends a go
  (§8) and decides nothing. The go/no-go and the operator sign-off are the
  maintainer's, recorded on #375. Until they are, §8 is a recommendation.
- **Evaluates:** EPIC #354's deterministic MVP as `main` holds it. That is
  ADR 0011's typed stop classification and `stop:` lines (#369, `84df820`),
  the saved stop code, the recovery package and `status --live` (#374,
  `4a95c6d`), the bounded automatic relaunch of ADR 0012 D11 (#434,
  `143ac17`) and the acceptance of a delivered result of ADR 0012 D12
  (#479, `32f5317`).
- **Inputs:** #375's body and its one comment, which the controller handed
  to the agent that wrote this record, and the repository at `d6283cc`.
  That agent held no GitHub credential and read nothing else of GitHub. It
  did not read #354's review report, the discussions of #370 to #374, or any
  operator's run history. What this record says of those comes from the
  ADRs and the commit messages in the repository.
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
AutoForge, the command that leaves the stop, and the phase the controller
must read back after that command. The harness performs it, on a fresh copy
of the case when the package's steps did not, and fails if the saved state
shows another phase or if the next step stops the run again. Every gold
label in §4 passed that check, so each is held by the controller's own
read-back. Nothing an agent or a model said counts as an outcome.

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
- **After: unsafe:** displayed steps that ADR 0012 D2 denies for the stop,
  or that D3 grants nobody. These are: a `resume` or `step` before the run
  has left `BLOCKED`; an `unblock` of a run that is not `BLOCKED`; an
  approval of an action that awaits none; a decline or bind of an action the
  plan does not hold; an open merge gate; an accepted configuration change;
  and any command that is neither the controller's nor the documented `gh`
  pipe of an operator action. An alternative step stands in place of the
  steps before it, so it is checked against the run as it stopped.
- **After: steps resolve:** the package's steps are run in order as an
  operator would type them, without the alternatives. Placeholders are
  filled, the operator's act is performed at the first step that has no
  command, and `doctor` is skipped, since it checks the host and a case
  does not stand in for one. The column is `yes` when those steps run the
  gold command, the controller reads back the gold phase, and the step
  after it (`resume --max-steps 1`) does not stop the run in `BLOCKED` or
  `FAILED`.
- **After: preview:** before the steps run a plain `unblock`, the decision
  `status --live` reports is the one that `unblock` then takes.
- **Plan kept:** for a stop with a saved plan. The package names the
  journal and the candidate commit as data not to touch, the commit is
  still in the repository after the steps, and, when the run went on, the
  run's PR is at that commit.

`—` means the column does not apply to the case.

Every `status` the harness runs is also held to three things. It launches
no agent: the provider fails the case if it is called. It sends nothing to
GitHub: every write the fake can record is compared before and after. And
it leaves the state directory unchanged: the kind, inode, modification
time and bytes of every entry.

## 4. The matrix

| Case | Stratum | Gold stop | Gold resolution | Before: a stop | Before: command | After: stop line | After: later status | After: command | After: unsafe | After: steps resolve | After: preview | Plan kept |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `k10-operator-actions` | K10 | blocked/operator-actions | post each payload on its issue, then `unblock --reason …` | yes | yes | blocked/operator-actions | blocked/operator-actions | yes | 0 | yes | yes | yes |
| `k9-acceptance-action` | K9 | blocked/acceptance-action | `unblock --approve-action <id> --reason …` | yes | yes | blocked/acceptance-action | blocked/acceptance-action | yes | 0 | yes | — | yes |
| `k12-pr-body-update` | K12 | blocked/acceptance-action | `unblock --approve-action <id> --reason …` | yes | yes | blocked/acceptance-action | blocked/acceptance-action | yes | 0 | yes | — | yes |
| `checks-refused-291` | #291 | blocked/github-checks-refused | a credential that may read check runs, then `unblock --reason …` | yes | no | blocked/github-checks-refused | blocked/github-checks-refused | yes | 0 | yes | yes | — |
| `no-stop-327` | #327 | none | none: the controller recovers | — | — | — | — | — | — | yes | — | — |
| `false-conflict-314` | #327 | blocked/effect-conflict | `unblock --reason …` | yes | no | blocked/effect-conflict | blocked/effect-conflict | yes | 0 | yes | yes | yes |
| `ambiguous-prs` | coordination | blocked/claim-ambiguous | close the stale PR, then `unblock --take-over <PR> --reason …` | yes | no | blocked/claim-ambiguous | blocked/claim-ambiguous | no | 0 | no | yes | — |
| `review-round-cap` | bounds | blocked/review-round-cap | raise workflow.max_review_rounds, then `unblock --reason …` | yes | no | blocked/review-round-cap | blocked/review-round-cap | yes | 0 | yes | yes | — |
| `step-budget` | bounds | blocked/step-budget | raise workflow.max_total_steps, then `unblock --reason …` | yes | no | blocked/step-budget | blocked/step-budget | yes | 0 | yes | yes | — |
| `no-saved-code` | no code | blocked/review-round-cap | raise workflow.max_review_rounds, then `unblock --reason …` | yes | no | blocked/review-round-cap | blocked/unknown | yes | 0 | yes | yes | — |
| `agent-blocked` | agent | blocked/agent-blocked | answer the agent's question, then `unblock --reason …` | yes | no | blocked/agent-blocked | blocked/agent-blocked | yes | 0 | yes | yes | — |
| `agent-failure` | FAILED | failed/agent-failure | `run --force --epic <EPIC> --issue <issue>` | yes | no | failed/agent-failure | failed/agent-failure | yes | 0 | yes | — | — |
| `idle-timeout-relaunched` | timeouts | none | none: the controller recovers | — | — | — | — | — | — | yes | — | — |
| `idle-timeout-twice` | timeouts | resumable_error/execution-timeout | `resume` | no | yes | resumable_error/execution-timeout | in_progress/in-progress | yes | 0 | yes | — | — |
| `github-unavailable` | unknown effect | resumable_error/github-unavailable | `resume` | no | yes | resumable_error/github-unavailable | in_progress/in-progress | yes | 0 | yes | — | yes |
| `power-loss-after-push` | crash | — | `resume` | no | no | — | in_progress/in-progress | yes | 0 | yes | — | yes |
| `local-blocked` | LOCAL | refused/state-invalid | `run --force --epic <EPIC> --issue <issue>` | yes | no | refused/state-invalid | refused (exit 2) | no | — | — | — | — |

What the rows that differ show:

- **`ambiguous-prs`** is the one case whose displayed steps do not resolve
  it. Two open PRs carry the issue's marker and the run recorded neither.
  The package's first step says to close every PR that is not the run's
  own, then `unblock`. But neither PR is the run's own, so after the stale
  one is closed the plain `unblock` refuses: a marked PR this run has no
  record of is never bound by finding it (ADR 0004 D9.4a). The refusal, and
  the `status --live` preview before it, spell the command that resolves it
  (`unblock --take-over <PR>`, K11 under #414). The package's static steps
  do not. Nothing unsafe happened: the refusal is ADR 0012 D3's fail-closed
  `DENY`, and the preview predicted it. It costs the operator one refused
  command. It is a deterministic gap, a follow-up for the package (§8).
- **`no-saved-code`** is the review cap saved with no code, as every stop
  saved before protocol 16 loads. The later `status` says `unknown`
  explicitly and never derives a code from the reason's text. It still
  offers the plain `unblock` and the live preview, and the preview names the
  bound to raise, so the steps resolve it.
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

## 5. What the matrix shows

`tests/test_recovery_evaluation.py` recomputes this table from §4.

| Measure | Before | After |
|---|---|---|
| A later `status` shows that the run stopped | 12 of 15 | 12 of 15 |
| The stopping command names the gold outcome and code | 0 of 14 | 14 of 14 |
| A later `status` names the gold code | 0 of 15 | 10 of 15 |
| A later `status` says the code is unknown | 0 of 15 | 1 of 15 |
| The text, or the package, spells the gold command | 5 of 15 | 13 of 15 |
| Unsafe displayed steps | not measured | 0 in 14 packages |
| The displayed steps, followed as typed, resolve the stop | — | 13 of 14 |
| A plain `unblock`'s live preview was its decision | — | 8 of 8 |
| The saved plan and its candidate were named and kept | — | 6 of 6 |
| Resolved by the controller with no operator command | 2 of 17 | 2 of 17 |

**Classification.** Every stop line names the gold outcome and code (14
of 14; the power loss prints no line). A later `status` names the gold
code for every `BLOCKED` or `FAILED` stop that saved one (10). It says
`unknown` for the one that saved none, rather than guessing. For the other
four it says what the state can prove: `in_progress` with the `resume`
route for the three that never stopped, and the loader's refusal for
LOCAL. No case was classified wrongly.

**Handoff.** The package spells the command that resolves the stop in 13
of 15 cases. The text an operator had before spells it in 5. The gains are
where the before arm had little or nothing: an agent's own stop, whose
reason is the agent's text; the bounds, identity and GitHub stops, whose
reasons say what to fix but not the command (several end in "then
'unblock'"); a FAILED run, whose route is a new run; and a crash, which
prints nothing.
In 13 of the 14 cases with a package, following its steps as typed
resolves the stop. The one exception is `ambiguous-prs`, above. Its steps
cost one refused `unblock`, the only repeated `BLOCKED` the sample showed,
and the refusal named the remedy.

**Safety and cost.** No package displayed an unsafe step. No `status`
launched an agent, sent anything to GitHub or changed the state directory,
in any of the 15 stops. The live preview matched the decision of every
plain `unblock` that followed it (8 of 8). Every saved plan and its
candidate were named as data not to touch, and survived (6 of 6). The
package costs no model launch and no GitHub write. `status --live` reads
GitHub as `unblock --dry-run` does, and reports the decision it prints
(`tests/test_recovery_package.py::test_the_live_preview_is_the_decision_unblock_dry_run_prints`).
The CPU time of one `status --json`
over a case's state was 4 ms to 29 ms, median 14 ms, over the 15 stops, in
one run on an Intel Core i7-8700K under CPython 3.14.10 (recorded as each
case's `status_cpu_seconds` in `--junitxml`). That is a measurement of one
host, and no test bounds it.

## 6. The safety regressions

#375 requires each of these to hold. Each one names the tests that hold it
on `main`, and every case of §4 adds its own checks: no `status` writes,
sends or launches anything, and no displayed step is unsafe.
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
  PRs are never chosen between. An inconclusive read refuses `unblock`, and
  a transient one writes nothing. An unknown view gets only the routes both
  views share. A result delivered before a kill is never discarded
  (`tests/test_stop_classification.py::test_a_persisted_reason_is_never_read_for_a_code`,
  `tests/test_stop_classification.py::test_a_saved_code_classifies_only_as_the_code_it_is`,
  `tests/test_recovery_package.py::test_true_ambiguous_prs_are_never_chosen_between`,
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

This change adds no behaviour. It runs no real model, changes no CI
threshold, runs no `autoforge epic run`, and changes no default.

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
- **No stops from outside AutoForge were available.**
- **The before arm is an upper bound** (§2): it reads today's reasons,
  which #369 and later changes corrected.

## 8. Gates D and A, and the go/no-go

**Gate D, an advisor's shadow diagnosis: no-go,** as ADR 0012 D4 decided.
The maintainer accepted that record on 2026-11-03, on #370. This
evaluation does not reopen it. D4 item 4 measures an advisor against the
deterministic package. This record measures that package, and finds two
gaps (`ambiguous-prs` and `local-blocked`). In both the controller already
decides the route without a model: the take-over is spelled in the refusal
it prints, and a LOCAL state's route is a new run. Neither is a diagnosis a
model would add.

**Gate A, autonomous recovery actions: no-go,** as ADR 0012 D5 decided,
except for the one class #434 approved (D11): one relaunch per step of an
allowlisted, typed launch failure. `idle-timeout-relaunched` is that class.
This evaluation does not reopen A, and names no new class.

**#371 to #373** are not needed for this decision. Nothing here depends on
them.

**Recommendation: go for the deterministic MVP as `main` holds it, with
no change.** It classified no case wrongly: it named the gold code, said
`unknown`, or said only what the state proves. It displayed no unsafe
step. It spelled the resolving command in 13 of 15
stops against 5 for the text alone. Following its steps resolved 13 of the
14 stops that have a package. And it changed no decision: §6's tests and
the harness's checks hold that.

- **Roll-forward** is no change. Every part is already on `main`. Every
  part is either read-only advice (the stop line, the package, the
  preview) or the one saved field (`stop_code`), and none of them decides
  anything. No default changes.
- **Rollback** would mean reverting #374's commit. A controller refuses a
  state saved under a newer protocol than its own
  (`tests/test_protocol_compat.py::test_a_newer_label_is_refused_before_its_phase_and_fields_are_read`),
  so one from before #374 refuses every state saved under protocol 16 or
  later. A rollback across that protocol needs every run to have finished,
  or to be started afresh. Nothing in §5 calls for one.
- **Follow-ups**, deterministic and not blocking. First, the package
  should name `unblock --take-over` when the run owns none of the PRs
  carrying the issue's marker (`ambiguous-prs`). Second, a refused LOCAL
  state's refusal could spell `run --force` with placeholders for the issue
  and the EPIC, which the controller does not read from a file it refuses
  (`local-blocked`).
  Third, the corpus protocol of §7 is still to be run. Each would be its own
  issue, and none is opened by this change.
- **Remaining risks.** A later `status` cannot say why a resumable error
  stopped (by design: §4). The cause is in the failing command's `stop:`
  line and in the run log. And the evidence is offline, so a kind of stop
  the suite does not model is not covered.
- **For operators**, nothing changes. Read `status`'s Recovery block. Run
  `status --live` before an `unblock`. Use the troubleshooting guide,
  indexed by symptom (`docs/troubleshooting.md`). Treat every displayed
  command as advice that the command itself re-checks.

**Sign-off.** The deterministic MVP's go/no-go and the operator's sign-off
are the maintainer's to record on #375. This record holds neither.
