# ADR 0012. Recovery and escalation: who may act on a stop, and the advisor's two gates

- **Status:** accepted. A maintainer accepted this record on 2026-11-03 in
  the
  [maintainer acceptance on #370](https://github.com/iceboundrock/AutoForge/issues/370#issuecomment-6591923375),
  as proposed in PR #446 at `0aa7987`. That comment is the sign-off #370's
  first acceptance criterion asks for; this line only records it. The
  commit that records it changes no decision. One addition followed the
  acceptance, at PR #446's review (R2-F1): D5 now names the threat-model,
  cost and security reviews #370's decision 4 requires before any go for
  gate A. It makes reopening A stricter, keeps A's no-go and changes no
  other decision; the acceptance itself is of the text at `0aa7987`.
  Planning only. Written at `main` `0ba238e` (2026-11-02), where every
  reference below was read.
  - *Implemented with it:* nothing. No phase, edge, settlement, effect kind,
    configuration key, state field, protocol number, launch class, prompt or
    default changes. Three tests keep this record honest. Every test it names
    exists (`test_every_test_adr_0012_names_exists`). D2's matrix has a row
    for every outcome that needs recovery
    (`test_adr_0012_stop_matrix_takes_every_outcome_that_needs_recovery`).
    D5's table has every reason family and code
    (`test_adr_0012_gate_a_takes_every_reason_family_and_code`).
  - *Recorded here:* the stop matrix (D2), the authority matrix (D3), a
    **no-go** for gate D, shadow diagnosis (D4), a **no-go** for gate A,
    autonomous recovery actions, with the class and the threat-model, cost
    and security reviews a go needs (D5), and the rules any later advisor
    must meet if a new record reopens D: the pause (D6), isolation (D7),
    evidence (D8), cost and replay (D9), protocol and scope (D10).
- **Decides:** #370 (REC-AR1), for #354.
- **Builds on:** ADR 0011 (#369's typed stop classification, the baseline
  #370 names as its hard dependency).
- **Amends:** nothing.
- **Amended by #414:** D3's adoption row (taking over a marked PR is the
  operator's decision by command, and the controller's write) and D10,
  which records K11's approval.
- **Amended by #433:** D5's `agent` row gains `partial-fix`, the stop of a
  FIX that reported `blocked` after the controller published the commits it
  verified for some findings. It fails (i) as `agent-blocked` does: what the
  fixer asked for is a human's answer, and `unblock` alone leaves the stop.
- **Amended by #434:** gate A reopened for one class only, bounded
  automatic recovery, under the
  [maintainer decision on #434](https://github.com/iceboundrock/AutoForge/issues/434#issuecomment-6819386619)
  of 2026-11-12, which approved the policy and claims no implementation or
  acceptance. D11 records the rule, the class and the three reviews D5
  requires; D2's `resumable_error` row, D3's bounded-relaunch row, D5's
  outcome and its `execution` row (with the new `provider-usage-limit`
  code), D9's launch bound and D10's persisted field change with it. No
  advisor, no gate D, no pause, no phase and no edge out of `BLOCKED` or
  `FAILED` is added.
- **Amended by #479:** D12 records how the engine accepts a result an
  agent delivered before its launch timed out or exited badly, which D11.2
  left to #479 and the decision on #434 ranked D2. No recovery action is
  added: accepting a reply relaunches nothing, and D11's allowlist and its
  refusal of a launch whose output holds a complete `CONTROL_RESULT` are
  unchanged; D11's rule also refuses a launch whose teardown could not look
  for a re-parented process (`orphans_unchecked`, R1-F2 of PR #481). One state
  field is added, the provisional reply kept until the step accepts it or not
  (`delivered_reply`, protocol 18 → 19, D12.4); no effect kind or
  configuration key changes.
- **Related:** ADR 0004 (the authority boundary, K9 and K10), ADR 0005 (the
  outer sandbox and its launch classes), ADR 0007 and #126 (the durable
  pause), ADR 0008 D9.4 and D15 (the scheduler's entry checks and
  `Settlement`), ADR 0009 (LOCAL's removal); #23 (protocol numbering), #258,
  #260, #265, #311; #374 and #375, which ADR 0011 names as consumers of its
  baseline; #306, #307, #315, #316, #319, #321, #363, #364 and #371 to #373,
  which #370 names and the repository does not reference at `0ba238e`
  (D5, D9, D10).

## 1. Context

#370 is the architecture gate of #354's recovery and escalation work. It
replaces the premise it calls "blanket Pre-BLOCK Advisor first" (read
here as: a model is consulted before the controller blocks a run) with a
deterministic-first record. That record keeps what the controller already
decides without a model, says who may act on each stop, and admits a model
only where evidence shows it adds something. #354's review report gives
the rationale and is not repeated here.

What a stop is and allows at `0ba238e` is typed and tested:

- **Stops are classified, and the classification grants nothing.** ADR 0011
  gives each stop an outcome, a reason code, a typed source and the
  documented operator routes. Only the engine and the CLI import the
  classification, the engine never reads a route, and `unblock` takes none
  (`test_only_the_sinks_and_the_cli_import_the_classification`,
  `test_the_engine_only_names_codes_and_never_reads_a_route`,
  `test_unblock_takes_no_classification`). No code is persisted (ADR 0011
  D5), so a code exists only in the process that computed it. *(Amended by
  #374: the code is now saved with the stop, protocol 16; see D10.)*
- **`BLOCKED` has operator edges only, and `FAILED` none.** `unblock` is the
  one exit from `BLOCKED` that keeps the run. It is a controller decision
  from live GitHub, not an operator override: the operator supplies a reason
  and the controller chooses among `UNBLOCK_TARGETS` or refuses
  (`docs/agent-guides/workflow.md`, "Leaving BLOCKED").
- **Retries are typed and bounded.** A failure is retried only when its
  class is known to be transient and a bound exists
  (`docs/agent-guides/state-and-recovery.md`, "Error handling"). Semantic
  and safety failures become `BLOCKED` or `FAILED`.
- **The run log is audit output.** `events.jsonl` is append-only and no
  recovery path reads it (`state-and-recovery.md`, "Idempotency and crash
  recovery"; `test_runlog_recovers_sequence_from_step_names_never_from_the_journal`).
- **No pause exists.** `transitions.durable_pause` is `False` for every
  state until #126 lands (`test_no_state_awaits_input_until_126_lands`).
- **Agents hold no GitHub credential**, and every externally visible write
  is a typed controller effect or nobody's (ADR 0004 D1.1).
- **LOCAL is removed** (ADR 0009). A LOCAL state is refused by the loader
  and gains no `unblock` edge
  (`test_a_local_state_is_refused_never_given_an_unblock`).

So the open questions are not about what a stop *is*. They are what may act
on one, under whose authority; whether a model's diagnosis improves on what
the deterministic package already tells the operator (gate D); and whether
any recovery *action* exists that something other than the operator could
be allowed to take (gate A).

### Terms

- **Advisor:** a model launch whose output is advice about a stop: a
  diagnosis, or a handoff text for the operator. It is not an agent phase.
  It produces no candidate, review, effect or transition.
- **Recovery executor:** anything that performs an action to resolve a
  stop without an operator's command for that action.
- **Deterministic package:** what the operator has at a stop without a
  model: the `stop:` line and `status --json`'s `stop` object (outcome,
  code, source, routes; ADR 0011), the persisted `block_reason` (which names
  the object and the remedy), `unblock --dry-run`'s preview of the decision,
  and #260's troubleshooting guide, indexed by symptom.
- **Shadow-only:** an output kept beside the deterministic package, never
  written into it, and shown only when asked for.

## 2. Decisions

### D1. The deterministic MVP needs nothing new

#354's deterministic MVP is what `0ba238e` already has: ADR 0011's
classification and stop lines, `unblock`, `resume`, a new run, the
scheduler's `epic resume` and `epic archive`, and #260's guide. It requires
no advisor, no recovery executor, no new pause state and no state field.

Gates D and A are decided separately (D4, D5). A no-go on either is a final
outcome in its own right. Neither blocks the MVP's acceptance, and no
executor or advisor is built only so that #354 can close.

### D2. The stop matrix

REMOTE is the only mode. "Target" is ADR 0011's view: a standalone run, or
a scheduler namespace. An outcome's routes come from the outcome alone
(ADR 0011 D4), so no classification, code or advice adds, removes or
reorders an edge.

| Outcome | Persisted as | Who leaves it, and how: standalone | Who leaves it, and how: namespace | Permitted while stopped | Denied |
|---|---|---|---|---|---|
| `blocked` | phase `BLOCKED` | the operator: `unblock --reason`, where the controller chooses among `UNBLOCK_TARGETS` from live GitHub or refuses and stays `BLOCKED`; then `resume`. Or a new run (`run --force` when the run holds operator actions) | the operator: `unblock` against that issue's own namespace state directory, after the scheduler exits (ADR 0008 D9.4), then `epic resume`; or `epic archive` and a fresh admission | inspection: `status`, `status --json` and `unblock --dry-run`, which write nothing, and `doctor`, which writes its state-directory probe (D3); the operator's own GitHub actions; `unblock`'s K9 and K10 flags (D3) | `resume` and `step` (terminal for them); any agent launch; an automatic `unblock`; `BLOCKED -> REPLAN_REEXECUTE`; an `unblock` that takes a classification, a code or a recommendation |
| `failed` | phase `FAILED` | the operator: a new run | `epic archive`, then a fresh admission | the same inspection | `unblock` (it refuses every phase but `BLOCKED`), `resume`, any edge |
| `resumable_error` | the phase as last saved, with the attempt counter the step saved | the operator: `resume`, which reconciles the phase entry with GitHub before any launch. *Amended by #434:* or the running `run` (or `resume`) itself, once per step, for an allowlisted typed failure only, through the same reconciliation (D11) | `epic resume`; *amended by #434:* or the worker's own loop, by the same rule (D11) | the same inspection | treating it as `BLOCKED` or `FAILED`; a relaunch beyond the step's bound; retrying a failure no adapter typed as transient; *amended by #434:* a second automatic relaunch of a step, whatever failed; an automatic relaunch of a category D11 does not allow |
| `refused` | nothing: the command acted on no run | the operator, by code (`REFUSAL_ROUTES`). Its `repeat` route is the refused command itself, `resume` and `unblock` included, once the cause is fixed: the recorded configuration restored, or the change accepted with `--accept-config-change` (written to the run log first); a fixed configuration or host; the lock holder gone. Otherwise a state repaired or moved aside (`run --force` quarantines an unreadable one) | the same; `epic resume --accept-config-change` forwards the acceptance to its workers (ADR 0008 D9.4) | the same inspection | a repeat that skips any check of the refused command (below); reading the refusal as a change of the run, which it did not save; a configuration change taken without `--accept-config-change`, a weaker `sandbox.mode` included |
| `awaiting_input` | #126's pending record (not implemented) | nobody until #126 lands; then #126's decision commands | settled (ADR 0008 D15.1) | the same inspection | producing it for anything but a human's pending answer (D6) |
| `unknown` | whatever was saved | the operator, after investigating; acting needs live facts re-read | the same | the same inspection | any route a known outcome would grant |
| LOCAL state (removed mode) | a `mode: "LOCAL"` file | nobody: the loader refuses it (`refused` / `state-invalid`), and a REMOTE run starts fresh | not applicable: a namespace's state is REMOTE | none on the file | `unblock`, `resume`, or a migration of it into a REMOTE state |

The stops by design (`ready_for_merge`, `handoff`, `done`, `in_progress`)
are not recovery cases. ADR 0011 D1's routes for them stand unchanged; a
namespace's `ready_for_merge` is merged by hand
(`test_a_namespace_ready_for_merge_is_merged_by_hand_never_resumed`).

**A refusal's route names the refused command and grants it nothing.**
`refused`'s `repeat` route is the command that was refused, whichever it
was. A `resume` or an `unblock` refused for configuration drift is repeated
with the recorded configuration restored, or with `--accept-config-change`
(`test_accept_config_change_records_the_change_in_the_run_log_and_continues`,
`test_unblock_refuses_another_configuration_and_records_an_accepted_one`).
The repeat runs every check that command has. `unblock` still refuses a run
that is not `BLOCKED` (`test_unblock_requires_a_blocked_run`) and still
chooses the phase from live GitHub. `resume` still stops at a run that is
`BLOCKED` or `FAILED`, and still reconciles its phase entry before any
launch.

**Crash windows.** Each has one outcome:

- **Before a stop is saved.** The run is in the last saved phase, and
  `resume` re-enters it through its entry reconciliation. It reaches the
  same sink again from GitHub, or progresses. An effect whose outcome is
  unknown (`intended` or `attempted`) is reconciled by its identity before
  anything is sent again: adopted if it landed, `conflict` (`BLOCKED`) if
  someone else's object is there (ADR 0004 D4).
- **After `BLOCKED` or `FAILED` is saved.** The stop is durable and its
  code is lost with the process (ADR 0011 D5). `status` shows the outcome
  with code `unknown` and the text of `block_reason`.
- **During `unblock`.** It sends nothing to GitHub and launches nothing. The
  chosen phase and its history entry are one atomic save. Before it, the
  run is still `BLOCKED`, and a run log line already written for it decides
  nothing, since no recovery reads the log. A transient read failure
  propagates and records nothing, not even a refusal
  (`test_unblock_propagates_a_transient_github_failure_and_writes_nothing`).
- **During an advisor's run**, should D ever be approved: the stop was saved
  before the advisor started (D6). A crash loses the advisor's output and
  nothing else.

**Uncertainty.** Every case resolves to "the run stays where it is" or
`BLOCKED`, never to a guess:

- a conclusive negative refuses, naming it;
- an inconclusive GitHub read refuses `unblock`, which stays `BLOCKED`
  (`test_unblock_refuses_a_pr_that_cannot_be_read_conclusively`), and keeps
  the merge gate's phase only within `merge.max_verification_attempts`;
- two candidates (two PRs, two marker-bearing objects) block, and the
  controller never chooses between them;
- an unknown view gets only the routes both views share, and an unknown
  code is `unknown`, never derived from text (ADR 0011 D2, D6).

### D3. The authority matrix

Every recovery-relevant operation has exactly one kind of authority.
**An advisor holds none of them.** Its recommendation is not an input to
any check below, not evidence that a check passed, and not an approval;
no check gains a parameter for it, as `unblock` has none for a
classification (`test_unblock_takes_no_classification`).

| Operation | Authority | Mechanism at `0ba238e` | When it cannot allow |
|---|---|---|---|
| reconciliation the controller already does (phase entry, effect records, plan completion) | the controller, automatically | ADR 0004 D4; `resume` | conflict or ambiguity: `BLOCKED`; transient: state unchanged, for `resume` |
| bounded transient reads and relaunches | the controller, automatically, within a typed bound | an adapter's `transient_failure` (at most `MAX_TRANSIENT_RETRIES`, 2, per step); the pushed-head wait (#403); `merge.max_verification_attempts`; `GitHubUnavailableError` for `resume`; *amended by #434:* the bounded automatic recovery rule (D11): one relaunch per step of an allowlisted typed failure, journaled and audited | bound exhausted: `BLOCKED`, or the error for `resume`. An untyped failure is not transient. *Amended by #434:* a refused decision leaves the error, with its typed refusal, for `resume` |
| dry-run inspection and read-only status | anyone | `--dry-run` of `run`, `step`, `resume`, `unblock` and `rebind-epic`; `status` | writes nothing, whatever it finds (`test_run_dry_run_writes_nothing`, `test_unblock_dry_run_reads_github_and_writes_nothing`) |
| environment checks | anyone | `doctor` | is not write-free, and changes no run whatever it finds. Its one write to the controller's files is its state-directory probe: it creates the state directory when it is absent, and the directory stays; then it creates and removes one probe file in it, through the same boundary as a run's writes (`test_state_dir_on_a_filesystem_without_hard_links_names_the_requirement`). Its sandbox rows launch in system temporary directories they remove. It neither reads nor touches the run's `state.json` (`test_the_controller_row_names_the_imported_package_and_reads_no_state`), and skips the probe for a configuration it refused (`test_doctor_writes_no_state_directory_for_a_configuration_it_refused`) |
| leaving `BLOCKED` | the operator asks; the controller chooses the phase from live GitHub | `unblock` | refuses, and the run stays `BLOCKED` |
| K9, the acceptance comment on the run's issue | the operator per action id (`safety.acceptance_actions: approve`, the default), or the operator's configuration (`auto`); under `off` nobody, and the resolution is refused. The value is recorded with the plan, so a later change never widens it | `unblock --approve-action` / `--decline-action` | nothing is sent while unapproved (`test_an_acceptance_comment_awaiting_the_operator_is_held_with_nothing_sent`) |
| K10, a comment on another issue | the operator performs it; the controller only verifies | `unblock`'s operator-action flags | never sent by the controller (`test_operator_actions_block_with_the_plan_saved_and_nothing_sent`) |
| K12, the description of the run's own PR | the operator's configuration (`safety.pr_body_updates: auto`, the default), or the operator per action id (`approve`); under `off` nobody, and the resolution is refused. The value is recorded with the plan, so a later change never widens it | `unblock --approve-action` / `--decline-action` | nothing is sent while unapproved, and an edit outside the plan is neither written over nor adopted (`test_an_edit_after_the_plan_saved_is_a_conflict_and_a_reason_is_not_evidence`) |
| any other GitHub write | the controller, as a typed effect kind (K1 to K9, and K11 since #414); a new kind is an ADR 0004 amendment, approved separately (D10) | the effect records | no kind: nobody performs it (ADR 0004 D1.1) |
| PR adoption and identity repair | the controller recovers only the PR its own state recorded, while that PR's marker names this run, and adopts through K3 only an unmarked PR. Taking over a marked PR is decided by the operator, by command, and written by the controller (K11, #414). Any other object's ownership is the operator's decision, taken on GitHub, and `unblock` re-reads it | `ANALYZE_EXECUTE`'s entry; K3; `unblock --take-over` and K11 (ADR 0004 D9.4a) | two candidates, a foreign marker or a PR opened during the run: `BLOCKED`, never adopted (`test_analyze_unmarked_pr_opened_on_the_branch_during_the_run_is_not_adopted`); a marked PR the run has no record of, or a bound PR whose marker does not name this run: `BLOCKED` before any launch or send (`test_a_fresh_run_blocks_on_a_pr_another_run_owns_and_names_the_owner`, `test_a_displaced_run_blocks_at_its_next_entry_and_launches_nothing`); a body changed since the takeover read it: `BLOCKED`, nothing written over (`test_k11_never_writes_over_a_body_edited_after_the_takeover_read_it`) |
| security downgrade (a weaker `sandbox.mode` than the pin) | the operator, explicitly | `sandbox_pin`; `--accept-config-change` | exit 2, nothing written (`test_a_remote_run_refuses_disabled_with_nothing_written`) |
| configuration drift (profile routing, worktree root) | the operator, explicitly | `config_fingerprint` (#219); `--accept-config-change` | exit 2, nothing written (`test_unblock_refuses_another_configuration_and_records_an_accepted_one`) |
| raising a bound (`workflow.max_total_steps`, `max_review_rounds`, ...) | the operator, through the configuration | configuration, then `unblock` | the bound keeps blocking (`test_unblock_refuses_while_the_step_budget_is_exhausted`) |
| merge | the controller, only with `safety.allow_merge` *and* `--allow-merge`, after its own pre-merge verification and an applicable review certificate; or a human by hand; never a scheduler worker | the merge gate | conclusive negative: `BLOCKED`; inconclusive: the phase is kept within its bound, then `BLOCKED` (`test_merge_phase_requires_both_gates`, `test_with_the_merge_gate_open_no_worker_merges_or_writes_the_epic`) |
| state migration | operator commands only (`run --force`'s quarantine, `epic archive`, `rebind-epic`); a protocol change is code, numbered on #23 | the strict loader | refused, nothing written |
| abandoning a run | the operator | a new run | — |

**`DENY` and `INCONCLUSIVE` fail closed.** Every row above has three
results. **`ALLOW`** needs conclusive, positive evidence the controller read
itself. **`DENY`** is a conclusive negative. **`INCONCLUSIVE`** is anything
else: a transient failure, an unreadable or ambiguous object, more than one
candidate. `DENY` and `INCONCLUSIVE` both leave the run where it is or enter
`BLOCKED`. Neither writes what `ALLOW` would have written. An
`INCONCLUSIVE` read is retried only where a bound already exists. That is
today's behaviour, held by the tests the table names. This record names it
so that no later change reads "not denied" as "allowed".

### D4. Gate D, shadow diagnosis: no-go

**Outcome: no-go.** No advisor diagnosis or handoff text is approved, in
shadow or otherwise.

The gate's condition is offline evidence that an advisor's independently
checked diagnosis, or its handoff text, improves on the deterministic
package in error rate, time or cost. No such evidence exists. ADR 0011's
corpus protocol has not been run over the review report's history (its
D7.5). This planning change authorizes no paid-model benchmark, so the
trial that would produce the evidence needs its own approval. The
deterministic package has not been measured either, so there is nothing yet
for an advisor to beat.

A later record may reopen D only by bringing all of the following:

1. **A corpus of real stops**, gathered with ADR 0011 D7's read-only tool.
   Each stop is labelled with the action that actually resolved it before
   any advisor output is seen.
2. **Pre-registered measures.** The error rate, time and cost measures, how
   each is taken, and the threshold that counts as an improvement are named
   before the trial runs. A diagnosis that points to a wrong route, or to an
   action D3 does not permit, is an error. One that repeats what the
   deterministic package already says is no improvement.
3. **Independent checking.** Every claim in the advisor's output that names
   a typed fact is checked by the controller against the evidence bundle
   (D8). A claim that cannot be checked is shown as unchecked, and counted
   as an error when it is wrong.
4. **The comparison is against the deterministic package**, measured on the
   same stops, not against having nothing.
5. **D6 to D9 hold** for the trial and for anything shipped after it.

A go under these conditions approves only an optional advisor: off unless
configured, shadow-only, isolated (D7). Shadow-only means it never changes
`block_reason`, the `stop:` line, `status`'s `stop` object, a route, an
existing run log record, the state or GitHub. Its output is kept apart and
shown only on an explicit request.

### D5. Gate A, autonomous recovery actions: no-go

**Outcome: no-go.** No recovery executor is approved.

Gate A asks two things, in order. First, whether #369 and #373 show a
nonempty class of recovery actions that are (i) permitted by D3 to
something other than the operator, (ii) verifiable afterwards from facts
the controller reads itself, and (iii) not already decided
deterministically. Second, whether that class passes a threat-model, cost
and security review (#370's decision 4). With no class there is nothing to
review, so the no-go below rests on the first question alone; what a go
needs is after the table. #373 is not referenced in the repository at
`0ba238e`. On #369's evidence, ADR 0011 D6's inventory of every stop sink,
each family of reason codes (`ReasonFamily`) but `intentional` fails at
least one condition. Every family is below, with every code it holds. The
`intentional` family (the stops by design, and #126's reserved pause) is no
recovery case; its row is there so that a code added to it is decided like
any other:

| Family | Codes | What would resolve it | Fails |
|---|---|---|---|
| `bound` | `step-budget`, `review-round-cap`, `review-stagnation`, `replan-refused`, `producer-validation-bound`, `validation-interrupted`, `validation-inconclusive`, `selection-rejected` | raising a bound; deciding that more work is worth its cost; fixing what stopped a validation run | (i): configuration, spending and the host are the operator's, and leaving `BLOCKED` is too. After the operator's `unblock`, a fresh validated round is already the route (iii) |
| `operator_wait` | `acceptance-action`, `operator-actions` | an approval, or the operator's own comment | (i), by definition (K9, K10) |
| `identity` | `unjournaled-write`, `effect-conflict`, `claim-ambiguous`, `pr-owner`, `default-branch`, `read-back-mismatch`, `fix-plan-refused`, `epic-roadmap` | deciding whose object it is, then deleting, closing, reopening or editing it | (i): no effect kind performs those writes. Ownership is not decidable from GitHub ("The controller cannot tell who posted the comment", `docs/usage.md`), and a model's guess at it would be prose turned into authority (D8) |
| `github` | `github-read-failed`, `github-checks-refused`, `github-unavailable`, `github-error`, `git-transport` | a credential with the right access, or another read or push | (i) for a credential. (iii) for a transient failure, which `resume` already retries within its bound, and a push, which reconciles by its identity first |
| `merge_gate` | `merge-inconclusive`, `pr-identity`, `pr-not-open`, `unreviewed-merge`, `merge-checks`, `merge-conflict`, `merge-not-ready`, `merge-write`, `premerge-verification`, `review-certificate`, `review-not-backed` | merging; changing the PR or its checks | (i) for a merge. Changing the PR is implementation work whose result must be reviewed: a workflow phase, not a recovery action. After the operator updates the PR, `unblock` already routes the moved head to `REVIEW` (iii) |
| `workspace` | `checkout-drift`, `sandbox-tamper` | restoring a checkout or object store something moved | (i): a security signal, which an automatic repair would turn into a downgrade path |
| `agent` | `agent-blocked`, `agent-failure`, `partial-fix` | a human's answer, or a fresh launch | (i) for an answer, which is #126's domain. (iii) for a launch, which is `resume` or a new run; `FAILED` has no edge. After a `partial-fix` stop the controller has already published what it verified, and the answer the fixer asked for is still a human's |
| `execution` | `execution-failed`, `execution-timeout`, `sandbox-launch-refused`, `interaction-required`, `control-result-invalid`, `verification-failed`, `provider-usage-limit`, `run-log-refused` | `resume` | (iii): already the route. Whether a *new* failure is transient is a typed adapter rule reviewed as code, as #164 and #395 added, not a model reading stderr (D8). `interaction-required` is #126's domain, and `sandbox-launch-refused` never runs unsandboxed (ADR 0005 D12.1). *Amended by #434:* the one class gate A approves comes from this family: an idle timeout and an adapter-typed result-less exit, the controller taking `resume`'s own route once per step (D11). Every other failure of the family stays (iii); a provider's usage limit stays the operator's while no provider reports a reset time the controller can trust (D11.3) |
| `configuration` | `configuration-invalid`, `configuration-drift`, `sandbox-capability`, `scheduler-owned` | another configuration, an accepted drift, a capable host, the scheduler's own commands | (i) |
| `state` | `state-invalid`, `state-protocol`, `state-transition`, `lock-held` | a repaired or moved-aside state file, a newer controller, the right command, another process ending | (i) |
| `unknown` | `unknown` | — | (ii): nothing typed says what happened |
| `intentional` | `ready-for-merge`, `handoff`, `done`, `in-progress`, `awaiting-input` | nothing: a stop by design keeps ADR 0011 D1's routes (D2), and a pause is answered by its human through #126 (D6) | none applies: not a recovery case |

The class is empty, so A is no-go. Nothing is built only to satisfy #354.

*Amended by #434:* a class now exists, and gate A is a **go for it alone**:
the controller's own `resume` of a step whose agent launch ended with an
allowlisted, typed failure, once per step (D11). It meets (i): D3's
bounded-relaunch row is the controller's, automatically, within a typed
bound. It meets (ii): the relaunch re-enters the phase through the same
entry reconciliation an operator's `resume` runs, which reads GitHub and the
journal itself. And it is not decided deterministically today (iii): before
#434 the run waited for the operator to type `resume`. D11.7 records the
threat-model, cost and security reviews. The no-go stands for every other
family, code and action, and for an advisor in any role.

A later record may reopen A, and give it a go, only by bringing all of the
following. A qualifying class is necessary, not sufficient: the three
reviews are approvals in their own right, and no evidence that the class
exists stands in for them.

1. **A concrete action class** that meets (i) to (iii), named action by
   action.
2. **The corpus evidence** (D4 item 1) that it occurs.
3. **A passed threat-model review.** It places the executor and every
   input it reads among the actors of the supported threat models (ADR
   0004 §2.11, ADR 0005 §2.2), and shows that nothing untrusted (project
   data, an agent's output, a stop's text) can choose an action or its
   target. A review that leaves the executor outside those models, or finds
   a path from untrusted text to an action, fails.
4. **A passed cost review.** It states what each action spends in model
   launches, time and GitHub writes, against the operator route it
   replaces, under bounds enforced as counts and time (D9). An action whose
   cost is only a provider's estimate, or has no enforced bound, fails.
5. **A passed security review** of the executor's authority. It names the
   D3 row that grants each action and shows that the executor holds no
   credential an agent can reach and that `DENY` and `INCONCLUSIVE` still
   fail closed for every action.

Each review and its outcome is recorded in the record that reopens A,
before any go and before any executor is built. This record performs none
of them: with no class, there is nothing to review.

Three things could make a class appear: a new stop family or code, a new
effect kind approved under D10 that turns a remedy into a controller write
with a verifiable identity, or #373's findings. The first cannot pass
unnoticed: a family or code added to `stop_classification.py`, in any
family, `intentional` included, without its row here fails
`test_adr_0012_gate_a_takes_every_reason_family_and_code`. An action
approved this way is still a typed controller operation behind D3, never an
advisor's.

### D6. #126 and the scheduler's `Settlement`: no pre-BLOCK pause

There is no pause before `BLOCKED`. A stop is saved first; that is what
deterministic-first means here. Anything an advisor ever computes about it
comes afterwards and cannot delay or change the stop.

- **No new run state.** No phase, no `Settlement` value and no
  `durable_pause` predicate is added for an advisor. `issue_settlement`
  keeps its five values (`test_every_phase_has_exactly_one_settlement`), so
  the scheduler sees a `BLOCKED` issue as `terminal` at once, as today.
- **Where an advisor would run**, should D be reopened: as a separate,
  explicitly invoked, read-only command over a saved stop, reading the state
  as `status` does. Never as a step of `run`, `step`, `resume` or `unblock`,
  and never inside a scheduler worker. That keeps the default workflow and
  every worker's lifetime unchanged, and makes shadow-only hold by
  construction. The price is the process boundary: such a command sees the
  stop's code only if the code is persisted, which needs its own protocol
  bump (D10, ADR 0011 D5).
- **A wait for a human reuses #126.** A future durable pause that waits for
  a human's answer reuses #126's durable pause (ADR 0007 D7.5, D7.8) and
  `Settlement.AWAITING_INPUT` unchanged: one control plane, not two. Any
  other pause needs a separately ratified amendment to ADR 0007 and ADR 0008
  D15. That amendment states the scheduler consequences: whether the issue
  is settled, what `epic status` shows, and which claim and lock it holds.
- **A wait for a machine is never `AWAITING_INPUT`.** `AWAITING_INPUT` means
  a human's answer is pending, and the scheduler treats it as settled. An
  advisor's computation that set it would settle an issue no human was
  asked about.

### D7. Advisor isolation, should D be reopened

ADR 0005's REMOTE agent launch class maps the issue's worktree writable
and its admin directory writable. An advisor needs neither and must get
neither. A reopened D brings, through its own ADR 0005 amendment, a new
**advisor launch class**:

- no workspace: its working directory is an empty, controller-owned
  directory;
- no `.git` entry, no git common dir, no object store, no other worktree,
  and no state directory, run log or read context other than its own;
- one read-only carve-out: its evidence bundle (D8), as the read context is
  for an agent;
- a private per-launch home and temp directory, and no persistent cache;
- provider model auth only, through its declaration (ADR 0005 §2.7), and no
  GitHub or git credential (child_env, as for every launch);
- the network position of a REMOTE agent, which it needs to reach its model.

**Under `sandbox.mode: required`** the launch must be enforced, as its
record proves (`enforced: true`).

**Under `disabled`** the advisor is refused before it launches. A prompt
telling a model not to write, or a flag the controller sets, is logical
isolation, not security isolation (AGENTS.md, "Runtime model"). The only
alternative is a separately approved read-only transport, for example a
model API call that carries the bundle and executes no tool. It needs its
own record. There is no silent fallback from the launch class to that
transport, or from either to an unisolated launch.

### D8. The evidence boundary

`events.jsonl` is diagnostic and audit output, not a recovery input
(`state-and-recovery.md`). It stays that way. An advisor's input is an
**evidence bundle the controller builds at the source** from facts it
observed itself, each typed, redacted and bounded through the read
context's rendering (`read_context.py`):

- the stop: outcome, code, source and view (ADR 0011);
- the persisted phase and settlement;
- the bound identities: issue, PR, reviewed HEAD, review round;
- effect records: kind, stage and identity;
- step counts and their bounds; the sandbox pin and mode;
- what the controller reads live for the bundle, as `unblock`'s inspection
  does.

**Not inputs:** `events.jsonl`, stdout, stderr, `progress.log`, agent prose
(a `CONTROL_RESULT` `message`, or the agent text `block_reason` quotes), and
any earlier advisor output.

**The log-to-diagnostic bridge: none.** Nothing from a log, stderr or prose
becomes a typed fact, and no code is derived from text by any pattern (ADR
0011 D2). If a reopened D wants text excerpts, they go in a separate,
labelled untrusted section of the bundle, redacted and bounded as read
context is. Nothing the advisor says about them counts as checked (D4 item
3).

**An advisor's output is never proof.** It is quoted, labelled as advice,
and never re-enters as evidence for anything, including a later advisor's
bundle.

### D9. Cost and replay

- *Amended by #434:* an automatic relaunch (D11) is one more step of the
  run, so its launches are counted and bounded like a `resume`'s. Two bounds
  apply, and only the second is cumulative:
  - The **automatic sequence** of a step, the command whose step failed and
    the one relaunch the rule then makes, launches at most `(1 +
    MAX_AUTOMATIC_RELAUNCHES_PER_STEP) × (1 +
    execution.max_correction_attempts + MAX_TRANSIENT_RETRIES)` agents, 8
    with the defaults. It bounds what the controller does on its own, not
    the step. Every **operator-initiated** `resume` (or `run`, or `step`)
    that re-enters the step afterwards launches up to `1 +
    execution.max_correction_attempts + MAX_TRANSIENT_RETRIES` agents more,
    and makes no automatic relaunch, since the step's allowance stays spent
    (D11.4). A step re-entered often enough therefore launches more: nine
    one-step invocations of a step whose agent keeps idling launch nine
    agents, one more than the 8 above.
  - `workflow.max_total_steps` is the **cumulative bound on physical
    launches**, of a step and of the run, over every command, whoever starts
    it. Each launch (a step's first, a correction, a transient retry, an
    automatic relaunch) is charged as one step, against the persisted count,
    before it starts (D11.4), so the run's launches never exceed its step
    count, and none starts once the budget is spent.
- **Count physical model launches.** Every advisor launch, correction and
  transient relaunch is one launch: one run log line with its own producer,
  counted in ADR 0011 D7.3's launch denominator. None is hidden inside
  another.
- **#319 and #321** are not referenced in the repository at `0ba238e`. #370
  asks to use them where they exist: whichever lands first defines the
  launch and cost counters an advisor uses, and the advisor brings no
  counter of its own.
- **Estimates are not budgets.** The bounds enforced today are counts and
  time:
  - at most `1 + execution.max_correction_attempts + MAX_TRANSIENT_RETRIES`
    launches per execution of a step (one command's; *amended by #434:* an
    automatic sequence is at most two of them, above);
  - `workflow.max_total_steps` per run;
  - each profile's idle and maximum-runtime limits.

  A provider-reported cost (Claude Code's `total_cost_usd`, kept as
  `cost_micro_usd` in `provider_summary`) is the provider's estimate, absent
  for other providers, and enforces nothing. An advisor's budget is enforced
  as launches and time. Money is reported only as an estimate, labelled so.
- **Pin the identities.** Each advisor output records the digest of its
  evidence bundle, the digest of its prompt template, the gate record that
  approved it, and its provider, model and effort. A replay over the same
  bundle is then comparable. A replay never re-reads live GitHub into an old
  bundle.
- **The proposal envelope.** An advisor's output is a typed, versioned,
  bounded envelope, sharing the envelope and schema approach #363 and #364
  take for proposals. #315 (and #316) are not referenced in the repository.
  As #370 says, neither is a prerequisite of the deterministic MVP.

### D10. Protocol and scope

- **No persisted field here.** A later one is coordinated on #23 with the
  protocol number current when its pull request is ready, never a reserved
  one (`state-and-recovery.md`, "Numbering a new protocol"). **Amended by
  #374:** #374's recovery package needs the stop's code across processes,
  so the sink that enters `BLOCKED` or `FAILED` saves it as `stop_code`
  (protocol 16, ADR 0011 D5 as amended). It is the code and nothing else of
  the classification, `unblock` clears it, and nothing that decides a phase,
  a write or a permission reads it: `status` classifies the stop with it and
  the package says what it means. No stop matrix row, authority or gate of
  this record changes.
- **Writes.** A new cross-object write kind is approved separately, by #306
  and #307 as #370 assigns, and lands as an ADR 0004 amendment with its own
  protocol bump, as K9 and K10 did. **Amended by #414:** #414's decision
  record approves K11 (`owner_takeover`) as a new controller write under
  this rule: the takeover of the issue's implementation PR, which the
  operator decides with `unblock --take-over` and the controller writes.
  It lands as ADR 0004's D9.4a and K11, with protocol 14 (ADR 0004
  D13.11).
- **Amended by #434:** the recovery journal (`auto_recovery`, protocol 18,
  ADR 0004 D13.13) is the authority for D11's accounting: the step's
  allowance, its failure counters and each decision with its K13 audit
  record. Only the recovery path writes it and only it and `status` read
  it; no stop's code is read by it (D11.1).
- **Ownership stays where it is:** #260 the troubleshooting guide, #311
  target preflight (ADR 0011 D6 leaves open what it owns), #258 requests an
  agent CLI refused, #265 the protocol refusal and its guidance.
- **GitHub links.** The dependencies this record names are prose. Prose is
  not a scheduler-enforceable `blocked_by` edge, and native links are
  established and read back on GitHub, not here.

### D11. Bounded automatic recovery (#434)

The maintainer decision on #434 approved the policy: D1 (one shared
per-step allowance, failures counted by category), D3 (on by default for a
narrow allowlist only), D4 (a usage limit is never retried at once), D5 (a
result-less exit relaunched at most once, after reconciliation), D6 (in-step
retries first), D7 (one automatic relaunch per step, inside every existing
bound) and D8 (the journal and the dual audit). This section is that
policy as built (`src/autoforge/auto_recovery.py`, `ControllerEngine.run`).

**D11.1. Typed facts, never a reason code or text.** The rule decides on
the class of the error that ended the step and on the typed facts the
engine set on it of the agent launch that ended it (`LaunchEnd`, the
error's `launch_end`): the limit that killed it (`idle`, `max_runtime`,
`loop`), whether a process it started may still run (or the platform could
not look for one it left outside its group, `orphans_unchecked`), whether its output
holds a complete `CONTROL_RESULT` in the part the parser reads, and what its
adapter typed (a usage limit, a known-transient failure, an exit without
any result, a failure its provider had established before a limit killed
it). It reads no message, no agent output and no stop
classification: `auto_recovery.py` does not import
`stop_classification.py`, and ADR 0011 D3 stands
(`test_the_rule_reads_no_message`, `test_the_rule_reads_no_stop_classification`).

**D11.2. The allowlist.** Two categories, and only these, are relaunched:

- `idle-timeout`: the executor killed the agent at its idle limit;
- `result-less-exit`: the agent's CLI exited on its own inside a session
  its protocol stream shows was running, before a single result record,
  with no line lost on the way, as its adapter typed it (Claude Code's
  `stream-json` today: the CLI's `system/init` and a tool call of the
  model's, which no authentication, permission, configuration or host
  failure at the CLI's start produces, and no model API error the CLI
  reported in its own typed message, below). The absence of a result is no
  evidence of why the CLI ended: an empty stream, or one that ended before
  the model's first tool call, is an untyped exit whatever stderr says, and
  stderr, free text, is never read for it. An exit code or a missing
  `CONTROL_RESULT` alone types nothing either (R1-F1 of PR #480). What this
  evidence cannot tell apart is a fault that strikes inside a running
  session (the host's disk filling up while the model works, say) from a
  crash of the CLI itself: such a step is relaunched once, through the
  reconciliation, and its repeat is refused, the step's allowance spent
  (D11.4).

Refused, and the operator's as before: a hard runtime limit and a loop
kill (never repeated), a timeout nothing typed, a provider's usage limit
(D11.3), a failure the provider had established before a limit killed the
launch (below), a known-transient failure whose in-step relaunches were spent or
withheld, a request for a human decision, an untyped exit, a result refused
after its corrections, a sandbox refusal or clean-up uncertainty, a GitHub
or git failure (drift and propagation stay stops; the pushed-head wait of
#403 is in-step and unchanged), a failed verification, effect or claim, and
anything unknown. An allowlisted failure is still refused when a process the
launch started may still run, or when the platform could not look for one
the launch left outside its group (`orphans_unchecked`, no child subreaper:
the teardown cannot prove that no writer remains, so the refusal is
`process-may-survive`, R1-F2 of PR #481), or when its output holds a complete
`CONTROL_RESULT` (#285; the engine accepts that result first where it can
prove it safe, D12, and a relaunch that would discard one it did not accept is
not made)
(`test_only_the_allowlist_is_ever_relaunched`,
`test_a_result_delivered_before_the_kill_is_never_discarded`,
`test_a_launch_that_may_have_left_a_process_is_never_relaunched`,
`test_an_idle_timeout_whose_orphans_could_not_be_looked_for_is_never_relaunched`,
`test_a_claude_exit_whose_cause_nothing_typed_tells_is_never_relaunched`,
`test_a_claude_cli_that_died_inside_a_running_session_is_relaunched_once`).

A failure the adapter established before a limit killed the launch stands
through the kill (R2-F1 of PR #480). A launch whose provider had already
failed the run and that stayed alive until a limit killed it is a timeout
to every other reader, since a timeout wins over what the protocol said
(ADR 0003 §2.6): Claude Code inside its failure-exit grace after a turn
ended on a model API error (401 and 403, an authentication or permission
refusal, included), an error result or a protocol violation; OpenCode hung
after a run-level error; Pi killed inside the abort that followed a failed
conversation. The adapter keeps the failure's fixed kind as
`failed_before_limit` (`claude: API error 401`, `claude: stream-json
protocol violation`, `opencode: a run-level error was printed`, `pi: the
RPC conversation had failed the run`), never text from the output, and the
rule reads it before the timeout, after a usage limit only. Its category,
`failed-before-limit`, is not on the allowlist, so neither an
authentication or permission refusal nor protocol drift becomes an idle
timeout to relaunch
(`test_a_claude_failure_then_an_idle_kill_is_never_relaunched`,
`test_a_failure_reported_before_any_kill_vetoes_the_relaunch`).

Claude Code types a model API error twice: in its own `assistant` message
(`is_api_error_message: true`, the status as `api_error_status`) and in the
error `result` that ends the turn. A CLI killed, or exiting, between the two
never writes the result, so the adapter keeps the message's typed facts
without it (R3-F1 of PR #480), never its text: killed by a limit, its
status is the failure before the limit (`claude: API error 401`); exited, it
is a failed exit naming the status and never a result-less exit, so an
untyped exit; and a 429 is the usage limit either way (D11.3). The message
fails nothing by itself: a run whose result then arrives is decided by that
result, as before
(`test_a_claude_authentication_error_without_its_result_is_never_relaunched`,
`test_a_claude_usage_limit_without_its_result_is_a_usage_limit_never_relaunched`).

**D11.3. Usage limits.** An adapter types a provider's usage or rate limit
from the provider's own typed answer (Claude Code's model API error 429, in
its error result or, when that never arrives, its typed API-error message;
OpenCode's run-level error "The usage limit has been reached", compared
whole), never as transient. The engine raises `ProviderUsageLimitError`,
whose stop is `resumable_error` / `provider-usage-limit` naming the provider
and the limit. It is never relaunched: with no reset time the controller
can trust (no adapter reads one today) the refusal is `no-trusted-reset`,
and with one it is `no-wait-policy`, since no explicit bounded wait policy
exists. No wait, no inferred reset and no switch to another profile is made;
a fallback belongs to #321's routing (`test_a_usage_limit_is_never_relaunched_at_once`).
A limit the adapter established stands whatever ended the launch after it:
a CLI that reported its limit and then stayed alive until its idle limit
killed it (Claude Code inside its failure-exit grace, OpenCode hung after
its error) keeps the fact through the teardown, the step ends as a usage
limit, and the rule reads the limit before the timeout, so it is never an
idle timeout to relaunch (R1-F2 of PR #480;
`test_a_claude_limit_then_an_idle_kill_is_a_usage_limit_never_relaunched`,
`test_a_limit_reported_before_any_kill_vetoes_the_relaunch`).

**D11.4. One allowance per step, inside every bound.** A step is one phase
of one issue, PR, review round and execution attempt, over every command
that re-enters it. Its allowance is `MAX_AUTOMATIC_RELAUNCHES_PER_STEP` (1),
shared by every category: the second failure of the step is refused
whatever it is, so a timeout followed by a usage limit cannot loop. Each
failure also counts in its category, which grants nothing. The allowance is
persisted, so neither a crash, a later `resume` nor an `unblock` back into
the same phase refills it; a phase change is a new step. A relaunch also
needs a step of `workflow.max_total_steps` and one of the invocation's
`--max-steps`, and a scheduler worker's `should_continue` still true. The
allowance bounds the controller's own relaunches, never the operator's: a
`resume` after it re-enters the step with the in-step bounds and no
automatic relaunch, and `workflow.max_total_steps` is the bound on every
launch (D9 as amended, R2-F2 of PR #480). The
relaunch is one more step, charged when it first persists, like any step
(`test_a_second_failure_of_the_step_is_refused_whatever_failed`,
`test_the_step_budget_and_the_invocation_bound_are_never_exceeded`,
`test_an_invocation_asked_to_stop_is_not_relaunched`).

Every physical launch is charged the same way (D7, R1-F3 of PR #480): a
relaunch within a step, a correction or the retry of a transient failure,
is one more step of `workflow.max_total_steps`, checked against the
persisted count and saved in the save before it starts, so a crash while
it runs, or any later command, finds it counted. A relaunch with no step
left enters `BLOCKED` (`step-budget`) with nothing launched, as the budget
does before a step. So no mix of an automatic relaunch and in-step
relaunches launches past the budget
(`test_a_correction_or_transient_relaunch_is_charged_as_a_step`,
`test_an_automatic_relaunch_then_in_step_retries_never_exceed_the_budget`,
`test_a_relaunch_interrupted_while_it_runs_stays_charged_after_a_restart`).

**D11.5. The journal and the dual audit.** Every decision, a relaunch or a
refusal, is in this order: the state read again from disk (the relaunch sees
exactly what an operator's `resume` would); the decision; one save of it with
its accounting, its typed evidence, its `intended` K13 record (ADR 0004
K13) and its run log record still to write, before anything follows from
it; its run log record (`<PHASE>-auto-recovery`, profile `(automatic
recovery; no agent)`, which the corpus tool counts as bookkeeping); then its
audit comment on the run's PR, or its issue while it has none, through the
effect path. Only an allowed decision with both complete relaunches; one
without relaunches nothing, and the allowance stays spent.

The run log record is completed at most once, like the comment (R1-F4 of
PR #480): the step directory it goes to is named and saved (`attempted`)
before the write, and `written` is saved after it. A crash before the write
leaves it `intended` or `attempted` with no such directory, and the next
command writes it; a crash after the write leaves it `attempted`, and the
next command finds that one directory's `execution.json` (the run log
publishes it before the event journal's line) and adopts it, never writing
it twice. That one directory is all of the run log ever read for it.

A decision is *settled* once both are complete. The journal never drops an
unsettled one (past its bound it refuses a new decision instead), and no
`run`, `resume` or `step` executes anything before every unsettled
decision it finds is completed, oldest first (R1-F5 of PR #480): a run log
that refuses the record, or a GitHub that cannot be read now, stops the
command with the phase unchanged, for `resume` to try again; a conclusive
GitHub failure (`github-read-failed`) or a comment in conflict
(`effect-conflict`: a second comment with the marker, or one whose text is
not the controller's) enters `BLOCKED`, naming the target and the marker.
The operator resolves it on GitHub and runs `unblock`, which has a
conflicting comment read again (ADR 0004 D2.4's rule for a phase plan's
conflict: its attempt count kept); the next command adopts the comment or
posts it once before anything else. Neither sink is a recovery input: they
decide no relaunch
(`test_an_unconfirmed_audit_relaunches_nothing_and_the_next_command_posts_it_once`,
`test_an_audit_that_landed_before_a_crash_is_adopted_never_posted_twice`,
`test_a_crash_after_the_decision_spends_the_allowance`,
`test_a_crash_around_the_run_log_record_completes_it_exactly_once`,
`test_repeated_github_failures_stop_every_command_and_lose_no_decision`,
`test_a_conflicting_audit_blocks_the_run_across_restarts_until_the_operator_resolves_it`).

A comment on the run's PR is a write on that PR, so it is sent only while
the PR is this run's (ADR 0004 D9.4a; R4-F1 of PR #480): right before it is
driven, after a failed launch and on every later command that would send it,
the PR is read as itself, and its implementation marker must name the
decision's issue and this run, as for K4's review comment. The agent ran for
minutes, and a takeover in between hands the PR to another run. A PR that
another run owns, or whose owner is not knowable (no marker, no owner, an
unreadable marker, a read answered with another PR), stops in `BLOCKED`
(`pr-owner`) with nothing sent and nothing relaunched, the comment kept
`intended` or `attempted`; a read that fails conclusively stops as any
conclusive failure of the audit does, and one GitHub cannot answer now
stops the command with the phase unchanged. A comment on the issue, before
the run has a PR, has no owner marker to read. The way back is the one the
stop names, the operator's `unblock --take-over` of the PR: while that
takeover is journaled and not yet written, the comment waits for it, still
unsent; the step that follows writes this run into the marker and completes
the rest from the journal, launching no agent (a launch reached while the
comment waits is refused), and the next command or loop step reads the PR
again, posts the comment once, or adopts the one that landed before the
takeover, and only then executes anything else. A failure of the takeover's
own step is decided like any other, and its comment, on the same PR, waits
for the takeover too, the phase unchanged for `resume`
(`test_a_takeover_during_the_failed_launch_posts_no_audit_and_blocks`,
`test_a_takeover_before_the_replay_posts_no_audit_and_blocks`,
`test_the_operators_takeover_completes_first_then_the_audit_then_the_step`,
`test_a_takeover_step_github_failed_leaves_every_audit_for_after_the_takeover`).

**D11.6. Where it runs.** Inside the running `run`, `resume` or scheduler
worker loop (`ControllerEngine.run`), and nowhere else: no daemon, no
scheduler, no wait. `step` executes exactly one phase and never relaunches
on its own; a dry run decides nothing
(`test_step_never_relaunches_on_its_own`, `test_a_dry_run_decides_nothing`).
Only an error that ends a step of an agent phase is decided; a refusal
(exit 2) acted on nothing and is not.

**D11.7. The three reviews D5 requires.**

- *Threat model.* The rule's inputs are typed values the controller and its
  adapters produce: an error's class, the executor's limit and survivor
  flags, the protocol framing of the agent's output, and an adapter's fixed
  reasons, each from typed fields (ADR 0004 §2.11's agent and project data
  are actors; neither chooses a value). Nothing untrusted chooses the action
  or its target: the action is always the same (re-enter the same phase
  through its reconciliation), and the target is the state's own step. An
  agent can at most end its own launch idle or without a result, which
  costs it the step's one relaunch and nothing else; the delivered-result
  and survivor checks only refuse more. The audit comment carries no agent
  text.
- *Cost.* One automatic paid relaunch per step at most, counted as a step of
  `workflow.max_total_steps` and of `--max-steps`, against the operator
  `resume` it replaces (the same launch, minutes to hours earlier). Its
  in-step corrections and transient relaunches are the existing bounds
  (D9), each now charged as a step of `workflow.max_total_steps` too
  (D11.4). A hard runtime limit is never repeated, so no allowance buys
  a second multi-hour launch; a usage limit is never relaunched. GitHub
  writes: one comment per decision, posted at most once.
- *Security.* D3's bounded-relaunch row grants the action, to the
  controller, within a typed bound. The rule holds no credential: the
  relaunch is an ordinary launch, with every check of a launch (the
  sandbox gate, the checkout anchor, the credential preflight, the owner
  check). `DENY` and `INCONCLUSIVE` fail closed: anything not proven allowed
  is refused, a decision that cannot be saved, logged or audited
  relaunches nothing, and one left unsettled stops every later command
  until it is settled.

**D11.8. Not done here.** A bounded wait for a trusted reset time and a
fallback profile (D4, D11.3; #321); typing a result-less exit for OpenCode
and Pi, whose output types none today. Accepting a result delivered before a
kill is D12 (#479).

### D12. A result delivered before a failed launch end (#479)

An agent can deliver its complete final reply and still end badly: its CLI
exits with a failing status after the reply, or stays alive after it (a
shutdown that hangs, a task left pending) until a limit kills it. The engine
used to discard such a reply with the failure, and the operator's `resume`
paid for the step again (#285). #439 kept a Claude `stream-json` reply that
decides the run from its idle limit, by stopping the CLI. D12 is the
provider-neutral rule for every other end, as built
(`src/autoforge/delivered_result.py`, `ControllerEngine._invoke_phase`). A
provider's success is a claim: D12 accepts nothing on it alone.

**D12.1. Five distinctions, none implying the next.** A *completed turn* is
the provider protocol's typed end of the agent's turn (Claude's `result` for
every turn opened with no tool call open; Pi's `agent_settled`). A *delivered
complete result* is a completed turn that ended the run on a successful final
reply, with nothing opened after it, handed over whole: the adapter's fact
`AgentExecutionResult.reply_delivered`, set by Claude Code's `stream-json`
(`ClaudeStream.reply_proven`: `replied`, read to the stream's end, with no
line of the stream skipped past the per-line bound, since a record never read
may have opened a later turn or a tool call, or failed the run, R3-F1 of PR
#481) and by Pi (its
conversation settled and its last assistant text read back, before any limit
cut it short, with every tool execution it started ended by its call id and
nothing but that text's response on stdout after `agent_settled`, read
through Pi's shutdown to the end of stdout or the kill that ended it, a
record of unknown kind included: `PiConversation.reply_settled`, R1-F3 and
R2-F3 of PR #481), never by an output that
types no turn (Claude's `text`, OpenCode). A *terminated process* is how the CLI ended: an exit 0, or a stop
after its reply (#439), is read as always and never reaches D12; a failing
exit (or a provider failure, ADR 0003 §2.6) and a kill at a limit do. A
*successful cleanup* is the executor's teardown verified complete: no member
of the group outlived SIGKILL, no writer held the pipes, no re-parented
process outlived its kill, and the platform could look for one
(`orphans_unchecked` false); a process the teardown killed is gone. An
*accepted controller result* is never D12's to declare: the checks below
make the reply *provisional* and no more, and only the step's own checks
after them (D12.4) accept it.

**D12.2. The rule.** Typed facts only, the first refusal that applies wins:
a request for a human decision ended the launch; the provider's protocol had
failed the run (a provider failure, a failure before the limit, a usage
limit, a transient failure); a limit other than the idle limit or the
maximum runtime killed it (the loop detector's verdict is about the very
activity that produced the reply, and an unnamed limit proves nothing); a
process the launch started may still run; the platform could not check for
one; no delivered reply; a reply whose blocks are missing, begun and never
closed, or in conflict (`reply_block_shape`: every complete block must decode
to the same value, stricter than the parser's last-block rule, since a reply
whose launch failed is taken only when it says one thing); a reply the
capture bound cut. Anything not proven is refused
(`test_each_missing_fact_refuses_the_reply`,
`test_the_refusals_come_in_their_declared_order`,
`test_a_reply_is_taken_only_when_its_blocks_say_one_thing`).

**D12.3. The checks are a clean exit's.** A reply the rule lets through goes
to exactly the checks the reply of a clean exit goes to, in the same place:
the strict import or the verified export (ADR 0005 D6.5, ADR 0013 D5), the
parse, and the phase's own checks of the run's issue, the worktree's HEAD
against the reported `head_sha`, the bound PR, HEAD and round. A malformed
result, another invocation's (another issue, another phase, a review of a
HEAD the PR no longer has) and a candidate that moved after the reply are
refused there. Passing them makes the reply provisional (`execution.json`'s
`delivered_result` says `provisional`), never yet accepted (R1-F4 of PR
#481). A refusal is final for that launch: nothing corrects a reply
whose launch failed (a correction is a relaunch, and whether the step runs
again is D11's), and a read that could not be completed proves nothing. The
launch's failure then ends the step as before, with the reason on it, and
D11 decides on it: a launch whose output holds a complete block is still
never relaunched (`result-delivered`), and the decision's evidence names why
the reply was not accepted
(`test_a_reply_delivered_before_a_failed_end_is_accepted_and_the_step_goes_on`,
`test_a_reply_without_its_proof_is_never_accepted_and_the_step_stops_safely`,
`test_a_review_for_another_head_delivered_before_a_kill_is_never_accepted`).

**D12.4. Durability and acceptance.** A provisional reply is kept in the
run's state before the step goes on from it (R1-F1 of PR #481): one save
writes `delivered_reply` (`delivered_result.KeptReply`, state protocol 19),
the validated `CONTROL_RESULT` object (redacted, as every agent text the
engine persists; the parser refuses a credential in any field the controller
publishes, so redaction changes nothing it acts on), its digest, and its
binding: the phase, attempt and launch label of the launch that delivered
it, the issue, the PR, the review round, the candidate the checks read, the
revision the run was bound to (the PR's HEAD, base and merge base: the diff a
review decides on, R2-F1), how the launch ended and its run log step. Before
that save the operator's checkout is checked against the anchor of the
invocation that delivered the reply (R2-F2): the anchor lives in memory only
and a restart reads one of its own, so a move found only later would go
unseen by a restart that goes on from the kept reply; a move found there
ends the step in `BLOCKED` (`checkout-drift`) with the reply never kept. The
step then goes on from it as from any reply: the operator's checkout is
checked again, the PR's owner, then the phase's verification, whose plan the effect journal (ADR 0004) saves before
anything is written. Only when that verification applies the reply as the
step's result is the reply *accepted*, and the evidence is explicit: the
save of the plan the verification turned it into (`_save_plan`), whatever
stops the step after it (a review round's cap once the round's comment is
posted, a conflict, a write that waits for the operator) and although the
plan's completion may drop it again, or the record of the agent's own
`blocked`/`failure` (R2-F4 of PR #481). That holds on every path out of
the step after the save, an exception included: another run's takeover of
the PR found before the first write stops the step in `BLOCKED`
(`pr-owner`) with nothing sent, and the reply was accepted all the same
(R3-F2). A step that stops before that save
(checkout drift, another owner, a verification or validation refusal), or
moves on without one (a FIX whose PR moved goes to REVIEW with no fix
recorded), did not accept it (R1-F4). Either way the step's own
controller-only record in the run log (`<PHASE>-delivered-result`) says so,
`accepted`, or `not-accepted` with the stop's code or the phase the step
moved to, with whether the reply reached that step kept across an
interruption (`kept_across_restart`) and already applied before it
(`applied_before_restart`), and the step's message opens with one sentence
that claims acceptance only when it holds. The save of the plan also marks
the kept reply `applied`, in the same write (R3-F2): an error or a crash
after it (a PR create GitHub did not answer, say) ends the step without an
outcome, and the state holds both the plan and the acceptance's
provenance. The step that next enters the phase takes up the kept reply it
finds, whatever then ends that step, and settles it once: an applied one is
recorded `accepted` while the step completes the plan from the journal, and
is never checked again or gone on from (a launch that finds one, the plan
gone with the phase asking again, records it first and drops it); one not
applied is checked again as below, and recorded `not-accepted` if the step
ends before it goes on from it. A crash
anywhere after the keeping save, before the step concludes, leaves the
reply in the state: the next `resume`, `run` or `step` re-enters the phase
through its reconciliation, which completes a saved plan from the journal
first, and otherwise checks the kept reply again, exactly as a fresh one,
against the worktree and GitHub as they are then, and goes on from it with
no agent launched and no step of a launch charged; a write whose outcome
was lost is read back, never sent twice. A binding that no longer matches
the run, a revision the run is no longer bound to (a REVIEW entry under bwrap
or `disabled` binds the PR's HEAD, base and merge base again before the kept
reply is read, and a review's verdict is about its own diff alone, so a
retargeted base or a moved merge base refuses it, R2-F1), or a check that now
refuses the reply (a candidate that moved, a review HEAD the PR no longer
has), is a safe stop: `BLOCKED`
(`control-result-invalid`) with nothing launched, published or applied, and
the reply dropped, so `unblock` launches the agent afresh. The kept reply is
dropped by every save in another phase or after another launch, and by the
step when its own checks refuse it, so a refused reply is never gone on from
twice. The run log records each outcome (`execution.json`'s
`delivered_result`: provisional or refused, the reason, the facts; the
step's record: accepted or not) and is never read for recovery; D11's
journal records a refusal's decision, as for any failed step
(`test_an_accepted_reply_is_never_run_again_after_a_restart`,
`test_an_accepted_reply_whose_pr_create_landed_is_adopted_never_sent_twice`,
`test_a_provisional_reply_is_kept_in_the_state_before_the_step_goes_on_from_it`,
`test_a_kept_review_is_posted_once_after_a_restart_and_the_reviewer_not_run_again`,
`test_a_kept_review_whose_diff_moved_before_the_restart_stops_safely`,
`test_a_kept_reply_whose_candidate_moved_before_the_restart_stops_safely`,
`test_a_kept_reply_bound_to_another_launch_is_never_gone_on_from`,
`test_a_kept_reply_outlives_an_unavailable_github_and_resume_goes_on_from_it`,
`test_a_reply_the_checkout_drift_check_refuses_after_the_parse_is_never_accepted`,
`test_a_checkout_the_agent_moved_is_found_before_its_reply_is_kept`,
`test_a_reply_the_phase_verification_stops_before_planning_is_never_accepted`,
`test_a_review_applied_before_the_round_cap_stopped_the_step_was_accepted`,
`test_a_review_refused_before_its_plan_was_saved_was_not_accepted`,
`test_a_review_whose_pr_was_taken_over_after_its_plan_was_saved_was_accepted`,
`test_a_reply_applied_before_an_interruption_is_recorded_by_the_step_completing_its_plan`,
`test_an_applied_reply_whose_plan_is_gone_is_settled_and_never_gone_on_from`,
`test_a_kept_reply_the_restart_refuses_is_recorded_not_accepted`,
`test_a_fix_whose_pr_moved_goes_to_review_without_accepting_the_reply`,
`test_a_reply_the_producer_gate_refuses_below_its_bound_is_dropped_and_relaunched`).

**D12.5. Every bound stays.** The launch was charged as a step before it
started, and accepting its reply launches nothing, so no step, no relaunch
and no allowance is spent or refilled; the runtime limits killed the launch
as before; `step` accepts as `run` does, since accepting is no relaunch; a
dry run launches nothing to accept
(`test_an_accepted_reply_after_a_kill_is_charged_one_step_and_decides_nothing`,
`test_step_accepts_a_delivered_reply_as_run_does`,
`test_a_dry_run_launches_and_accepts_nothing`).

**D12.6. The providers.** Claude Code's `stream-json` vouches for a reply its
stream decided whatever the exit after it, and for none with a call still
open or a turn opened after it, nor for one in a stream of which a line was
skipped past the per-line bound, before the reply or after it (R3-F1 of PR
#481): a clean exit and #439's stop on a reply read such a stream as before; Pi vouches for a settled conversation whose
shutdown then hangs past a limit or exits badly, and for none its limit cut
short, none with a tool execution left open, and none after which Pi wrote
anything but the text's response, a later turn in its shutdown included (a
start without a call id, or past the reducer's bound, can never be shown
ended, so it refuses too), and so does a record after the run settled whose
kind cannot be known: not UTF-8, not JSON, or not an object with a string
`type` (R2-F3 of PR #481); OpenCode and Claude's `text` output vouch for none, so a block they
printed before a kill is refused (`no-reply`) and stays the operator's
(`test_a_claude_cli_that_exits_badly_after_its_reply_has_the_reply_accepted`,
`test_a_claude_reply_with_a_call_still_open_is_never_accepted`,
`test_a_claude_reply_overtaken_by_a_later_turn_is_stale_and_never_accepted`,
`test_a_claude_reply_followed_by_a_line_past_the_bound_is_never_accepted`,
`test_a_claude_reply_followed_by_a_line_past_the_bound_reads_as_before_otherwise`,
`test_a_skipped_line_voids_the_proof_of_a_reply_but_not_the_stop_on_it`,
`test_a_pi_that_hangs_on_shutdown_after_a_settled_run_has_its_reply_accepted`,
`test_a_pi_that_exits_badly_after_a_settled_run_has_its_reply_accepted`,
`test_a_pi_reply_with_a_tool_execution_left_open_is_never_accepted`,
`test_a_pi_reply_overtaken_by_a_later_turn_in_its_shutdown_is_never_accepted`,
`test_a_pi_reply_followed_by_an_unreadable_shutdown_record_is_never_accepted`,
`test_opencode_types_no_reply_so_a_block_before_its_kill_is_never_accepted`).
#439's behaviour is unchanged: its reply still completes the run, as
before, with no D12 decision.

**D12.7. The three reviews D5 requires.** D12 is no recovery action, but it
turns a failure into an accepted result, so it gets them. *Threat model:*
every input is typed (the adapter's fact, the executor's flags, the block
framing the parser reads of any reply); an agent can at most deliver a reply
and then hang or fail, which buys it nothing the checks of a clean exit would
not give it, and the stricter block rule refuses an ambiguous reply; a kept
reply is re-checked in full after a restart, against the PR revision it was
kept under, and is kept only once the checkout its invocation was anchored to
was found unmoved, so time between the crash and the restart buys it nothing
either. *Cost:* none added; one paid relaunch
saved per accepted reply, and per crash in a step that went on from one.
*Security:* no credential, write or bound changes; every check of a clean
exit's reply runs, again after a restart; the kept payload is redacted
before it is persisted; a teardown that may have left a writer, or could not
look for one, refuses, and so does the recovery rule's relaunch (D11).

## 3. What does not change, and what holds it

| Invariant | Held by |
|---|---|
| the controller alone decides transitions, and no other edge exists | `test_every_phase_pair_matches_the_declared_topology`, `test_unblock_goes_through_validate_transition` |
| `BLOCKED` is left only by `unblock`; `FAILED` and `DONE` have no edge | `test_terminal_phases_have_no_outgoing_edges_except_the_operators_unblock`, `test_unblock_requires_a_blocked_run`, `test_unblock_refuses_the_other_terminal_phases` |
| LOCAL gains no `unblock` by implication | `test_a_local_state_is_refused_never_given_an_unblock`, `test_the_one_workflow_mode_is_remote` |
| a classification is never an authorization, and text is never a code | `test_only_the_sinks_and_the_cli_import_the_classification`, `test_the_engine_only_names_codes_and_never_reads_a_route`, `test_an_error_message_is_never_read`, `test_a_persisted_reason_is_never_read_for_a_code` |
| no pause, and no new settlement | `test_no_state_awaits_input_until_126_lands`, `test_every_phase_has_exactly_one_settlement` |
| only a stop's code is persisted (#374), and nothing that decides reads it | `test_only_the_code_of_a_classification_is_persisted`, `test_unblock_takes_no_classification`, `test_the_saved_stop_code_is_written_and_cleared_only_by_the_sinks_and_unblock` |
| the run log is never a recovery input | `test_runlog_recovers_sequence_from_step_names_never_from_the_journal` |
| no agent launch holds a credential route | `test_an_agent_finds_no_credential_route_the_controller_finds_every_one` |
| transient retries are bounded | `test_transient_retries_are_bounded_per_step_across_corrections`, `test_transient_read_failure_is_bounded_then_blocked` |
| an automatic relaunch is once per step, allowlisted and audited (#434) | `test_only_the_allowlist_is_ever_relaunched`, `test_two_idle_timeouts_relaunch_once`, `test_an_idle_timeout_is_relaunched_once_and_the_run_goes_on` |
| a result delivered before a failed end is accepted only on proof and only by a clean exit's checks and the step's own (#479) | `test_each_missing_fact_refuses_the_reply`, `test_a_reply_without_its_proof_is_never_accepted_and_the_step_stops_safely`, `test_a_reply_the_checkout_drift_check_refuses_after_the_parse_is_never_accepted` |
| a provisional reply is never run again after a crash, and is re-checked before it is gone on from (#479) | `test_a_provisional_reply_is_kept_in_the_state_before_the_step_goes_on_from_it`, `test_a_kept_reply_whose_candidate_moved_before_the_restart_stops_safely`, `test_a_kept_review_whose_diff_moved_before_the_restart_stops_safely`, `test_a_checkout_the_agent_moved_is_found_before_its_reply_is_kept` |
| no write without its authority: K9, K10, merge, downgrade, drift | the tests D3's table names |

Default single-agent behaviour, every default and the prompts are
unchanged.

## 4. Open questions

- **The corpus.** Running ADR 0011 D7's tool over the review report's
  history, and posting the summary on #369, is the first input any reopening
  of D or A needs. It is a maintainer's step, not this record's.
- **#373.** Whether its findings name an action class that meets D5's
  conditions (i) to (iii). Such a class still needs D5's three reviews
  before a go.
- **The persisted code.** It is needed only by a consumer in another
  process: a reopened D (D6), or #374 and #375 if they need it. It comes
  through #23 at that time. *#374 brought it (protocol 16, D10 as amended).*
