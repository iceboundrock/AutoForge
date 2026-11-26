# ADR 0015. Task state below a phase, Issue Memory, and what wins after a crash

- **Status:** accepted. The maintainer accepted this record on 2026-11-18
  in the
  [maintainer decision on #316](https://github.com/iceboundrock/AutoForge/issues/316#issuecomment-6994876085),
  as revised at `821735e` after review round 1 of PR #512. That comment is
  the decision #316 asks for; this line only records it. D1 to D20 stand.
  The comment answers the seven questions §5 held, and §5 now records each
  answer in place of its question. Planning only. Written at `main`
  `9baed0a` (2026-11-18), where every reference below was read. #316's
  "Current scope" was reconciled on 2026-10-25 at `f04db5f` (state
  protocol 12); §1.2 says what has moved since and how this record reads
  the issue where the code moved under it.
  - *The answers, written into the decisions they settle:* D10's cost is
    accepted with no gate, and #317 measures it (answer 1). D9.1's scope of
    one run is kept (2), and so is D13's `REVIEW` without a projection, for
    the MVP (3). D7.4's refusal of read-only roles under `disabled` is
    accepted (4). D7.7's last verified candidate stays reachable by a ref
    of its own under `refs/autoforge/`, the one exception to AutoForge
    creating no local refs (5). #332 closes D5.3's window, for the
    single-agent path too (6). D7.5's residual and cost are accepted, and
    an `inconclusive` caused by replaced checker inputs is named
    `checker-altered`, with the changed paths, wherever the outcome is
    reported (7). D3, D11, D19, §4.2 and §6 follow.
  - *Implemented with it:* nothing in the package. No phase, edge, state
    field, protocol number, configuration key, prompt, effect kind, launch
    class, stop code or default changes. `tests/test_adr_0015.py` keeps
    this record honest: every test it names exists
    (`test_every_test_adr_0015_names_exists`); it claims no protocol
    number and its links resolve
    (`test_adr_0015_claims_no_state_protocol_number_and_its_links_resolve`);
    D3's transition table uses only its own states, leaves none out, and
    cancels each state that is not terminal when its step ends
    (`test_adr_0015_task_table_is_closed_over_its_states`); D14's table
    has a row for every provider the registry knows, and the argv facts
    it states hold (`test_adr_0015_fresh_context_table_takes_every_provider`,
    `test_adr_0015_fresh_context_proofs_hold_in_the_adapters`,
    `test_adr_0015_resume_flags_pass_validation_today`); D2's identities
    are the code's (`test_adr_0015_identities_are_the_codes`), and the
    attempt counter D2.1 refuses as an identity repeats after an `unblock`
    into the same step, as its regression scenario says
    (`test_adr_0015_attempt_repeats_after_an_unblock_into_the_same_step`);
    an agent's stop is applied in one save with no plan, as D4.6 says
    (`test_adr_0015_a_stop_result_is_applied_in_one_save_with_no_plan`);
    git's create-only write leaves a taken ref where it was, and two
    candidates' refs coexist, as D7.7 relies on
    (`test_adr_0015_a_candidates_ref_is_created_and_never_moved`);
    and what D10 says of SQLite holds
    (`test_sqlite_is_only_for_the_read_only_probe_and_disposable_index`).
  - *Revised* on 2026-11-18, after review round 1 of PR #512: an
    attempt's identity is a run-wide launch ordinal, not `state.attempt`
    (D2.1); a result passes through receipt, parse, verification and
    acceptance as separate stages, and the producer's result keeps the
    phase's `CONTROL_RESULT` schema (D4); a fingerprint records what an
    attempt was launched with, and each role has its own rule for what
    makes it stale (D7.3); independent verification takes every checker
    input from the base, reads a pass from a report checked against the
    base's, and has outcomes that are not a pass (D7.5). D3, D5, D11, D19,
    D20 and §4.2 follow.
  - *Corrected after the acceptance,* at review round 2 of PR #512. Each
    correction makes an accepted decision implementable. None reopens
    one, though the third widens a cost the maintainer accepted, as it
    says:
    - a result whose own `status` stops the step is accepted in the stop
      save today's code makes, with no plan, and its Memory proposals with
      it (D4.6);
    - every task still open when its step concludes, is routed elsewhere
      or is unblocked is canceled in that save, its process stopped after
      it, and a reply it delivers later discarded (D3.1);
    - a candidate that changed any checker input is `inconclusive`
      (`checker-altered`), never `verified`, even when the base's checkers
      pass on its replaced tree. That holds §4.2's requirement, which the
      maintainer accepted with the rest. It widens the cost answer 7
      accepted, from a candidate that needs a checker change to any
      candidate that makes one, a new test included (D7.5).
  - *Corrected* at review round 3 of PR #512: each `verified` candidate
    has a ref of its own, created before the save that keeps it and never
    moved. One ref per step, moved to the new candidate before that save,
    left a crash between the two with the kept candidate unreachable, so
    the controller's own update defeated D7.7. A new crash window, W12,
    says what the next command does (D7.7, D11, §4.2, §5, §6).
  - *Recorded here:* the task identities (D2), lifecycle and transition
    table (D3), the `TaskResult` / `PhaseResult` split (D4), the
    launch-intent protocol (D5), the reducer's input and output types for
    #332 (D6), the roles, the step's task graph and the one mutating
    worker of the Memdoor addendum (D7), the four memory layers and the
    trust boundary (D8), Issue Working Memory's record contract (D9), its
    storage (D10), the commit order and a crash-window table (D11), the
    projection and its freshness (D12), and the capability and scope
    decisions (D13 to D20).
- **Decides:** #316 (DEL-A1), for EPIC #315, stage M0.
- **Builds on:**
  [ADR 0004](0004-authority-boundary-and-typed-external-effects.md) (the
  authority boundary, typed effects and their journal),
  [ADR 0005](0005-outer-sandbox-and-host-resource-capability-model.md)
  (launch classes and `sandbox.mode`),
  [ADR 0006](0006-candidate-validation-evidence-and-review-certificates.md)
  (candidate identity, `ValidationRecord`, `ReviewCertificate`),
  [ADR 0007](0007-sdk-providers-isolated-helpers-and-interaction-contract.md)
  (the interaction contract, #126's pause),
  [ADR 0008](0008-parallel-epic-scheduler-authority-run-identity-claims-and-locks.md)
  (run identity, lock domains, `Settlement`),
  [ADR 0011](0011-typed-stop-classification-and-recovery-baseline.md)
  (typed stops) and
  [ADR 0012](0012-recovery-and-escalation-stop-authority-and-advisor-gates.md)
  (D11's bounded relaunch, D12's kept reply).
- **Amends:** nothing. D19 names the record each implementing child
  amends.
- **Related:** #317 and #333 (the Memory MVP), #332 (the reducer), #319
  (the multi-agent MVP), #321 (profile routing and fallback), #318
  (`docs/evaluation-protocol.md`), EPIC #331 with #324, #325 and #326, #23
  (protocol numbering), #126 (durable pause), #147, #154, #176, #266, #269
  and #278. #324, #325, #326 and #332 are not referenced in the repository
  at `9baed0a`; this record knows them from #316's text alone.

## 1. Context

### 1.1 The question

AutoForge runs one agent at a time for each phase entry, and parses
whatever that agent replies as the phase's result. EPIC #315 proposes two
features that change this. Issue Working Memory carries bounded lessons
from one launch of an issue to the next. Multi-agent execution splits a
phase's work among roles. Both need answers the code does not have: what a
unit of agent work below a phase is and who decides its fate; where a
lesson lives and how far it may be trusted; and what wins when a crash
separates the controller's checkpoint from a lesson, a candidate commit, an
attempt's reply and the budget it spent.

#316 asks for one record that answers these as three separate concerns
(authoritative task state, the memory architecture, the capability and
scope decisions), with the smallest contract that lets the Memory MVP
(#317, #333) ship first and the multi-agent MVP (#332, #319) after it, and
with no new orchestration daemon, privileged agent runtime or GitHub effect
kind.

The main judgment of this record is that the Memory MVP needs almost none
of the task machinery. The single-agent path keeps one task per step, and
every state of that task can be read from fields the state file already
holds (D3). Issue Memory lives inside the checkpoint, so no crash can
separate the two (D10). Its projection is a deterministic, bounded file in
the launch's read context (D12). The persisted task table, the reducer and
the roles are #332's and #319's; this record fixes their contract so that
they extend the single-agent path rather than replace it.

### 1.2 What has moved since #316's reconciliation

- **The state protocol is 19, not 12.** Seven bumps landed since; the
  last, protocol 19, added `delivered_reply` (below).
- **LOCAL mode is removed** (ADR 0009, protocol 13). REMOTE is the only
  mode (`WorkflowMode` has one member). Of #316's "FIX and LOCAL follow
  later", only FIX is left to decide (D13).
- **A reply can outlive a failed launch end.** ADR 0012 D12 (#479) keeps a
  reply delivered before a timeout or a bad exit in the state, once the
  checks a clean exit's reply meets accept it, and a restart re-checks it
  without launching (`delivered_reply`, `engine.py:17492`). D5 builds on
  it.
- **Every physical launch is charged before it starts** (#434, ADR 0012
  D11.4): a correction and a transient relaunch count against
  `workflow.max_total_steps` like a step's first launch.
- **The evaluation is registered** (#318,
  [docs/evaluation-protocol.md](../evaluation-protocol.md), 2026-11-05).
  It fixes three Memory ceilings before any design existed: 1,048,576
  bytes of storage per issue, 16,384 characters of projection per launch
  and 1,000 ms of latency per launch. It lists the fixtures a
  Memory arm owes, and leaves open how a seeded lesson enters Memory
  unchanged (its §3.5). D9.5 answers that.
- **The Claude SDK helper exists** (#151) and declares its interaction
  capabilities; every `opencode-sdk` launch is refused until #152.
- **The line numbers #316 cites have moved.** `_invoke_phase` is at
  `engine.py:16740` (cited as :13111), the broker built with
  `pause_available=False` at `engine.py:16957` (cited as :12930),
  `SafeRoot` at `safefs.py:586` (cited as :590), and the whole-state write
  `save_state` at `state.py:2917` (cited as :2154).

### 1.3 What the code does at `9baed0a`

- **A step is one phase entry.** ADR 0012 D11.4 names it: one phase of one
  issue, PR, review round and execution attempt, over every command that
  re-enters it (`auto_recovery.StepKey`, `auto_recovery.py:547`). A step
  launches one agent at a time, and relaunches it only for a correction, a
  transient failure the adapter typed, or D11's one automatic relaunch.
- **One save precedes a launch.** `_invoke_phase` increments `attempt`,
  charges a relaunch's step, records the launch label and clears the kept
  reply, then saves (`engine.py:16941`). The entry observation (ADR 0004
  D4.4) is already in the state. Nothing else is persisted before the agent
  starts.
- **Every reply is a phase result.** `_check_reply` (`engine.py:17389`)
  imports the agent's objects strictly, parses the reply as the phase's
  `CONTROL_RESULT` (`parse_control_result`, `engine.py:17414`) and runs the
  phase's own checks of the candidate, comment or plan. No other kind of
  agent output exists.
- **A phase commits in its plan save.** Effect records saved `intended`
  with the completion context (ADR 0004 D4.6) are what recovery completes
  from, without relaunching. The transition save drops them.
- **Evidence lives in the state.** `ValidationRecord`s and the
  `ReviewCertificate` are strict, bounded fields of `state.json` (ADR 0006
  D11).
- **The nearest thing to Memory today** is `verification_failures`, the
  last 20 redacted controller-side failures (`engine.py:4075`), which the
  replan prompt renders as untrusted text (`replan.py:164`). A producer's
  failed validation also reaches its next launch as one record
  (`prompts/validation_failure.md`, ADR 0006 D5.4). A correction quotes the
  end of the refused reply, held in memory and never persisted (#393).
- **The state is one file, saved whole.** `save_state` writes `state.json`
  through `SafeRoot.write_bytes` (`safefs.py:845`): a fresh inode, fsynced
  and published by name under a descriptor-relative walk, the directory
  fsynced after it. The loader is strict and bounded at 64 MiB
  (`MAX_STATE_FILE_BYTES`). The run log is audit output that no recovery
  reads, and the read context is a per-launch directory of untrusted,
  bounded, redacted files that the controller never reads back
  (`read_context.py`).
- **Two identities are called "run".** `run_id` is
  `af-<UTC stamp>-<6 hex>`, made once per run (`generate_run_id`,
  `engine.py:1309`). ADR 0008's `RunKey` is `(repository_id,
  issue_number)`, which names an issue's scheduler namespace across runs.
- **Each adapter starts a new session per launch,** by a flag or by
  construction (D14). Claude Code's and OpenCode's `extra_args` pass
  through, apart from Claude's integration flags (`providers.py:1238`), so
  a flag that resumes a session passes validation today.
- **No pause exists.** `durable_pause` returns `False`
  (`transitions.py:175`), and the broker is built with
  `pause_available=False`.
- **Under `sandbox.mode: disabled`** an agent runs as the operator and can
  rewrite the state directory, which carries no keyed integrity check
  ([docs/security.md, "Agents run as you"](../security.md#agents-run-as-you)).
  Under `required` it sees its read context read-only and the rest of the
  state directory not at all.

### 1.4 Terms

- **Task:** one unit of agent work inside a step: a role, its inputs, and
  the contract its result is held to.
- **Attempt:** one physical launch for a task, named by its `AttemptID`
  (D2.1).
- **Producing task:** the task whose accepted result is the phase's
  result. A step has one open producing task at a time.
- **Receipt:** the controller's binding of what an attempt delivered to
  the attempt it launched (D4.1).
- **`PhaseResult`:** today's `CONTROL_RESULT`, the one input to a phase
  transition. **`TaskResult`:** the typed object a role's parser makes of
  an attempt's reply, a claim.
- **The step's current candidate:** the candidate the controller last
  recorded for the step from a producer's checked output (D7.3), never
  the live worktree.
- **Controller-observed fact:** a record the controller wrote from a typed
  source it holds as evidence, citing that source. **Model-proposed
  claim:** anything an agent wrote.
- **Memory record, Memory revision, projection:** D9 and D12.

## 2. Decisions

### D1. Three concerns, three owners, and no substitution

- **Task state** is controller state, in `state.json`, below the phase.
  The engine owns it. The reducer (#332) proposes; the engine decides.
- **Memory** is advisory data the controller owns and stores. It informs a
  launch's prompt and nothing else.
- **Capability and scope** are configuration plus the adapters' typed
  declarations, checked before anything of a step is saved or launched.

None stands in for another:

1. A Memory record is never task state, phase state, evidence, a
   certificate, a budget or an authorization. No code that decides a
   phase, a task's state, a write, a permission, a budget or a merge reads
   Memory. #317 adds the import rule that holds this, as ADR 0011's
   classification is held to its sinks.
2. Task state is never inferred from Memory, the run log, the read context
   or an agent's prose.
3. GitHub stays the source of truth for what it holds. Neither task state
   nor Memory keeps a copy of it as a fact (D9.3).

### D2. Identities

| Identity | What it is | Persisted |
|---|---|---|
| `RunID` | `AutoForgeState.run_id`, `af-<UTC stamp>-<6 hex>` (`generate_run_id`) | yes, today |
| `StepKey` | phase, issue URL, PR URL, review round, execution attempt (`auto_recovery.StepKey`) | today in the recovery journal; derived for every step |
| `TaskID` | `(RunID, StepKey, ordinal)`: the task's ordinal in its step, from 1, allocated when the task is proposed and never reused in the step; a producing task admitted after an `unblock` into the same step takes the next (D3) | no: nothing names a task in the single-agent path; persisted by #332, with the step's ordinal counter |
| `AttemptID` | `(RunID, launch_seq)`: the run-wide launch ordinal the attempt's launch-intent save allocates (D2.1). The receipt names the task the attempt belongs to | `launch_seq` is saved from protocol 20 (#317); `state.attempt` is a counter, not an identity |
| role | a closed enum (D7.1) | derived in the single-agent path; persisted by #332 |
| candidate | base: the revision the entry observation required (ADR 0004 D4.4); candidate: a `RemoteCandidate` (ADR 0006 D2.1), the commit the phase's candidate check accepted (D2.4) | yes, today, where ADR 0006 records it |
| phase | `Phase` | yes |
| policy version | `PolicySnapshot`: the controller version, `prompt_version`, the recorded configuration (`config_fingerprint`, #219), and the validation `policy_digest` and each `definition_digest` (ADR 0006 D8.1, D3.5) | its parts today; its digest by #332 |
| provider profile | profile name, provider, model and effort, as each launch's run-log record holds them, and D14's fresh-context proof | the run log today; the proof's kind with the task, by #332 |

- **`RunID` is not ADR 0008's `RunKey`.** A `RunKey` outlives runs. Tasks
  and Issue Working Memory belong to one run (D9.1).
- **Every identity comes from values the controller holds,** never from an
  agent's report. The controller binds a reply to the attempt it launched
  (D4.1). Where a role's schema asks the agent to echo a value (the
  producer's `phase`), the echo is a cross-check, and a mismatch is
  refused (D4.3).
- **None is a GitHub object.** No task is a GitHub sub-issue, and the
  EPIC's graph is unchanged.

**D2.1. An attempt's identity survives every re-entry of its step.** An
`AttemptID` names one launch of the run, and no other.

- **Why not `state.attempt`.** It counts the launches of one phase entry,
  for the correction and transient bounds, and every phase entry resets
  it to 0, `unblock` included (`engine.py:9495`). ADR 0012's `StepKey` is
  deliberately the same when `unblock` re-enters the same phase, so that
  D11's allowance is not refilled. `(StepKey, state.attempt)` therefore
  names two launches after such an `unblock`, and a receipt, a Memory
  record or a late reply of the first would bind to the second.
- **Why not `step_count`.** It is unique per launch today, because #434
  charges every physical launch a step. But it is a budget. It also counts
  steps that launch nothing, and its rules belong to ADR 0012 and #434. An
  identity read from it would break silently the day a budget rule
  changes, a launch class charged otherwise for example. The run log's
  step directory is unique too, but it is audit output that no decision
  reads.
- **Allocation.** `launch_seq` is a run-wide integer in `state.json`, 0
  when the run starts. Each launch-intent save (D5.1) sets it to one more
  than its last value, and the launch that follows is
  `AttemptID = (RunID, launch_seq)`. Nothing else changes it: not
  `unblock`, `resume`, a phase change, an issue switch
  (`reset_for_new_issue`) or a crash. A new run (`run --force`) has a new
  `RunID`, so its counter starting again collides with nothing. Under
  #319, every attempt of every task, read-only ones included, allocates
  from the same counter in its own intent save. AttemptIDs are therefore
  unique across a step's tasks too, and ordered by their intent saves.
- **The save spends it.** The value is computed in memory before the
  intent save, so the read context and the prompt can name it, and only
  that save spends it. A crash before the save launched nothing (W1, D11),
  so the next intent save allocates the same value. A file rendered with it
  before the crash names no launch, and nothing reads a read-context file
  back. After the save, the value names exactly one launch, even if its
  process never started (W2). Such a launch has a spent `AttemptID` and no
  receipt.
- **`state.attempt` keeps its meaning.** It is the launches of this phase
  entry, which the correction and transient bounds count and D3's derived
  states read. It names nothing.
- **Migration.** `launch_seq` is a new field (D19.2), added by the first
  child that persists an attempt's identity: #317, whose records name the
  attempt that created them (D9.2), or #332 if it lands first. A file of
  the previous protocol loads with `launch_seq` equal to its `step_count`.
  Every launch it made was charged a step before it started (#434), so the
  run made at most that many launches, and every `AttemptID` allocated
  after the load is greater than that number. Nothing persisted before the bump
  names an `AttemptID`, so nothing else is rewritten. A reply kept under
  ADR 0012 D12 keeps its existing binding, and the first launch after the
  load clears it, as today. A file labelled older that carries the field
  is refused, and a file of the new protocol must carry it.

*Regression scenario.* This is the sequence that makes
`(StepKey, state.attempt)` name two launches.
`test_adr_0015_attempt_repeats_after_an_unblock_into_the_same_step` checks
the facts it rests on against today's code. It runs an `ANALYZE_EXECUTE`
step that its agent's result blocks, and an `unblock` back into the same
step. The step keeps its `StepKey`, both launches have `attempt` 1, and
`step_count` grows:

1. `FIX` in review round 2, with step key K. The intent save leaves
   `attempt` 1 and allocates `launch_seq` 7, so the launch is `(R, 7)`.
   The agent commits, then the launch ends without a usable reply. No
   relaunch is left, and the step enters `BLOCKED`.
2. `unblock` re-enters `FIX` through `UNBLOCK_TARGETS`. `attempt` is 0
   again and the step key is still K. D3 cancels the open task
   (`unblocked`), and the re-entry admits a new producing task, ordinal 2.
3. The next intent save leaves `attempt` 1 again and allocates
   `launch_seq` 8, so the launch is `(R, 8)`. The worktree the first
   launch left is read (#187) and imported strictly before it starts, as
   any leftover is (D5.4).
4. Any receipt, Memory record or late reply naming `(R, 7)` is not
   `(R, 8)`'s. A receipt is refused by the binding check (D4.3), and the
   projection of the second launch names `(R, 8)` (D12). Under
   `(TaskID, state.attempt)`, both launches would have been `(R, K, 1, 1)`.

#317 and #332 each owe this sequence as a test with fakes (§4.2).

### D3. The task lifecycle

A task is in one of seven states. `ACCEPTED` and `CANCELED` are terminal.

| From | Event | To |
|---|---|---|
| `PROPOSED` | the controller's admission checks pass (D7.2) | `ADMITTED` |
| `PROPOSED` | an admission check refuses it (`not-admitted`) | `CANCELED` |
| `PROPOSED` | its step ended while it was open (D3.1: `step-concluded` or `unblocked`) | `CANCELED` |
| `ADMITTED` | its launch-intent save (D5.1), which allocates its attempt's `AttemptID` | `RUNNING` |
| `ADMITTED` | an input its role's rule names moved (`stale-input`, D7.3) | `CANCELED` |
| `ADMITTED` | its step ended while it was open (D3.1: `step-concluded` or `unblocked`) | `CANCELED` |
| `RUNNING` | the launch ended with a reply the controller holds: the receipt (D4.1, stage 1) | `VERIFYING` |
| `RUNNING` | the launch ended with no reply, or a crash left its outcome unknown | `RETRYABLE` |
| `RUNNING` | an input its role's rule names moved, and the controller stopped the launch (`stale-input`, a read-only task, D7.3) | `CANCELED` |
| `RUNNING` | its step ended while it was open (D3.1: `step-concluded` or `unblocked`); its process is stopped after the save | `CANCELED` |
| `VERIFYING` | the parse and every check passed, and the acceptance save holds it (D4.1, stages 4 and 5): for the producing task, the save that concludes the step from its result, its phase plan's or, for a result that stops the step with nothing planned, its stop save (D4.6) | `ACCEPTED` |
| `VERIFYING` | the parse or a check refuses the result (D4.1, stages 2 and 3) | `RETRYABLE` |
| `VERIFYING` | an input its role's rule names moved before the acceptance save (`stale-input`) | `CANCELED` |
| `VERIFYING` | its step ended while it was open, before its acceptance save (D3.1: `step-concluded` or `unblocked`) | `CANCELED` |
| `RETRYABLE` | a bounded relaunch: a correction, a transient retry, D11's automatic relaunch, or the operator's `resume` | `RUNNING` |
| `RETRYABLE` | an input its role's rule names moved (`stale-input`) | `CANCELED` |
| `RETRYABLE` | its step ended while it was open (D3.1: `step-concluded` or `unblocked`) | `CANCELED` |

`RETRYABLE` means a relaunch would be legal by the existing rules, not that
one will happen: with no bound left, the step stops through its existing
sink and the task waits in `RETRYABLE` for the operator. A `CANCELED`
task's result is never accepted or used, whatever it later turns out to
have been. What a canceled mutating task left in the worktree is the next
launch's starting state, read (#187) and imported strictly like any
leftover (D5.4), never its result.

**D3.1. A step that ends cancels every task still open in it.** A step
ends in one of three saves. Each save cancels every task of the step that
is not yet terminal, in whatever state it is and whatever its inputs:

1. **The step concludes** from its producing task's accepted result. The
   save that accepts the result, its plan's or its stop's (D4.1, stage 5;
   D4.6), cancels every other open task (`step-concluded`). That includes
   a read-only task pinned to the unchanged current candidate. None of its
   D7.3 inputs moved, but its step has its result, and nothing left in the
   step would read another.
2. **The entry's reconciliation routes the phase elsewhere** (a push by
   someone else turns `FIX` into `REVIEW`). The save that records the new
   phase cancels every open task of the step, the producing one included
   (`step-concluded`). No result was accepted.
3. **`unblock` re-enters a phase** from the step's `BLOCKED`. The save that
   records the re-entry cancels every open task of the blocked step
   (`unblocked`).

A stop the controller makes on its own, not one a result asked for, ends no
step by itself. Its open tasks keep their last state. No receipt is saved
after the stop, and their processes end with the invocation, as every
process an agent starts does. A task left `RUNNING` on disk then reads as
`RETRYABLE` (D5.4). `unblock` cancels these tasks (item 3), and a `FAILED`
run launches nothing again.

Whether a step waits for outstanding read-only work before it concludes is
#332's policy: the reducer proposes `Conclude` or `Wait` (D6). Once the
conclusion is saved, nothing waits for that work.

- **The save is the boundary.** A task is `CANCELED` from the save that
  ends its step, and nothing it delivers after that save is decided on. A
  reply that arrives later binds to a canceled task and is discarded and
  logged (D4.3). It is never saved as a receipt, parsed for Memory
  proposals, or read by the reducer or the plan. A receipt saved before
  that save but not yet accepted (`VERIFYING`) is canceled with its task,
  and its result is never accepted or used. A read-only task already
  `ACCEPTED` stays `ACCEPTED`, and the reducer's input says which
  candidate its result was about (D7.3).
- **Processes are stopped after the save.** The engine stops the process
  of every task the save canceled while it was `RUNNING`, as it stops a
  stale read-only launch (D7.3). It does so before the step goes on: before
  a plan's effects, the next phase's entry or the re-entered phase's first
  launch. Stopping a process decides nothing, because the save already
  did. A crash between the save and the stop leaves no read-only process
  behind: such a task runs only enforced (D7.4), and an enforced launch
  dies with the controller (bwrap's `--die-with-parent`, or ADR 0013's
  death-watch for a container). D5.5's survivor check still runs before
  any later launch.

*Scenarios.* #332 and #319 owe these as tests with fakes (§4.2).

1. **A conclusion with read-only work outstanding.** The worker's task is
   accepted with its plan. Ideator I1 is `RUNNING` and verifier V1 is
   `VERIFYING`, both pinned to C2, which is still the step's current
   candidate, so none of their D7.3 inputs moved. The plan save cancels
   both (`step-concluded`), and the engine then stops I1's process. A
   reply from I1 that arrives later binds to a canceled task and is
   discarded. V1's parsed result is never accepted. Neither reaches the
   reducer, Memory or the plan. The same holds when the producing task's
   result stops the step (D4.6).
2. **A crash between that save and the stop.** The next command finds I1
   and V1 `CANCELED`. I1's process did not outlive the controller, D5.5's
   survivor check runs before any launch, and a reply that turns up is
   discarded.
3. **Reconciliation and `unblock` with tasks open.** A save that routes
   the phase elsewhere cancels every open task, the producing one included
   (`step-concluded`). An `unblock` cancels every open task of the blocked
   step (`unblocked`), in each state that is not terminal.

**A terminal task is never launched again.** A step that launches again
after its producing task is terminal admits a new producing task, with the
next ordinal. That happens after an `unblock` into the same step, where
the producing task is either `ACCEPTED` (its result asked for the stop) or
canceled by the `unblock` (D3.1), and after a plan that a later read
voided, so that the step asks again (ADR 0004 D4.7's `UPDATE_EPIC`
rejection). Every attempt has its own `AttemptID` either way (D2.1).
Corrections, transient retries and D11's relaunch are further attempts of
the same task.

**Persisted and derived.** The single-agent path persists nothing new
until #332 keeps its receipts (D5.3). Every state of its producing task is
read from fields the state already holds. `PROPOSED` never occurs, because
the phase's entry admits its task by construction. No `TaskID` is needed,
because nothing in that path names a task (D2):

| State | How it is read in the single-agent path |
|---|---|
| `ADMITTED` | an agent phase with `attempt == 0`: the entry admitted its producing task, the first one or, after an `unblock` into the same step, a new one |
| `RUNNING` | in the process that launched it: `attempt >= 1` and the launch under way |
| `VERIFYING` | in that process: the reply from its receipt to the plan save (`_check_reply` parses and checks it, then the validation gates run); on disk: a `delivered_reply` not yet applied and, once #332 keeps every receipt (D5.3), a receipt not yet accepted |
| `ACCEPTED` | its result taken as the phase's: the phase's plan saved (`completion_context`), or the phase moved on from it through `decide_next_phase`; or its stop saved, when the result's own `status` is `blocked` or `failure` and nothing is planned: the phase `BLOCKED` or `FAILED` with the stop code `agent-blocked` or `agent-failure`, which the engine saves itself (D4.6; `decide_next_phase` never enters either) |
| `RETRYABLE` | on disk: `attempt >= 1`, no plan and no kept reply. A crash during `RUNNING` reads the same, and both resolve the same way: the entry's reconciliation, then a charged launch |
| `CANCELED` | the phase moved on without an accepted result: the entry's reconciliation routed it elsewhere (a push by someone else turns `FIX` into `REVIEW`), or `unblock` re-entered the phase, which restarts `attempt` at 0 for the new task |

Under #319 a step holds tasks the existing fields cannot describe, so
#332 persists each task's state, attempts and receipts (D19).

**Where a task state mirrors the workflow:**

| Workflow | Task | How the two relate |
|---|---|---|
| an agent phase is entered | its producing task is `ADMITTED` | the entry admits it; under #319, the other tasks are admitted one by one |
| the phase moves on from its result, to its next phase or to the stop the result asks for | the producing task is `ACCEPTED`; every other open task is `CANCELED` (`step-concluded`, D3.1) | a transition is still `decide_next_phase`'s; a stop the result asks for is the engine's stop save, as today (D4.6) |
| the entry's reconciliation routes the phase elsewhere | every open task is `CANCELED` (`step-concluded`, D3.1) | no result was accepted; the next phase's entry admits its own task |
| `BLOCKED` | none | not a task state. A stop the result asked for concluded the step (D3.1). After a stop the controller made on its own, open tasks keep their last state, and a `RUNNING` one reads as `RETRYABLE`. `unblock` re-enters a phase through `UNBLOCK_TARGETS`, which cancels every open task of the blocked step (`unblocked`). The phase re-entered then admits a new producing task, in the same step (the same `StepKey`, new `AttemptID`s, D2.1) or in another |
| `FAILED` | none | terminal; no open task is resumed |
| `AWAITING_INPUT` | none | reserved for #126 (D15) |

*Rejected:* `BLOCKED` and `FAILED` as task states. A task-level `BLOCKED`
would be a second stop the operator has to leave, with no route of its
own, and could disagree with the phase's. A task cannot proceed only
because its step cannot, and the step's stop is the workflow's.

### D4. `TaskResult` and `PhaseResult`

**D4.1. Five stages, in order.** Each stage is separate. None is skipped,
and none is inferred from a later one. A task is `VERIFYING` from stage 1
until stage 4 accepts it or stage 2 or 3 refuses it (D3).

1. **Receipt.** When a launch ends, the controller binds what it holds
   to the attempt it launched: the reply, how the launch ended, and the
   objects the launch left. The binding is the `AttemptID`, the task, the
   role and the input fingerprint, all from the launch-intent save, none
   from the reply's text. The strict import of those objects (ADR 0005
   D6.5) belongs here. It reads what the launch delivered, not what the
   reply says, and `_check_reply` runs it before the parse; tampering it
   finds blocks the run, as today. Nothing else is decided yet. A launch
   that ended with no reply has no receipt and is `RETRYABLE`.
2. **Parse.** The role's parser makes a typed `TaskResult` of the reply,
   strictly, bounded and redacted (D4.5), or refuses it. A refusal is a
   parse outcome, not a verification: the task is `RETRYABLE`, and the
   correction quotes the refused reply as today (#393). For the producing
   task the parser is `parse_control_result`, unchanged.
3. **Verification.** The controller runs its checks of the parsed result,
   and each check is its own. For the producing task they are the phase's
   checks of the candidate, comment or plan, which `_check_reply` runs
   after its parse. The validation gates follow (ADR 0006 D5). For another role they are its role's checks: the
   binding (D4.3), D7.3's invalidation rule, and D7.6's reproduction.
   Records are saved as they are made (ADR 0006), and so is the Memory
   derived from them (D11). A refusal makes the task `RETRYABLE`.
4. **Task acceptance.** Once every check has passed, the task is
   `ACCEPTED` in the next save that records it, after D7.3's last
   comparison. A task other than the producing one is accepted in a save
   of its own (#332). Its result then becomes an input of later tasks and
   of the reducer, never a phase result.
5. **Phase acceptance.** The producing task's accepted `TaskResult` is
   the `PhaseResult`. Its stage 4 is this stage's save, the one that
   concludes the step from the result:
   - for a result that plans effects, the phase plan (completion context
     and `intended` effects);
   - for a result whose own `status` stops the step with nothing planned,
     the stop save (D4.6).

   That one save also holds the task `ACCEPTED` and the result's Memory
   proposals (D9.8). So no crash leaves a producing task accepted without
   its plan or its stop, or either without its task. The effects and the
   transition follow a plan, as today. A stop has neither.

In the single-agent path, `_check_reply` spans stages 1 to 3: the strict
import, then the parse, then the phase's checks. The plan save, or a stop
result's stop save, is stages 4 and 5. A reply delivered before a failed
launch end is kept after its checks (ADR 0012 D12). That kept reply is a
saved receipt still at stage 3, re-checked after a restart. No launch path
changes.

**D4.2. Another role's reply is never parsed as a phase result.** It is
framed with markers of its own, and each parser refuses the other's
markers in its input. A `CONTROL_RESULT` block in an ideator's reply is
therefore refused, not read. #332 defines the framing.

**D4.3. A result is bound by the controller, not by its text.** The
receipt's binding comes from the launch-intent save. An agent has no value
of its own that the controller could trust to name a task or an attempt,
and the reply is asked for none. Where a role's schema asks for an echo, it
is checked against what the controller holds, and a mismatch is refused.
The parser refuses a wrong `phase` from the producer. The phase's checks
refuse a wrong `previous_head_sha` in a `FIX` result. A receipt whose task
is `CANCELED`, or whose `AttemptID` is not its task's last attempt, is
discarded and logged. That covers a reply that arrives after its task was
canceled, a read-only task's reply that arrives after its step ended
(D3.1), and a reply of a launch from before an `unblock`.

**D4.4. What a `TaskResult` holds is closed per role.**

- **The producing task's result is the phase's `CONTROL_RESULT`,** in the
  schema the phase holds today, `phase` and `status` included.
  - Its `phase` is the binding echo of D4.3: `parse_control_result`
    refuses any value but the launched phase ("phase mismatch"), so the
    field names the phase the controller launched and never requests
    one.
  - Its `status` and its other fields are claims the phase's existing
    routing and checks read, as today. That includes a result asking for
    `BLOCKED` or `FAILED`, and what `FIX` asks the controller to publish
    (follow-ups, acceptance and operator actions, a PR description).
  - The candidate is read from the worktree, and its SHA field is checked
    against it.
  - What the controller writes is still decided by the phase's plan, and
    the transition by `decide_next_phase`.
  - Agent Memory proposals, if the MVP takes them, are one bounded
    optional field more, a prompt and schema change (D9.8, D19).
- **Every other role's result** carries what its role may claim and
  nothing else: a task proposal (a Manager's, D7), a finding or
  counterexample claim (D7.6), an approach (an ideator's), and Memory
  proposals (D9.8). It has no `phase` and no `status` that routes the
  workflow. It names no candidate, effect, GitHub write, merge,
  permission, budget change or stop code. A field outside its schema is
  refused at parse.

**D4.5. Bounded and redacted at parse,** as `CONTROL_RESULT` is today: a
block bound and per-field bounds, enforced by refusal, never by a silent
cut.

**D4.6. A result that stops the step is accepted in its stop save, with no
plan.** A producing task's result whose own `status` is `blocked` or
`failure` asks the controller to stop the step. Today the engine applies
it in one save (`engine.py:6587`). The save sets the phase to `BLOCKED` or
`FAILED`. It takes the agent's message, redacted and bounded, as the block
reason, and the stop code `agent-blocked` or `agent-failure` (ADR 0011).
It writes no completion context and no `intended` effect, and this record
adds none. The stop publishes nothing, so recovery has nothing to complete
and a plan would order no effect. `decide_next_phase` never enters
`BLOCKED` or `FAILED`; the engine does, as today, so no route changes.

- **The stop save is the acceptance save.** For such a result it is stage
  5. It holds the stop, the task `ACCEPTED`, every other open task
  `CANCELED` (`step-concluded`, D3.1) and the result's Memory proposals
  (D9.8), all in one save. Stage 3 for such a result is what the phase
  checks of a stop today: the parse, the PR owner check (ADR 0004 D9.4a)
  and the checkout check. No candidate check runs, because nothing is
  published.
- **Its Memory proposals are admitted,** as any accepted result's are
  (D9.8). An agent that stops has often learned what the next launch
  needs: why it could not go on. The launch after an `unblock` is in the
  same run (D9.1), and its projection shows them as proposals. A `failure`
  ends the run. Its proposals stay in the state, where only a later export
  reads them (D17).
- **A blocked `FIX` that offers a partial fix (#433)** is accepted in its
  plan save when the controller plans its publication, as a success is. It
  is accepted in its stop save when the controller stops it with nothing
  planned. Either way it is one save.
- **Crash windows.** Before the stop save, the reply is where any reply
  is. After a clean exit it is held in process and lost to a crash (W4,
  until #332's receipt closes it, D5.3). After a failed end it is kept
  after its checks, re-checked after a restart, and then makes the stop
  save (W5, ADR 0012 D12). After the stop save, the stop stands, with
  nothing to complete and no relaunch, and the proposals are in the
  state (W7). `unblock` admits a new producing task (D3), and the
  accepted one stays `ACCEPTED`.

`test_adr_0015_a_stop_result_is_applied_in_one_save_with_no_plan` holds
today's half of this. For an ordinary `blocked` and a `failure` result, the
first save after the launch is the stop with the agent's code, and no save
of the step holds a plan or an effect.

### D5. Launch intent and the attempt protocol

**D5.1. Before a launch, one save.** It holds the attempt number, the step
charge, the launch label and the entry observation, as today. From D19.2
on it also allocates the attempt's `launch_seq` (D2.1). Under #319 it also
holds the task, its role, its profile and its input fingerprint (D7.3).
Nothing is launched before that save is durable.

**D5.2. After a launch, a reply is kept before anything follows from it,
in two cases.** A reply delivered before a failed end is kept today, after
its checks (ADR 0012 D12). Once #332 lands, every attempt keeps its
receipt: the single-agent producer's (D5.3), and every task attempt's
under #319. The receipt is kept after its parse (D4.1, stage 2), in the
save that first records the attempt on disk as past `RUNNING`. It holds:

- its binding;
- the reply's digest;
- either the parsed, bounded and redacted `TaskResult`, or the code of
  its parse refusal.

It never holds the raw reply. A restart re-verifies a parsed receipt and
launches nothing for it. A refusal's receipt leads to the correction the
refusal earned. The correction carries no quote of the reply, because the
quote is never persisted (#393).

**D5.3. The clean-exit window: closed by #332, for both paths.** Today a
clean exit's reply to a phase task is checked in process, and not saved
before its checks. A crash in that window costs one charged relaunch (W4,
D11). The maintainer decided that #332 closes it for the single-agent path
too (§5, answer 6). A crash there is an expected failure: a closed laptop,
or `Ctrl-C` during a long validation. #319 needs receipts anyway, and one
crash semantics then serves both paths.

- **What #332 keeps.** Every attempt keeps its receipt as D5.2 says, the
  single-agent producer's included. It is saved after the parse, before
  the phase's checks, the validation gates and the save that accepts the
  result (D4.1).
- **A restart.** It re-verifies a parsed receipt and launches nothing: it
  runs the phase's checks again, reusing the same candidate's
  `ValidationRecord`s (ADR 0006). A refusal's receipt leads to its
  correction, without the quote (#393).
- **What it extends.** The receipt extends `delivered_reply`'s contract
  (ADR 0012 D12) from a failed end's reply to every reply. #332 decides
  how the two share a field. Its binding is the `AttemptID` (D2.1), so it
  needs `launch_seq` (D19.2).
- **Its bump.** It takes a protocol bump of its own, which #332 announces
  on #23 (D19.5).

The Memory MVP does not wait for it. Until #332 lands, W4 stays as it is.

**D5.4. An unknown outcome is never guessed.** A task found `RUNNING` after
a crash has its attempt charged and its result lost. No recovery reads
`stdout.log` or `events.jsonl` for it. Before a mutating task is launched
again, its worktree is read (#187) and its objects imported strictly (ADR
0005 D6.5), as today. A read-only task (D7.4) wrote nothing, and is
launched again within its bound or canceled.

**D5.5. No second concurrent launch.** The per-issue lock (ADR 0008's L0
and L2) is held across the launch. A relaunch is refused while the launch
before it may have left a process running (`may_have_survivor`,
`orphans_unchecked`), as today. Under `disabled`, a process left behind by
a controller killed with `SIGKILL` is the residual ADR 0007 D7.2 names.

**D5.6. Resource records.** What an attempt consumes, and where it is
recorded:

| Resource | Recorded in | Enforced |
|---|---|---|
| physical launches | `step_count` (run), `attempt` (step), D11's relaunch count | yes, before the launch |
| runtime | the run log's execution record; each profile's idle and maximum limits | per launch; totals are reported, not enforced |
| money | `cost_micro_usd` where the provider reports it | never: an estimate, not a budget (ADR 0012 D9) |
| storage | Memory's size (D9.6); each read-context directory's bound | at the write |
| attempts | `attempt`; under #319, each task's attempts | by D7.2's bounds |

### D6. The reducer's input and output (#332)

This record ratifies the types at this level; #332 owns the encodings.

```text
ReducerInput                 frozen and typed; no agent text beyond bounded typed fields
  run_id, step               RunID and StepKey (D2)
  tasks                      per task: id, role, state, prerequisites, input fingerprint,
                             its AttemptIDs, how its last attempt ended (typed), the
                             D4.1 stage its last receipt reached, receipt digest
  policy                     PolicySnapshot (D2), the role table, D7.2's bounds
  capabilities               per routed profile: D14's proof, the interaction
                             capabilities (ADR 0007 D5.3), whether its launch is
                             enforced (ADR 0005), whether D7.4's read-only class exists
  verification               the step's current candidate (D7.3), candidate checks,
                             import outcomes, the ValidationRecords of the candidates
                             in play, and each one's independent outcome under the
                             frozen plan: verified, failed, inconclusive with its cause
                             (for checker-altered: the changed paths and the replaced
                             tree's run), or not-run (D7.5)
  budgets                    step_count and workflow.max_total_steps, the step's
                             attempts and D11 allowance, launches per role

ReducerOutput                one or more ActionProposal
  AdmitTask(spec) | Launch(task, profile) | Cancel(task, reason)
  | Accept(task, attempt) | Retry(task, category) | Conclude(producing task)
  | Stop(StopReasonCode) | Wait
```

1. **Pure and total.** No I/O, clock, randomness or environment; the same
   input gives the same output; every input gives at least one proposal,
   `Wait` and `Stop` included.
2. **Memory is not an input** (D1), and nor are the run log, the read
   context, GitHub's text or an agent's prose.
3. **Proposals, not effects.** The engine executes a proposal only after
   its own checks (the step budget, the survivor check, ADR 0004 D9.4a's
   owner check, the sandbox gate, D18's capability check), and may refuse
   it, which changes nothing. No GitHub or git write is a proposal: K1 to
   K13 stay the phase acceptance path's.
4. **A stop goes through the existing sinks** with an existing code (ADR
   0011). A new code is an ADR 0011 amendment.

#332 decides the field encodings, the persisted schema (with #23), the
order among admitted tasks, the fixed-role table's contents and the
defaults of D7.2's bounds. It may route the single-agent path through the
reducer only if prompts, launches and effects stay identical, which §4.1's
tests hold.

### D7. Roles, the step's task graph and one mutating worker

The Memdoor addendum of #316, as the contract #319 implements.

**D7.1. Roles are a closed set, and none is an authority.** In the
single-agent path: `producer` (`ANALYZE_EXECUTE`, `FIX`,
`REPLAN_REEXECUTE`), `reviewer` (`REVIEW`) and `selector`
(`UPDATE_EPIC`). Under #319, in a producing phase: `manager`, which
proposes tasks; `ideator`, which proposes approaches; and `verifier`, which
claims defects. Acceptance of the work, every typed effect, every
transition, every candidate identity and the merge stay the controller's.

**D7.2. Admission.** A task is admitted only when:

- its role is in the policy's role table for the phase;
- its prerequisites are tasks of the same step and the graph stays
  acyclic;
- the step's task count, the count per role, the read-only tasks running
  at once and the attempts stay within their bounds;
- its profile proves what its role needs (D14, D18);
- its inputs can be fingerprinted now.

A Manager's proposal stays `PROPOSED` until then. A Manager cannot admit,
cancel, accept or order a task.

**D7.3. A fingerprint records what an attempt was launched with.
Staleness is a rule per role.**

- **The fingerprint is immutable.** It is saved in the attempt's
  launch-intent save and never rewritten. It holds:
  - the step's base and the entry observation;
  - the candidate the attempt starts from (commit and tree): for the
    worker, the step's current candidate, or the base when there is
    none; for a read-only task, the candidate it is pinned to;
  - the frozen plan's digest (D7.5);
  - the read context's digest;
  - the Memory revision and digest of its projection;
  - the prompt template's digest;
  - the receipts of its prerequisites, each `ACCEPTED` before it was
    admitted.

  A relaunch is a new attempt with a fingerprint of its own. The
  fingerprint answers what the attempt was shown, and receipts and the
  reducer cite it. It is not by itself a staleness test: a value that
  moves after the launch matters only where the role's rule below names
  it.
- **The step's current candidate is controller state.** It is the
  candidate the controller last recorded for the step from a producer
  attempt's output: imported strictly and accepted by the phase's
  candidate check (ADR 0006 D2.4). It is set in a save, never by a commit
  landing in the worktree. While the worker's attempt runs, its commits
  change the worktree, not the current candidate.
- **What makes a task stale, by role:**

  | Role | Stale when, against the current values | Never stale because of |
  |---|---|---|
  | producer (the mutating worker) | the step's base or its entry observation moved (the entry's reconciliation finds it, as today: a push by someone else turns `FIX` into `REVIEW`), or the frozen plan's digest differs | its own commits; the current candidate moving to its own output; the records of its candidate's verification; any Memory revision, including records derived from its own verification; a read-only task's result |
  | read-only (manager, ideator, verifier) | the step's current candidate is no longer the one it is pinned to; the base or the entry observation moved; the frozen plan's digest differs | any Memory revision; the records of its pinned candidate's verification; the worker's commits to the live worktree before the controller records them |

- **Memory never makes a task stale.** Memory is advisory (D1), and no
  task's state is decided on it. The revision in a fingerprint is
  provenance. A record retracted after a launch disappears from the next
  launch's projection (D12), and the claim an attempt returns is verified
  on its own terms (D7.6), not on what the attempt was shown.
- **Comparison points.** The engine holds the issue lock (D5.5) and
  compares a task's stale-making inputs with the checkpoint's current
  values at three points:
  1. at admission;
  2. in the launch-intent save;
  3. in the acceptance save (D4.1, stage 4).

  A move found at point 1 or 2 cancels the task (`stale-input`), and
  nothing is launched. A move found at point 3 cancels it instead of
  accepting it. Between the points, a running read-only launch whose
  pinned candidate stops being current is stopped (`RUNNING` to
  `CANCELED`). That saves a launch's cost and is not what makes the rule
  hold: point 3 holds whenever the launch ends.
- **Concurrency rules.** Only a save moves the step's base, its current
  candidate or its frozen plan. The current candidate moves only to a
  producer attempt's checked output. The save that moves it also cancels
  every open read-only task pinned to the previous candidate, so the
  cancellation is never apart from the move on disk. A read-only result
  moves nothing but its own task's state. Read-only tasks read an
  immutable export of their pinned candidate's objects, never the live
  worktree (D7.4).

*Scenarios.* #319 and #332 owe these as tests with fakes (§4.2). B is the
step's base.

1. **A successful producer.** The worker's attempt is launched from B,
   shown Memory revision r3. While it runs it commits C1, then C2, and
   nothing it is compared on moves. It replies, and its reply is parsed
   (stage 2). The candidate check imports C2, and a save records C2 as
   the step's current candidate (stage 3). The current candidate moved
   from none to C2, but that is the producer's own output. The acceptance
   save compares B, the entry observation and the plan's digest, finds
   them unchanged, and accepts the task with its plan (stages 4 and 5).
2. **Its verification-derived Memory write.** The frozen plan's
   `ValidationRecord` for C2 is saved together with the layer 2 record
   derived from it, which makes Memory revision r4 (D11). The producer was
   shown r3, and r3 to r4 makes no task stale. Had C2 failed, the task
   would be `RETRYABLE`. Its relaunch would then be a new attempt whose
   fingerprint records r4 and starts from C2, while the first attempt's
   fingerprint still records r3.
3. **Genuinely stale read-only work.** Take scenario 2's other branch:
   C2 failed, and the worker's task is `RETRYABLE`. Ideator I1 is pinned
   to C2 and runs while the worker's relaunch runs. The relaunch's output C3
   passes the candidate check, and the save that records C3 as current
   also cancels I1 (`stale-input`). The engine stops I1's process. A reply
   from I1, if one arrives, binds to a canceled task and is discarded
   (D4.3). Ideator I2, also pinned to C2, replied and was verified before
   C3 was recorded. It was accepted in a save in which C2 was current, so
   it stays `ACCEPTED`, and the reducer's input says its result is about
   C2, not C3.

**D7.4. One mutating worker.** At most one task of a step writes the
worktree, and it is the producing task. Read-only tasks read an immutable
export of a pinned candidate, never the live worktree, and may run while
the worker does. A read-only result is accepted only while its pinned
candidate is still the step's current candidate (D7.3, point 3).

Read-only is enforced, not asked for. Read-only roles run in a launch class
of their own (an ADR 0005 amendment by #319, shaped like ADR 0012 D7's
advisor class): the pinned export read-only, no worktree, no git admin
directory, no state directory but their own read context, a private home.
Under `required` the launch must be enforced (`enforced: true`). Under
`disabled` it is refused before it starts, with no fallback to an
unisolated launch: a prompt that says "do not write" is logical isolation.
So multi-agent execution needs a Linux host that can enforce the sandbox,
and has no macOS support. The maintainer accepted this (§5, answer 4):
D7.3's staleness rules depend on read-only being enforced. macOS in CI
(#501) runs the suite and enforces nothing. A macOS-capable enforcing
backend, ADR 0013's container backend for example, lifts this without a
new decision.

**D7.5. Independent verification: a frozen plan, checker inputs from the
base, and outcomes that are not a pass.** Ordinary validation (ADR 0006)
is unchanged, and so is the single-agent path's. It runs the trusted
definitions in a byte-exact export of the candidate. That export holds
the candidate's own tests, configuration and runner hooks, all of which
the worker can edit. Independent verification is a second, stricter
question that #319 asks of a candidate: does it pass checkers the agent
could not touch? It has four outcomes, and only one of them counts.

1. **The plan is frozen at the step's entry.** It is the definitions of
   the trusted configuration (ADR 0006 D3) that declare themselves
   independent, a declaration #319 adds to ADR 0006's definitions. Each
   such definition binds three things in its `definition_digest`:
   - its argv;
   - its checker inputs;
   - its report contract.

   The plan's digest binds the set. The plan is read from configuration
   only (ADR 0006 D3.4), and no role can change it.
2. **Every checker input comes from the base.** A definition declares, as
   path patterns, every file its checkers read besides the code under
   test:
   - the tests and their fixtures;
   - every file the runner loads before or around them. For pytest that
     is every `conftest.py` at any depth, `pytest.ini`, `pyproject.toml`,
     `setup.cfg`, `tox.ini` and plugin modules;
   - the interpreter's start-up hooks (`sitecustomize.py`,
     `usercustomize.py`, `*.pth`);
   - every path its argv names.

   An independent definition that declares none is refused at load
   (D18).

   The base here is a revision no agent wrote. For `ANALYZE_EXECUTE` it
   is the default branch revision the entry observation required. For
   `FIX`, when it follows (D13), it is the PR's merge base, never the PR
   head, which an earlier agent wrote.

   The run's tree is the candidate's tree with every path that matches a
   pattern replaced from the base. A path the base holds keeps the base's
   bytes, and a path the base lacks is removed. So the candidate's edits,
   deletions and additions to its checkers never reach the run: a changed
   `pyproject.toml`, an edited test, or a new `conftest.py` that skips the
   base's tests. The candidate contributes only what matches no pattern,
   the code under test. The evaluation's protected paths (evaluation
   protocol §3.4) are the model. A candidate that changed any path
   matching a pattern, by an edit, a deletion or an addition, is
   **checker-altered**, and its run's tree is not its own tree. The
   controller records every such path, whatever the outcome.
3. **A pass is read from a report, never from the exit code alone.**
   - **The report.** Each independent definition names a report format
     the controller parses (#319 names the formats, JUnit XML first). The
     run writes it to a path the controller supplies outside the run's
     tree, and the controller reads it back.
   - **The baseline.** Before the step's first independent run, the plan
     runs once on the base itself, and the tests the base's report lists
     as passed are saved as the baseline.
   - **What passes.** A candidate's run passes when four things hold.
     Every definition exits 0. Every report parses. The reports hold at
     least one test. And every test in the baseline is reported as passed:
     none is missing, skipped, deselected, expected to fail or in error.
     A test that failed or was skipped on the base is reported, and not
     required.
4. **Outcomes.** `verified` and `failed` are said only of a tree that
   ran as it is. So a checker-altered candidate is never either, whatever
   its run shows.
   - `verified`: the candidate is not checker-altered, and every
     definition of a non-empty plan passed by item 3.
   - `failed`: the candidate is not checker-altered, and a definition or
     a baseline test failed.
   - `inconclusive`: anything else, with its cause, which is one of these:
     - `checker-altered`: the candidate is checker-altered. The outcome
       names the changed paths and what the run on the replaced tree
       showed: `passed` by item 3, `failed`, or the cause below that it
       met. A failure there may be the replacement's, and a pass is of a
       tree that is not the candidate's;
     - a report that is missing or does not parse, or an empty report;
     - a baseline test that is missing, skipped, deselected, expected to
       fail or in error;
     - a timeout, or an interrupted run (ADR 0006's marker);
     - a baseline that could not be recorded or lists no passed test.
   - `not-run`: the plan is empty. `validation.commands` is empty by
     default, and an empty set, which is complete evidence for ordinary
     validation (ADR 0006 D3.6), is never independent verification.

   Only `verified` counts, for D7.6 and D7.7. An `inconclusive` qualifies
   no candidate for either, whatever the replaced tree's run showed. That
   tree is not a commit, and it is never kept, concluded with or
   published. A checker-altered candidate stays the step's current
   candidate (D7.3). Ordinary validation (ADR 0006) and the review judge
   it as they judge any candidate, and whether the step concludes with it
   is #332's policy, never silent (D7.7).

   **How `checker-altered` is reported** (§5, answer 7). It is told apart
   from every other `inconclusive`, so that a legitimate change to
   `pyproject.toml` or a test does not read as a broken run:
   - the outcome record holds the cause, the changed paths as a sorted
     list, and the replaced tree's run. #319 bounds the list, and a cut
     leaves a count of the paths it omits, never silence;
   - the reducer's input carries the same for every candidate in play
     (D6);
   - the step's conclusion names its candidate's outcome with them;
   - `status` shows them for the step's current candidate.

   `not-run` and `inconclusive` are never rendered, logged or reported as
   a pass, and neither is a checker-altered candidate whose replaced tree
   passed.

   **The cost.** A candidate that adds or edits a test, as most do, is
   checker-altered. It is never `verified`, so D7.7 keeps only a
   candidate that changed no checker input. For `FIX`, a PR that already
   changed a checker input makes every candidate of the step
   checker-altered, because the base is the merge base. This is wider
   than the cost described in question 7, which the maintainer answered
   (§5): a candidate that *needs* a change to a checker input. The
   narrower rule let a candidate that changed only checker inputs be
   `verified` once the base's files replaced them: a skipping
   `conftest.py`, say, kept as verified with it in the commit. §4.2 forbade that, so review round 2 of PR #512 found
   the two contradicting. The replaced tree's run keeps the distinction
   answer 7 asks for. A later record may narrow the rule, with an
   argument that the narrower rule cannot be gamed.
5. **An empty plan with roles is refused.** A configuration that enables
   #319's roles while its plan holds no independent definition is refused
   at load (D18). Without one, no counterexample could ever be reproduced
   (D7.6) and no candidate ever kept (D7.7).
6. **The residual.** Code under test runs inside the process that tests
   it, and it can subvert that process at run time. It can patch the
   runner, rewrite its report, or exit 0 early. Taking the checker inputs
   from the base keeps the candidate's *files* out of the checkers. It
   cannot keep the candidate's *code* out of the process. The baseline
   catches the plain forms: a missing or skipped test, and an empty or
   absent report. It does not catch a forged report that lists the right
   tests as passed. `verified` therefore means that the candidate, which
   changed no checker input, passed the base's checkers as it is, and
   that every test that passed on the base was reported passing. It does
   not prove the candidate honest. The review (D7.9) stays the independent
   judgment, and the merge gate is unchanged. The maintainer accepted
   this residual for #319 (§5, answer 7): `verified` qualifies a candidate
   to be kept (D7.7) or to reproduce a counterexample (D7.6), nothing
   more. A runner-independent check would be a separate project and does
   not block #319.

**D7.6. A counterexample is a claim.** One an ideator or verifier reports
is unverified until the controller reproduces it, by running a definition
of the frozen plan on the pinned candidate in D7.5's tree and recording
the failure (`failed`). A test the agent wrote that agrees with its own
output proves nothing (the evaluation's `invented_expected_answer`
fixture), and D7.5's tree never runs it. An `inconclusive` or `not-run`
reproduction leaves the counterexample unverified. So no counterexample is
ever reproduced on a checker-altered candidate: a failure there may be the
replacement's.

**D7.7. The last independently verified candidate is kept, and stays
reachable.** The controller records the step's last candidate whose outcome
is `verified` (D7.5), with its records. A later candidate never erases it,
and no other outcome ever makes one. With no `verified` candidate, the step
has none to keep, and its conclusion says so. Whether the step concludes
with the kept candidate after a regression is #332's policy, and never
silent: the conclusion names its candidate and that candidate's outcome.

Its objects stay reachable by a ref under `refs/autoforge/`, as the
maintainer decided (§5, answer 5). Each `verified` candidate gets a ref of
its own, named for its commit: `refs/autoforge/<run>/verified/<commit>`,
with the `RunID` and the candidate's full commit id. These refs are the one
exception to AutoForge creating no local refs, and they are bounded:

- a ref is never under `refs/heads/` and is never pushed;
- the state records the kept candidate's ref, and `status` lists it, with
  the run's prefix `refs/autoforge/<run>/`, under which lies every ref
  AutoForge made for the run;
- AutoForge creates a ref, and never moves or deletes one. Cleanup is the
  operator's, as it is for the per-issue worktree;
- a candidate whose ref is missing or no longer resolves to it is reported
  as unreachable, and is never treated as kept.

**Keeping a new candidate never touches the kept one's ref.** The
controller creates the new candidate's ref, then makes the save that
records that candidate as kept (D11). The creation is create-only. A name
that is absent is created. A ref of that name that already resolves to the
candidate is reused: a crash's leftover, or the same commit verified again.
A ref of that name that resolves to anything else is left as it is, the
candidate is not kept, and the step reports it; the candidate kept before
stays kept. Neither step writes the kept candidate's ref. So a crash
between them leaves the state naming the candidate it kept before, whose
ref still resolves to it, and that candidate stays kept. The new ref is a
leftover the state does not name, and its target is never adopted (D11,
W12). Only something outside AutoForge, an operator or an agent writing
the shared refs, can make a kept candidate's ref missing or wrong.

Review round 3 of PR #512 found the earlier text unsafe. It moved one ref
per step to the new candidate before the save, so a crash between the two
left the state naming a candidate whose ref now resolved elsewhere. The
controller's own update then made its kept candidate unreachable. Answer 5
gave `refs/autoforge/<run>/verified/<step>` as an example and allowed the
ref to be updated. A ref per candidate keeps the answer's bounds and drops
the update. The name carries no `StepKey`, which holds URLs a ref name
cannot hold; the commit id alone is unique, and the state records the
step. The cost is one ref per `verified` candidate. Every candidate comes
from a charged launch, so the run's step budget bounds their number, and
the operator removes them by the run's prefix. Deleting a superseded ref
after the save that supersedes it would also be crash-safe, but AutoForge
would then delete refs, which answer 5 excludes.
`test_adr_0015_a_candidates_ref_is_created_and_never_moved` checks the git
behaviour this rests on: a create-only write refuses an existing name and
leaves it where it was, and two candidates' refs coexist under the run's
prefix.

Each ref keeps objects reachable and is trusted for nothing else. ADR 0004
§2.6 rejected a `refs/autoforge/` namespace because shared refs are
agent-writable, so such a ref could never be trusted. These are not
trusted either: which candidate is kept is the state's record of its commit
and tree, never a ref's target, and a ref's existence never makes a
candidate kept. #319 amends `AGENTS.md` and the relevant guide to name the
exception.

**D7.8. No second scheduler.** Tasks run inside the per-issue engine, under
its locks (ADR 0008's L0, L2 and L1). No daemon, queue or cross-issue
coordination is added, and the EPIC scheduler (#176) still sees one issue.

**D7.9. Review is never self-approved.** `REVIEW` is never a task of a
producing step. No role of a producing step writes, sees as authority or
influences the review's verdict, and the reviewer stays one independent
launch. `ANALYZE_EXECUTE` and `FIX` get the same task contract, since
both produce a candidate through one mutating worker; `FIX` follows (D13).

### D8. Four memory layers and the trust boundary

| Layer | What | Authority | Scope | Lifetime | Owner |
|---|---|---|---|---|---|
| 1. Execution evidence | `ValidationRecord`s, certificates, effect records and their read-backs, entry observations, candidate receipts, the stop's code | authoritative (ADR 0004, ADR 0006) | run | as today | exists |
| 2. Issue Working Memory | typed records of this issue's work: controller observations citing layer 1, operator seeds, agent proposals | advisory | repository, issue, run; each record's applicability names phases and candidates | the run | #317, #333 |
| 3. Repository Knowledge | approved, durable lessons | advisory | one repository; no sharing across repositories by default | until revoked | #325 (EPIC #331) |
| 4. Retrieval projections | indexes and rendered views | none; replaceable and rebuildable | per projection | rebuilt at will | #324 (EPIC #331); D12's render for the MVP |

The trust boundary between them:

- **A controller-observed fact** is a layer 2 record the controller wrote
  from a typed layer 1 source, citing it by kind, identity and digest. Its
  trust is its citation's: every render resolves the citation against the
  current state, and a record whose citation does not resolve is not shown
  as observed.
- **A model-proposed claim** stays `proposed`. No agent and no evaluator's
  score upgrades it. When the controller independently establishes the
  same thing, that is a new observed record with its own citation, never
  the proposal relabelled.
- **An operator seed** is the operator's text, kept verbatim and labelled
  as theirs: trusted as their statement, not as evidence.
- **Layers 2 to 4 are advisory.** None is controller state, GitHub write
  authority, a permission, a review certificate or merge evidence.

### D9. Issue Working Memory's record contract (#317, #333)

**D9.1. One run.** The store is created empty with the run, and a new run of
the issue (after `run --force` or `epic archive`) starts empty again.
Carrying a lesson beyond its run is layer 3's, under approval. A run's
state is the one store whose integrity and lifetime the controller already
manages; an earlier run's state may have been quarantined, archived or
written by another controller version. Memory is retained as long as its
state file: an archived or quarantined state keeps its Memory, and nothing
reads it again. The maintainer kept this scope (§5, answer 2): `run
--force` usually means the earlier run's state is not to be trusted, and
the evaluation needs each run's Memory to start from a known point, empty
or the operator's seed. An operator who wants a lesson carried forward
seeds it (D9.5).

**D9.2. A record** has a controller-assigned id, monotonic in the run; a
kind from a closed set (#317 names it; at least a validation failure, a
refused candidate, a typed launch end, an agent's note and an operator's
seed); its provenance (`controller`, `agent` or `operator`) and trust
(`observed`, `proposed` or `operator`); bounded, redacted text; a citation,
required when observed; its applicability (the phases, and the candidates:
exactly one commit, descendants of a base, or any); the `StepKey` and the
`AttemptID` that created it (D2.1), never `state.attempt`; a status (`current`, `superseded` or `retracted`); and
its lineage.

**D9.3. Live GitHub and CI state is not memory.** Whether a PR is open, a
check's status, a label, a branch head, a review's state and an issue's
text are read fresh by the controller whenever it needs them, and a copy
remembered from earlier is wrong exactly when the value changed. No
controller observation has such a subject, and an agent's note about one is
a proposal worth no more than its text. Memory keeps stable lessons with
evidence behind them: how a command or a provider behaves, what failed on
which candidate, which approach a refused candidate took.

**D9.4. Operations.** `add`, `correct` (a new revision of the same record;
the old text is not shown again), `supersede` (another record replaces it)
and `retract` (withdrawn, nothing replaces it). The controller applies
them all. An agent may propose an `add`, and a `correct`, `supersede` or
`retract` of a `proposed` record only, never of an observed record or the
operator's. The operator may retract any record. A corrected or retracted
text leaves its content digest as a tombstone, and a later `add` with that
digest is refused, so a rejection persists. Memory is a snapshot in the
checkpoint, not a log that is replayed, so a correction cannot be undone by
a replay.

**D9.5. The evaluation's seed.** An operator command of the controller
imports a seed file before the first launch, byte for byte, as one
`operator` record whose digest is recorded with it, and the evaluation's
collector reads that digest back (evaluation protocol §3.5). No agent,
GitHub comment or configuration file can seed Memory. This holds a lesson
unchanged, so §3.5 needs no amendment on that point. #317 names the
command.

**D9.6. Bounds.** The serialized store is at most 1,048,576 bytes per
issue, the evaluation's ceiling. #317 sets the per-record text bound and
the record and tombstone counts so that the ceiling holds by construction.
A write past a bound is refused and counted; nothing is evicted silently.

**D9.7. Writes from evidence are idempotent.** An observed record is keyed
by its citation, so deriving it again adds nothing.

**D9.8. Agent proposals come only from an accepted result,** in the save
that accepts it: the phase's plan save, a stop result's stop save (D4.6),
or the save of another accepted `TaskResult` (D4.1, stages 4 and 5). A
stop result's proposals are admitted like a success's. A refused,
corrected or lost reply leaves no agent proposal in Memory. What the
controller observed while checking it, a failed validation for example, is
its own observed record, saved with its citation (D11). The MVP may ship without agent proposals, and
then the `CONTROL_RESULT` schema does not change; the contract above holds
either way.

**D9.9. Nothing raw.** No transcript, reasoning, tool trace, full diff,
environment, credential or token enters Memory. An agent's or the
operator's text is refused when the run-log redactor would change it, the
rule ADR 0004 D8.3 applies to a published field, so a seed is either kept
byte for byte or refused. Text the controller derives from evidence is
redacted, then bounded, as `verification_failures` is.

### D10. Storage

**Issue Working Memory is one bounded field of the run's `state.json`,
written by the ordinary atomic save.**

- The checkpoint and the Memory revision are one write, so no crash
  separates them (D11).
- `SafeRoot`'s guarantees apply unchanged: the descriptor-relative walk, a
  fresh inode published by name, the file and its directory fsynced. So
  does the strict, bounded loader.
- There is no second loader, schema label or file to reconcile.

The cost: every save rewrites Memory too, up to 1 MiB more per save of a
file that is a few tens of kilobytes today. And every change to Memory's
schema is a state protocol bump. The maintainer accepted the cost for the
MVP with no gate (§5, answer 1). One save is a local fsync of the file and
its directory, small next to an agent launch measured in minutes even at
the ceiling, and Memory is off by default, so only an evaluation arm comes
near the ceiling. #317 still measures one save at the ceiling and reports
the number in its PR. A number that matters is a reason to reopen D10, not
a precondition for building it.

*Rejected: SQLite as authoritative storage.* SQLite opens the database and
its rollback journal, WAL and shared-memory files by pathname, through
none of the controller's descriptors, so a link or a replaced directory
between two opens can redirect them; and with the journal turned off or
kept in memory, a crash can corrupt the database. It inherits none of
`SafeRoot`'s guarantees, and nothing in the package shows otherwise.
OpenCode's organization probe opens its database read-only. The #324
retrieval projection opens only `:memory:`, keeps temporary storage in
memory and disables attached databases. Its disposable generations are not
checkpoints. The rejection of pathname-based SQLite as authoritative storage
still stands.

*Rejected: a separate `memory.json` beside `state.json`.* Two files make a
crash window between them and a rule for it (the checkpoint naming the
Memory digest it commits), with nothing gained while Memory lives no
longer than its run. Layer 3 does outlive runs, since it is
repository-scoped, so #325 decides its store, under the same rule:
`SafeRoot` publication of whole files, or another mechanism whose
guarantees a record proves.

*Rejected: an append-only journal* (`SafeRoot.open_append`). Appends are
cheap, but replay order, a torn last line, compaction and the append's
one-syscall window are machinery a store of 1 MiB does not need.

FTS5 is compiled into the interpreter of the host where this was written
(Python 3.14.10, SQLite 3.54.1). It is a build option of the SQLite library
an interpreter's `sqlite3` module is linked against, so a supported
interpreter may lack it. A later FTS5 projection reports itself unavailable
rather than failing a launch, and the MVP has none.

### D11. Commit order and crash reconciliation

One attempt, each stage durable before the next begins:

1. **Budget and intent:** one save (D5.1). It charges the step, allocates
   the `AttemptID` and, under #319, records the fingerprint.
2. **The launch.** The agent's commits land in the worktree, or in a
   container launch's exported artifact.
3. **Receipt and parse** (D4.1, stages 1 and 2). A clean exit's reply is
   held in process. A failed end's reply is kept in the checkpoint after
   its checks (ADR 0012 D12). Once #332 lands, every attempt's receipt is
   saved after its parse, the single-agent producer's included (D5.2,
   D5.3).
4. **Verification** (stage 3). This is the strict import, then the
   candidate checks, then each `ValidationRecord`, each in its own save
   under ADR 0006's markers. Under #319 the save that records the step's
   current candidate (D7.3) comes between the checks and the records,
   and the independent run (D7.5) comes after the records. A `verified`
   candidate's own ref is created before the save that records it as
   kept, and no existing ref is moved (D7.7). A layer 2
   record derived from a record is saved in the same save as the record it
   cites, and it makes no task stale (D7.3).
5. **Acceptance** (stages 4 and 5). For the producing task this is one
   save: the phase plan (completion context and `intended` effects), or,
   for a result that stops the step with nothing planned, the stop save
   (D4.6); with either, the task `ACCEPTED` and the accepted result's
   Memory proposals. Another task is accepted in a save of its own, after
   D7.3's last comparison.
6. **The effects,** through ADR 0004's journal, then the transition save.
   A stop result has no effects, and its stop save was its transition.

**The checkpoint wins.** What is not in the last saved `state.json` did not
happen, as far as any decision goes. What lies outside it (the worktree,
the object store, the run log, the read context) is evidence that recovery
reads with today's checks, and never adopts because it is there.

| Between | In the MVP | What wins after a crash between them |
|---|---|---|
| checkpoint and Memory revision | one write | no window. If a later store keeps Memory apart, the checkpoint names the Memory digest it committed; a newer revision is discarded, an older or different one refused |
| checkpoint and candidate | the commit lands in git first | the checkpoint. A commit it does not name as accepted is a leftover: read again as the worktree's state (#187) and imported strictly before the next launch, never accepted for being there. A commit it names that the store lacks stops the step, as today |
| checkpoint and attempt result | the reply is outside the checkpoint until kept | the checkpoint. A reply not kept is lost and its attempt stays charged. Nothing reads `stdout.log` or `events.jsonl` to recover one |
| checkpoint and budget | the budget is in the checkpoint, charged before the launch | no window. The count can be one launch too high (a crash between the save and the start), never too low |
| Memory and candidate | a record bound to a candidate cites its commit and tree | the candidate: a record whose candidate is not the step's current one is not applicable (D12) |
| Memory and attempt result | a result's Memory proposals are saved with its acceptance; what the controller observed while checking it is saved with the record it cites (stage 4) | neither without the other: a lost or refused reply leaves no agent proposal. A failed validation or a refused candidate is the controller's own observed record, saved with its citation |
| Memory and budget | a Memory write launches nothing | neither: Memory's byte bound refuses a write, and it never charges or refunds a launch |
| candidate and attempt result | both outside the checkpoint until saved | neither: the next launch is told of the worktree's state, and the reply is lost |
| candidate and budget | the budget first | the budget: the launch that made the candidate was charged |
| attempt result and budget | the budget first | the budget |
| checkpoint and the kept candidate's ref (#319, D7.7) | the new candidate's own ref is created first, then the save that records it as kept. The ref of the candidate kept before is never moved or deleted | the checkpoint. After a crash between the two, the state still names the candidate it kept before, whose ref still resolves to it, so that candidate stays kept. The new ref is a leftover: its target is never adopted, and only a later save that records the new candidate keeps it. A kept candidate whose ref is missing or resolves elsewhere, which only something outside AutoForge can cause, is reported unreachable and never treated as kept |

The crash windows of one attempt, and what the next command does:

| Window | On disk | The next command | Launches | Memory |
|---|---|---|---|---|
| W1. before the intent save | as before the step; no `AttemptID` spent | the step as if new | the step's first, charged then | unchanged |
| W2. after the intent save, before the process starts | the attempt charged, its `AttemptID` spent with no receipt | the entry's reconciliation, then a charged launch with the next `AttemptID` | one more | unchanged |
| W3. while the agent runs | as in W2; the worktree may hold commits | the lock, the worktree's state, the strict import, the entry's reconciliation, a charged launch with the next `AttemptID`, told what the worktree holds | one more | unchanged: nothing was observed |
| W4. the reply received, not kept (a clean exit: stages 1 to 3 run in process), until #332 closes it (D5.3) | as in W3 | as in W3 | one more | unchanged |
| W5. the reply kept before acceptance: a failed end's reply after its checks (ADR 0012 D12); once #332 lands, a receipt after its parse | the kept reply or receipt; a validator's marker, perhaps | the kept reply re-checked, the receipt re-verified (a parse refusal's receipt: its correction); an interrupted validator recorded (ADR 0006) | none, or the correction's | the records saved with each verification record |
| W6. verified, before the acceptance save | the records | the kept reply or receipt re-checked, or W4's relaunch, reusing the same candidate's records | none, or W4's | the records derived from them, already saved; none makes a task stale (D7.3) |
| W7. the acceptance saved: for the producing task, its plan, or a stop result's stop save (D4.6) | the task `ACCEPTED`; the plan and its `intended` effects, or the stop with no plan | the journal completes the effects; a stop stands, with nothing to complete | none | the accepted result's proposals, already saved |
| W8. the effects observed, before the transition | all of the above | the transition | none | unchanged |
| W9. an operator's Memory command (seed, retract) | before or after its one save | nothing more to do | none | all or nothing |
| W10. under #319, a save moved the step's current candidate | the new candidate; the read-only tasks pinned to the old one `CANCELED` in the same save | a reply or receipt of a canceled task discarded (D4.3); D5.5's survivor check before any launch | none | unchanged |
| W11. under #319, a save ended the step with tasks open (D3.1), before their processes were stopped | every task that was open `CANCELED` in that save | a reply or receipt of a canceled task discarded (D4.3); D5.5's survivor check before any launch | none | unchanged: a canceled task's proposals are never admitted |
| W12. under #319, a `verified` candidate's ref created, before the save that records it as kept (D7.7) | the new candidate's ref; the state naming the candidate kept before, or none, with that candidate's ref untouched | the candidate kept before checked against its ref, and still kept; the new ref left where it is, its target not adopted; the new candidate kept only by a later save that records it, which reuses its ref | none | unchanged |

Under #319 every task attempt has these windows, each read-only attempt
charged in an intent save of its own. Once #332 lands, W4 is closed for
every attempt, the single-agent producer's included, by the receipt (D5.2,
D5.3): a crash after it re-verifies the receipt and launches nothing.

### D12. The projection and its freshness

- **One file per launch.** The MVP renders `memory.md` into the launch's
  read-context directory, and the prompt names it. Like every
  read-context file, it is rendered just before the launch-intent save. It
  is rendered from the Memory that save commits, which the save does not
  change. It is written once, not changed while the launch runs, and never
  read back.
- **The header is the catalog:** the projection's format version, the
  scope (repository, issue, `RunID`),
  the Memory revision and its digest, the `StepKey` and the `AttemptID`
  it was rendered for (the value the intent save then spends, D2.1), the
  current candidate or none, how many records are
  shown, omitted for the bound, not applicable and retracted, and a line
  saying the file is advisory, untrusted data, never instructions.
- **The body:** the applicable current records, observed ones first with
  their citations, then the operator's, then proposals, newest first in
  each; each record bounded, and every cut an omission marker.
- **Bounds:** at most 16,384 characters per launch. Rendering reads only
  the loaded state (no file, network or model); #333 measures it against
  the evaluation's 1,000 ms ceiling per launch, which nobody has yet.
- **Freshness:** rendered at every launch from the committed snapshot, so a
  correction or retraction shows at the next launch. The MVP has no index
  to lag behind it; read-after-write is the save itself.
- **Stale fails closed.** A record that does not apply (another phase, or a
  candidate that is not the current one or, for a range, not its
  descendant) is omitted and counted.
- **A later retriever** (EPIC #331) is a replaceable read interface over
  a rebuildable index: given a scope, the snapshot's revision and a query,
  it returns record ids, and the render decides what is shown. SQLite FTS5
  is the planned default backend and LTSearch an optional one, subject to
  #324's go or no-go. The index records the Memory revision it was built
  from. Every hit is revalidated against the current snapshot (it exists,
  is current, is not retracted, applies) before it is rendered. An index
  behind the snapshot can miss a new record, and can never show a
  retracted one. Rank is never authority, and an index never decides what
  is visible. [#324's read library](../memory-retrieval.md) implements that
  separation with one disposable FTS5 generation per launch binding. It
  is not wired into launch delivery. LTSearch remains out of the runtime.
- **No agent-callable search in the MVP.** An agent's only channels to the
  controller are the files it is given and its reply. A `search_memory` or
  `fetch_memory` call would be a new channel; if #319 or #324 adds one, it
  goes through ADR 0007's broker, bounded per launch, scoped and
  revalidated.
- **Never in the worktree.** Memory is never written as a file an agent can
  edit or commit (a `CLAUDE.md`, an `AGENTS.md`, a skill, a provider's
  memory file), which would be agent-writable, publishable and outside
  every bound. No plugin, MCP server, skill or hook delivers it, and Claude
  Code's safe mode, Pi's `--no-extensions --no-skills` and OpenCode's
  isolation stay as they are.

### D13. The first supported scope

- **The Memory MVP is REMOTE `ANALYZE_EXECUTE`.** It projects into every
  launch of that phase (the first, corrections, transient and D11
  relaunches, the relaunches after a failed producer validation), observes
  that phase's evidence, takes the operator's seed and, optionally, agent
  proposals. It is off by default, opt-in per run, and recorded with the
  run (D19). This is where the evaluation measures `memory_benefit`, it has
  the fewest crash windows of the producing phases, and its producer
  validation loop already shows what one record of evidence passed to the
  next launch does.
- **`FIX` follows, under the same contract,** once `ANALYZE_EXECUTE` has an
  evaluation result. `FIX` has the most crash windows (a partial fix,
  acceptance and operator actions, PR body updates) and its candidate is
  bound to a reviewed head.
- **`REVIEW` gets no projection in the MVP.** The reviewer is the
  independent check, and the implementer's lessons would anchor it on the
  implementer's view. The maintainer kept this (§5, answer 3). A later
  record may propose a projection of observed records only. It needs its
  own ADR and an evaluation result showing that the reviewer gains
  something it cannot already read on the PR. Proposals and operator
  records never reach the reviewer.
- **`REPLAN_REEXECUTE` keeps `verification_failures` as it is;**
  `UPDATE_EPIC` gets nothing.
- **Multi-agent execution starts with `ANALYZE_EXECUTE` too,** under
  `sandbox.mode: required` (D7.4).
- **LOCAL is removed** (ADR 0009); nothing is left to decide for it.

### D14. Fresh-session capability

A launch has a **fresh context** when it starts with no conversation of an
earlier launch and no provider-side memory written by one, so that all it
knows of earlier launches comes from its prompt and its read context. A
Memory phase relies on this: a resumed session would carry an unbounded,
unredacted earlier context beside the bounded projection, and the
evaluation's premise that a lesson reaches an agent only through Memory
would fail. The proof is a property of the argv or helper policy the
adapter builds, never the provider's claim.

| Provider | The proof today | What voids it | Status at `9baed0a` |
|---|---|---|---|
| `claude` | `-p` with `--no-session-persistence` (`providers.py:1724`); `--safe-mode` keeps `CLAUDE.md` auto-discovery off | `options.session_persistence: true`; in `extra_args`, any of `-c`, `--continue`, `-r`, `--resume`, `--session-id`, `--fork-session`, `--from-pr`, `--teleport` (2.1.327's help) | the option and these flags pass validation |
| `opencode` | `run --standalone` (`providers.py:2380`) with no session flag: `run` starts a new session | in `extra_args`, any of `-c`, `--continue`, `-s`, `--session`, `--fork`, `--server` (2.3.0's help) | these flags pass validation |
| `pi` | `--no-session` (`providers.py:2847`); `extra_args` refused (`providers.py:2818`) | nothing a profile sets | holds by construction |
| `claude-sdk` | a new session id per launch (`providers.py:3947`); `extra_args` refused (`providers.py:3421`); no resume option in the policy | nothing a profile sets | holds by construction |
| `opencode-sdk` | none: every launch is refused until #152 | not applicable | unsupported |
| `scripted` | the tests' provider: no session | not applicable | tests only |

- **Each adapter declares its proof,** typed, from the profile. A Memory
  phase, or a role that needs a fresh context, whose profile cannot prove
  one is refused before anything of the step is saved or launched (D18).
- **An allow-list, not a deny list.** Claude Code's and OpenCode's
  `extra_args` pass through, and a deny list misses a flag a later CLI
  adds. A Memory phase therefore takes such a profile only with no
  `extra_args` beyond an allow-list #317 names and checks against the
  installed CLI's help, as `AGENTS.md` asks.

### D15. Interaction, pause and budgets

- **#126 owns durable pause and resume.** Until it lands, every launch, a
  Memory phase's and any task's, is built with `pause_available=False`,
  and `durable_pause` stays `False`
  (`test_no_state_awaits_input_until_126_lands`).
- **An interaction request follows the profile's policy, as today.**
  `fail` fails the invocation; `prompt` asks the live resolver, and fails
  where the adapter declares no live capability
  (`test_prompt_without_a_live_capability_fails_and_nothing_else_degrades`);
  and `pause` is refused by configuration validation
  (`test_pause_is_refused_until_its_record_and_gates_land`). Read-only
  roles run under `fail`, so nothing they ask reaches a person.
- **A budget exhausted is the existing safe stop:** the step budget enters
  `BLOCKED` with `step-budget`, and a task bound of #332's uses the same
  sink, with an existing code or one added by an ADR 0011 amendment. Never
  a pause, a wait or a silent skip.
- **Memory adds no interaction.** The operator's seed and retractions are
  commands between invocations, not answers to a running launch.

### D16. Storage security

- **Under `disabled`,** an agent runs as the operator and can rewrite
  `state.json`, and Memory with it. This record does not change that. D1
  bounds what such a rewrite achieves: Memory grants nothing, so a
  rewritten record can mislead a later launch, as injected issue text
  already can, and cannot change a phase, a write, a budget or a merge. A
  rewritten seed changes the evaluation's intervention, which is one
  reason the evaluation's `collect` requires `required`.
- **Under `required`,** a launch sees its read context read-only and the
  rest of the state directory not at all, so its projection is all it
  sees of Memory.
- **No keyed integrity check is added.** Under `disabled`, an agent of the
  same user could read any key the controller could use.
- **Memory holds nothing secret by design** (D9.9). It is stored in the
  clear like the rest of the state directory. Its projection is a
  read-context file: redacted, bounded, fenced as untrusted.

### D17. Online, offline and hybrid writes, and the later layers

No AutoForge result shows any of these better than the others yet, and the
figures #316 cites from MemCurrent come from another system's benchmark
(evaluation protocol §10). The trade-offs, as judged:

- **Online only:** corrections take effect at once, deterministically and
  within bounds; nothing is learned across issues, and lessons stay raw.
- **Offline only:** consolidation can distill and generalize, but a
  retraction waits for the next consolidation, so a revoked lesson stays
  visible until then, and every consolidation is a model launch to pay
  for and review.
- **Hybrid:** layer 2 written online, layer 3 built from offline proposals
  behind approval, retraction online in both.

**Decision: hybrid.** Layer 2 is written online only, and that is the MVP.
#326's offline consolidation generates layer 3 proposals and nothing else.
Every retraction or revocation applies online, at the next render, whatever
an index or a consolidation has seen.

- **Watermarks.** A layer 2 export to #326 is a settled run's snapshot,
  named by `RunID`, Memory revision and digest. #326 records each
  watermark it processed and processes it once, and each proposal cites its
  watermark and its layer 1 citations. A run that settles again later (an
  `unblock` and more steps) exports a later revision, and #326 reads only
  what changed since the last watermark it processed for that run.
- **Partitions.** Layer 2 belongs to its run's state and layer 3 to #325's
  repository store. Neither writes the other.
- **Approval** is explicit, by an operator command recorded with who, when
  and the proposal's digest. A model or an evaluator's score never
  approves, and a rejected proposal's digest is refused again.
- **Consolidation** is opt-in, run by the operator by hand or on a
  schedule the operator sets, never inside a run's step loop by default. It
  launches in its own class with no workspace (ADR 0012 D7's shape), is
  refused under `disabled`, spends a launch budget the operator gives it,
  reads records and their citations (never transcripts), and outputs
  proposals only. It installs no
  skill, changes no effect, and is no part of the Public Alpha (#255).
- **Layer 3 in a projection** is a separate, labelled section, from one
  repository, never shared across repositories by default.

### D18. Unsupported provider behaviour: a refusal before any side effect

- **Static:** these configurations are refused when they are loaded, as
  profile validation refuses a bad option today:
  - Memory or role settings that route a phase to a profile that cannot
    prove what the phase or role needs (D14's proof, D7.4's enforced
    read-only class, an interaction capability);
  - an independent definition that declares no checker inputs or no
    report contract (D7.5);
  - #319's roles enabled with no independent definition (D7.5).
- **Dynamic:** what only the launch's host or environment shows is checked
  at the phase's entry, in the part of `_invoke_phase` that runs the
  preflight and integration checks before the intent save, so a refusal
  leaves the state as it was.
- **No degradation:** no launch without Memory in place of one with it, no
  unenforced launch in place of an enforced one, and no other provider in
  place of the routed one. A fallback is #321's routing, decided there.
- **The refusal** is a `ConfigurationError` (stop outcome `refused`, family
  `configuration`) naming the profile, the capability and the remedy,
  never a value.

### D19. Persisted state and configuration, listed for #23

No number is assigned. A bump is numbered when its pull request is ready,
and two independent persisted changes never share one
([state-and-recovery.md](../agent-guides/state-and-recovery.md), "Numbering
a new protocol"). Items 1 to 3 are the Memory MVP's. They are one change,
so they take one bump (#317). If #332 lands item 2 first, that is a bump of
its own. #316 asks for this list on #23, and it was posted there during
review round 1 of PR #512, in
[the coordination comment on #23](https://github.com/iceboundrock/AutoForge/issues/23#issuecomment-6994507351).
That comment assigns nothing. The acceptance added no item to the list and
moved none to another owner. It settled item 5's condition, and its bump
is announced on #23 by #332 when it is ready (§5, answer 6). Item 4 gained
detail from the answers and the corrections that followed them: the kept
candidate's ref, a canceled task's reason, and an outcome's cause with the
changed paths. #332 and #319 bring item 4's full schema to #23 with their
bump.

1. **Issue Working Memory** (#317, #333): one field of the state. A file of
   the previous protocol loads with empty Memory; an older-labelled file
   that carries the field is refused; a file of the new protocol carries
   it, empty when Memory is off.
2. **The launch ordinal `launch_seq`** (D2.1): a run-wide counter that
   each launch-intent save increments and nothing resets. A file of the
   previous protocol loads with `launch_seq` equal to its `step_count`; an
   older-labelled file that carries the field is refused; a file of the
   new protocol carries it.
3. **The run's Memory setting,** recorded with the run (in
   `config_fingerprint` or beside it), so that no later command switches a
   run's arm without `--accept-config-change`. The evaluation needs this
   to show a run's settings.
4. **The task table** (#332, #319). It holds:
   - the tasks, with each step's ordinal counter (D2) and each canceled
     task's reason (`not-admitted`, `stale-input`, `step-concluded` or
     `unblocked`, D3);
   - the attempts, by `AttemptID`;
   - the input fingerprints (D7.3);
   - the receipts: the binding, the reply's digest, and the parsed result
     or the code of its parse refusal (D5.2);
   - the step's current candidate;
   - the frozen plan's baseline and each candidate's independent outcome,
     with an `inconclusive` outcome's cause and, for `checker-altered`,
     the changed paths and the replaced tree's run (D7.5);
   - the last verified candidate, and the name of the ref that keeps it
     reachable (D7.7).
5. **Every reply kept** (D5.3): #332 closes the clean-exit window for both
   paths, as the maintainer decided (§5, answer 6). It extends
   `delivered_reply`'s contract to every reply, as a receipt. It is a bump
   of its own, which #332 announces on #23, and the Memory MVP does not
   wait for it.

Coordinated, though not state: a `memory:` configuration section is
classified in `tests/evaluation.py` as a Memory arm's treatment (evaluation
protocol §4.1); the prompt change that names `memory.md` moves
`prompt_version`, which is an evaluation amendment (§11); #319's read-only
launch class amends ADR 0005; D7.5's declarations on a validation
definition (independent, its checker inputs, its report contract) amend
ADR 0006 and enter its digests; the kept candidate's ref, the one
exception to AutoForge creating no local refs, amends `AGENTS.md` and the
relevant guide (#319, D7.7); and a new stop code amends ADR 0011.

### D20. What the Memory MVP needs

**Implementation note (#317).** Protocol 20 ships the atomic store, seed
and retraction commands, and the pure projection. Automatic writers and
launch delivery remain #333's. For this first slice, only the Pi adapter
supplies D14's proof, in three parts. The profile's part is checked at
config load: context files off and `require_oauth: false`. Pi's agent
directory holds `SYSTEM.md` and `APPEND_SYSTEM.md`, which no flag turns off.
No check of a directory that outlives the launch keeps them out: a process
an earlier launch started can outlive a controller killed with `SIGKILL`
(D5.5, ADR 0002 §4e), and launches of other checkouts share the directory.
So the proof is the launch's own: its agent directory must be the one in
the private home the outer sandbox makes empty for that launch alone (ADR
0005 D8.1). The host's part is D18's dynamic check before each Memory
launch's intent save. The launch must be enforced, `PI_CODING_AGENT_DIR`
unset (it would project a host directory read-write), and no operator path
may show a host directory at, above or inside `~/.pi/agent` in that home,
decided on where the sandbox mounts it: one spelled in the home at that
spelling, wherever a link there leads. The launch's part is checked on its compiled sandbox description: Pi's home
is the private tmpfs, and nothing is mounted over its agent directory there.
Otherwise the launch is refused before anything is spawned. A same-UID
process outside the sandbox that enters the launch's namespace through
`/proc` is not excluded (docs/pi-policy.md §5). D14's table
above, written before this, lists Pi's proof as holding by construction.
That is true of its argv, not of its agent directory. Claude's no-session option does not
prove auto-memory is off; it is refused when Memory is on, as are the
other adapters without a proof. AutoForge does not start a version probe
or change how any Memory-off launch runs. For D9.6, every record not yet
retracted reserves one event slot and the bytes of one operator retraction,
so the operator's retraction (D9.4) is never refused for a bound, and it
stays available with Memory turned off, the one write that leaves (no
collection, no projection, the same issue and lock rules). Every
revision names the record revision it acts on. An agent's revision comes
from an acceptance save, from a launch no earlier than the record's latest
change. Every revision row also records the launch ordinal it was written
at; the operator's and the controller's are known only to the launches
after it, so a result of a launch up to that watermark cannot revise what
they changed, whatever revision it names. A stale one is refused, on write
and on load. A revision is
compared with the saved rows in its canonical form, so the same input
again is a replay, whatever the case of its URLs. A superseded record
stays superseded when its replacement is retracted. Only a validation
failure its retained receipt establishes on the launch stays shown, unless
a record the projection actually shows carries the same claim; a record
the budget leaves out does not. The distinct established claims are chosen
before anything else is spent, on the records that establish one alone.
Each is held by one record, the one establishing it whose whole block is
shortest as if it were not superseded: in rank order, each claim where
that block fits beside the claims held before it, then, with the budget
left, the claims not held, each by that record's block as it is now, up to
the first that does not fit. A record establishing a claim not held is not
shown. The header gives every count at one width and a render that shows a
record has no notice, so a supersession changes nothing the first choice
reads and only shrinks what the second sizes: a supersession removes no
claim the projection showed, and the prose of a record that establishes
no claim never changes which claims are shown. A budget that cannot hold the header and
its longest notice is refused whatever the records. A superseded record
then holds its claim alone, labeled as superseded with its text omitted, a
block no longer than the one it was chosen on; a current record with the
same claim takes its place where that fits, and its whole block is shown
only with the budget left after that. Neither prose nor a second proof of
one claim displaces another. A launch ordinal is bounded by 2^63 − 1, the
loader's bound: Memory's writes, its launch citations and a projection
take every ordinal the loader reads, and a run that has spent the last one
can still settle a reply it kept, which spends none, but launches nothing
more. A projection is bound to the next launch
ordinal of its snapshot, and its freshness digests every receipt it
resolved citations against, validation, candidate and launch, unique and
in any order. A
citation resolves to a typed retention result: established (a validation
record only), retained or unavailable. This slice admits validation,
candidate and launch receipts, not review, effect or Git references.

**Implementation note (#333).** The handoff ships for D13's first scope,
`ANALYZE_EXECUTE`, through `autoforge.memory_handoff`, and nothing of D1 to
D19 changes. Every launch of that phase in a run whose recorded setting
keeps Memory is given D12's `memory.md`, rendered from the snapshot its
intent save commits, for the ordinal that save spends, before anything of
the launch is saved. A render the store refuses refuses the launch there
(D18). The projection is bound to the launch's candidate: the worktree's
`HEAD` after the strict import when that is not the entry's base, and none
otherwise, with each base of a range-bound record that the candidate
descends from proved locally, never by asking GitHub or fetching, so D12's
render stays free of the network (R3-F1 of PR #536). On the very commit a
retained validation record of the run names, that record proves its base,
since the producer gate validates only a candidate whose range from that
base the controller proved; so a retry on a failed commit is shown its
failure however long its history (R4-F2). Any other base is proved by a
breadth-first walk from the candidate with at most 64 traversal slots. The
walk authenticates a reached base before counting it, even if there are no
slots left to follow its parents. Depth 64 is still reachable, but a missing
or unreadable endpoint, or an exhausted reader budget, leaves the base
unproved and its record omitted. `HEAD` is read through the admin dir the
registry binds and the common dir, never through the worktree's `.git`
file, which an earlier launch of the step can rewrite before a correction
or a retry, and only its id is read there: the commit is read from the
shared store's objects through the controller's private git directory,
which names no promisor remote, so no git version fetches a commit the
store lacks (R4-F1).

**Local read cost (#541).** Previously each commit cost a process with its
own 120-second timeout and 16 MiB capture cap; there was no shared byte
budget or session deadline. Now one private `git cat-file --batch-command`
child serves both the input and the walk. It checks type and size before
requesting a body and re-hashes exact bytes before using any parent link.
It shares a 16 MiB body budget and a 750 ms runtime limit before executor
cleanup; the controller parses at most 256 KiB of commit headers per object.
A read error ends the walk without retrying; bases already proved remain
proved. If the command is unavailable or the input cannot be authenticated,
the launch input is unknown. The run log's `projection_ms` includes
closing the child and removing its private directory, not just the reads.
The evaluation ceiling and the separate validation and publication readers
are unchanged.

A binding the registry does not confirm, a `HEAD`
naming no commit the store holds, and a launch that seeds, bind no
candidate. The prompt names the file in a section rendered only while the
run keeps Memory. The prompt label moves to `v19`, an evaluation amendment, as D19
says, and that move is the one exception to §4.2's byte-identical
Memory-off prompt: against what the release before rendered and saved for
the same Memory-off run (PR #536's merge base, captured by
`tests/fixtures/memory_off_baseline/`), a Memory-off run differs in the
label alone, in the first line of the prompt and in the checkpoint's
`prompt_version`. That holds for a run created with Memory off; a run
created earlier keeps the label it was created with, and one that turns
Memory off keeps its ledger. The run log records
what each launch was shown (sizes, counts, digests, the commit it started
from, or none when it could not be read, the commits the walk read, and
the time of Memory's whole preparation, the ledger's reconstruction from
the checkpoint, the candidate read and the walk included (R4-F4), with the
render's share), never a record's text. Two writers exist. The controller's
`validation-failure` record of a failed candidate cites its validation
record and is added by the producer's failure counter, in the save that
counts the failure (D11 stage 4): the one appending the record, or, for a
terminal record the producer gate reuses and the ledger does not cite
yet (one appended while Memory was off, say), the step's later save. It
is bound to the failing commit and to its base, so on that commit it is
established and on a descendant of the base it is shown with its source
and needs revalidation. An accepted
result's proposals come from an optional `memory` list of the result, which
no required field and no parser rule changes. They are admitted in the plan
save of a success or in the stop save of a `blocked` or `failure` result
(D9.8, D4.6), keyed by the launch ordinal, the commit the launch started
from as the controller read it before the launch (the entry's base only
when it started there) and the entry's position, so the same result
admitted again is a replay. A launch whose start could not be read keys
none: its adds are refused and counted, never keyed by a guessed commit,
and its revisions are admitted as usual (R4-F3). The key is provenance, and
an added record applies to the issue's launches of the phase whatever
commit they start from. The list is bound
to its launch when the reply is parsed: a `correct` or `retract` only of a
record that launch's projection showed, against the revision it showed, no
text past the store's bound or that the redactor would change, and each
reason redacted and bounded as the store keeps it. That binding is the controller's,
held by the process that parsed the reply and never written into the
result, so the result's `memory` key stays the agent's data whatever its
shape. A reply kept across a restart (#479) is kept as Memory off keeps
it, recovered as before, and admits none of its entries, whichever
controller kept it and whether or not its launch was shown Memory; and
nothing the binding adds counts against the reply's bound. Keeping a
binding across a restart is a persisted field and a protocol bump, which
belongs with #332's receipt (D5.3).
Launch ends and refused candidates are not collected yet: no candidate or
launch receipt has a producer, and a record citing one would be shown only
as needing revalidation. `FIX`, `REVIEW`, `REPLAN_REEXECUTE` and
`UPDATE_EPIC` stay unsupported, as `doctor` and the operator documentation
say. The evaluation's Memory collector is not part of this slice. No Memory
launch has run against a live repository, under the real bwrap backend, or
with a real Pi. `tests/test_memory_handoff.py` holds §4.2's Memory items
for this slice: the retry after a validation failure
(`test_a_validation_failure_reaches_the_retry_as_an_observed_record`), the
candidate binding
(`test_a_failure_on_an_earlier_commit_is_labelled_stale_on_its_descendant`,
`test_a_relaunch_reads_its_candidate_through_the_bound_admin_dir_not_the_dotgit_file`),
the corrected and retracted hypotheses across a crash and `resume`
(`test_a_correction_survives_a_crash_and_the_next_launch_never_sees_what_it_replaced`),
the stop save
(`test_a_stop_results_proposals_land_in_its_stop_save_and_nowhere_before`),
a kept reply in its own process
(`test_a_kept_reply_admits_what_its_launch_could_in_the_process_that_parsed_it`)
and across a restart, a Memory-off launch's and a legacy checkpoint's
included
(`test_a_reply_kept_across_a_restart_is_recovered_and_admits_no_proposal`,
`test_a_memory_off_reply_kept_across_a_restart_admits_nothing_once_memory_is_on`,
`test_a_legacy_kept_reply_holding_a_bound_form_admits_nothing`), a reply at
the parser's bound
(`test_a_reply_at_the_parsers_bound_is_kept_and_recovered_with_memory_on`),
the refused reply
(`test_a_refused_or_corrected_reply_leaves_no_proposal`), the seeded
secrets
(`test_no_seeded_secret_reaches_memory_a_projection_a_log_or_the_state`)
and the Memory-off prompt and checkpoint
(`test_with_memory_off_the_prompt_read_context_state_and_run_are_unchanged`,
`test_a_memory_off_run_renders_and_saves_what_the_release_before_did_but_its_label`).
§4.2's owed evaluation fixtures, and §8's of the evaluation protocol, are
held through Memory delivery as follows. Obsolete memory: an operator's
retraction and an agent's
(`test_an_operator_retraction_is_absent_from_the_next_launch`, the
corrected-hypothesis test above). Current memory: a blocked result's
proposals at the next launch
(`test_a_blocked_results_proposals_reach_the_launch_after_unblock`).
Corrected memory: the corrected-hypothesis test. Conflicting memories and a
hallucinated lesson: two contradicting hypotheses and a note claiming, in
the controller's words, a validation pass are shown as proposals, never
as the controller's account, and validation still fails the candidate and
is shown established at the retry
(`test_conflicting_and_false_lessons_stay_proposals_and_validation_still_decides`).
Volatile GitHub state: a note that the issue is closed and the PR merged
is a proposal in `memory.md`, while `issue.md` is the controller's fresh
read and the controller opens the PR and counts no merge
(`test_a_remembered_github_status_never_replaces_the_controllers_fresh_read`).
Scope boundary: a record for another candidate is labelled or omitted, and
`REVIEW` is shown none
(`test_a_failure_on_an_earlier_commit_is_labelled_stale_on_its_descendant`,
`test_review_is_shown_no_memory_and_its_launch_reads_no_projection`).
Negative debugging lesson: the controller's validation failure and an
agent's `failed-approach` (the retry and stop-save tests above). Prompt
injection: an instruction-like note stays inside its untrusted fence in
`memory.md`, out of the prompt, and changes no decision
(`test_an_instruction_like_proposal_stays_fenced_and_changes_no_decision`).
An incomplete index watermark is not applicable to this slice: it is the
watermark of D17's layer 2 export to #326's index, which does not exist;
the layer 2 rule nearest it, that a launch up to a revision's watermark
cannot revise what it changed, is the store's
(`test_a_proposal_from_a_launch_before_a_records_latest_change_is_refused`).

| Needs | Does not need |
|---|---|
| two state fields and a recorded setting, in one bump (D19.1 to D19.3) | #332's reducer or task table |
| the existing atomic save and strict loader (D10) | #319's roles, or D7.4's launch class |
| a deterministic render into the read context (D12) | #324, #325 or #326; SQLite, FTS5 or LTSearch; an embedding service |
| D14's proof from `claude`, `opencode` or `pi`, refused otherwise (D18) | an SDK-only provider |
| `sandbox.mode` `required` with bwrap for a Memory launch (every other launch keeps its configured mode) | Docker or Podman |
| an operator command for the seed and retractions (D9.5) | #126's pause; a new effect kind or stop code |

## 3. What does not change

- The phases, `LEGAL_EDGES`, `UNBLOCK_TARGETS` and `decide_next_phase`.
- Every GitHub write stays one of K1 to K13, the controller's, after the
  phase's acceptance path. Agents, roles and Memory hold no GitHub or git
  credential and gain no tool or shell escape beyond today's.
- Validation, the review certificate and the merge gate, which read no
  Memory and no task state but the producing task's accepted result.
- The lock domains and the EPIC scheduler (ADR 0008, #176).
- Each provider's credentials, isolation and environment.
- The single-agent path, and its default: Memory off, one task per step.

## 4. Tests

### 4.1 What holds the single-agent path today

An implementing child keeps these passing unchanged; each holds a property
this record relies on.

- The loop and its edges: `test_full_loop_to_ready_for_merge`,
  `test_every_decision_is_a_legal_edge`.
- The budget charged before a launch and kept across a crash:
  `test_step_budget_is_cumulative_and_survives_resume`.
- Crash recovery without a second effect:
  `test_a_crash_after_a_landed_effect_repeats_nothing_and_recovers`,
  `test_recovery_after_crash_with_persisted_pr`.
- An interrupted validator is never a pass:
  `test_an_interrupted_validator_leaves_its_marker_and_the_next_step_records_it`.
- One candidate's evidence reused after a crash:
  `test_the_same_candidate_after_a_crash_reuses_its_record`.
- Stale input denied:
  `test_review_base_changed_during_the_review_is_stale`,
  `test_a_stale_completed_node_is_no_success_after_its_input_changed`.
- A lost earlier candidate reported, not prevented, on arm 1:
  `test_a_correct_candidate_overwritten_after_advice_is_a_lost_success`.
- No credential reaches a launch:
  `test_the_child_environment_carries_no_operator_credential_route`.
- No pause, and no degradation without a capability:
  `test_no_state_awaits_input_until_126_lands`,
  `test_prompt_without_a_live_capability_fails_and_nothing_else_degrades`,
  `test_a_pause_needs_the_kinds_live_capability_and_a_way_back_to_the_session`.
- Fresh sessions where they hold by construction:
  `test_pi_rejects_any_extra_args`, `test_claude_sdk_profile_refusals`,
  `test_a_correction_is_a_fresh_session`,
  `test_sdk_providers_refuse_every_launch_until_their_helper_lands`.
- Recovery never reads the run log:
  `test_runlog_recovers_sequence_from_step_names_never_from_the_journal`.

### 4.2 What each child owes

Each is a deterministic test with fakes, offline, in `make check`.

- **#317 and #333 (Memory):**
  - with Memory off, prompts and effects are byte-identical to today's;
  - a seed's digest survives the round trip;
  - each crash window of D11 leaves Memory as its table says;
  - a retraction or correction shows at the next launch and never comes
    back, and a tombstoned digest is refused;
  - a record for another candidate or phase is omitted and counted;
  - the projection's and the store's bounds hold, and a write past one is
    refused;
  - a refused or lost reply leaves no agent proposal, while the
    controller's own observation of it is kept with its citation;
  - an ordinary `blocked` result and a `failure` result (D4.6):
    - each is accepted in one stop save that holds its proposals and no
      plan, and its route is today's;
    - a crash before that save leaves no proposal (W4), or re-checks the
      kept reply and then makes the save (W5);
    - after it, the stop stands with its proposals;
    - after an `unblock` of the blocked one, the next launch's projection
      shows them as proposals;
  - no module that decides a phase, write, budget or merge imports Memory;
  - a profile without D14's proof is refused before any save;
  - D2.1's regression scenario: an `unblock` back into the same step
    gives the next launch a new `AttemptID`, and a record of the first
    launch names the first launch's `AttemptID`, not the second's;
    `launch_seq` survives a crash, `resume`, `unblock` and an issue switch;
    a file of the previous protocol loads with `launch_seq` equal to its
    `step_count`;
  - the evaluation's owed fixtures: obsolete, current, conflicting and
    corrected memory, volatile GitHub state, scope boundary, a negative
    debugging lesson, a hallucinated lesson, a prompt-injection string,
    and an incomplete index watermark.

  #317 also measures one save at the 1 MiB ceiling and reports the number
  in its PR (D10). That is a measurement, not one of these tests.
- **#332 (the reducer and the task table):**
  - purity (the same input, the same proposals) and totality;
  - every proposal refused by the engine's checks changes nothing;
  - every row of D3's table and no other transition;
  - D4.1's stages kept apart:
    - a parse refusal, a check refusal and a binding mismatch each leave
      the task `RETRYABLE` with no plan;
    - a producing task is never `ACCEPTED` outside the save that
      concludes its step: its plan's save or, for a result that stops
      the step with nothing planned, its stop save;
    - an ordinary `blocked` result and a `failure` result are each
      `ACCEPTED` in their stop save, which holds no completion context
      and no `intended` effect and cancels every other open task;
    - a non-producing task is never handed to the phase's plan;
  - the producer's `CONTROL_RESULT` keeps its schema, and a `phase` other
    than the launched one is refused at parse;
  - each D11 window for a persisted task, W10 and W11 included;
  - D3.1's three scenarios: a conclusion with read-only work outstanding,
    a crash between that save and the stop, and reconciliation and
    `unblock` with a task open in each state that is not terminal; a
    late reply of a canceled task discarded in each;
  - D2.1's regression scenario for a persisted task: the canceled task's
    receipt is discarded, and the new task has the next ordinal;
  - D5.3's receipt for the single-agent producer:
    - a crash after the receipt's save and before the save that accepts
      the result re-verifies the receipt and launches nothing, reusing
      the candidate's `ValidationRecord`s;
    - a parse refusal's receipt leads to its correction, with no quote of
      the reply;
  - parity of the single-agent path through the reducer.
- **#319 (roles):**
  - timeout, crash, canceled work, tampering and candidate drift for each
    role;
  - a cycle and every bound refused at admission;
  - a read-only role refused under `disabled` and enforced under
    `required`;
  - D7.3's three scenarios: a successful producer, its verification-derived
    Memory write, and stale read-only work, canceled in the save that moves
    the current candidate;
  - a conclusion with an ideator `RUNNING` and a verifier `VERIFYING` on
    the unchanged current candidate: both canceled in the conclusion
    save, the ideator's process stopped after it, its late reply
    discarded, and the verifier's result never accepted (D3.1);
  - a `CONTROL_RESULT` block in a role's reply refused;
  - a counterexample unverified until reproduced, and still unverified
    when its reproduction is `inconclusive` or `not-run`;
  - D7.5's outcomes for a candidate that changed no checker input:
    `verified` when it passes, `failed` when a baseline test fails;
  - D7.5's checker-altered candidates, each `inconclusive` with the cause
    `checker-altered` and its changed paths, never `verified` or
    `failed`, never kept (D7.7), and no counterexample reproduced on any:
    - a candidate whose only change is a checker input, its replaced
      tree's run passing (`passed`): `pyproject.toml`'s pytest options
      edited; a `conftest.py` that skips the base's tests added, at the
      root or in a subdirectory; a `sitecustomize.py` or a `.pth` file
      added;
    - the same alterations with code under test that fails a baseline
      test, its replaced tree's run failing (`failed`);
    - a candidate that edits an existing test, and one that adds a test,
      each with a passing and a failing replaced tree;
    - the cause, the changed paths and the replaced tree's run named in
      the step's conclusion, the reducer's input and `status`, with the
      list's bound kept and a count of the paths it omits;
  - D7.5's other outcomes, none of them `verified`:
    - a run that exits 0 with no report, with an empty report, or with a
      baseline test missing, skipped, deselected or expected to fail;
    - a timeout or an interrupted run;
    - an empty plan (`not-run`);
  - a configuration with roles and no independent definition refused at
    load;
  - the last `verified` candidate kept through a regression, and none
    kept when no candidate was `verified`;
  - D7.7's refs:
    - each `verified` candidate has its own, named for its commit under
      `refs/autoforge/<run>/`, never under `refs/heads/` and never
      pushed; the kept candidate's is recorded in the state and listed by
      `status`;
    - nothing of AutoForge moves or deletes one: keeping a new candidate
      creates its ref and leaves the ref of the candidate kept before
      resolving where it did;
    - a name that already resolves to another commit is left as it is,
      that candidate is not kept and the step reports it, and the
      candidate kept before stays kept;
    - a kept candidate whose ref is missing or resolves elsewhere is
      reported unreachable, never treated as kept;
    - W12, with the crash injected deterministically: C1 kept and its
      ref saved, then C2 `verified`, and the crash after C2's ref is
      created and before the save that records C2 as kept. The next
      command finds C1 still kept and reachable through its unchanged
      ref, and C2 not kept, its ref's target not adopted and the ref
      left in place. A later save that records C2 keeps it and reuses
      that ref. With no candidate kept before, the same crash leaves
      none kept;
  - a Manager unable to admit, accept or cancel.

## 5. The maintainer's answers

This section held seven open questions when the maintainer accepted the
record. The
[maintainer decision on #316](https://github.com/iceboundrock/AutoForge/issues/316#issuecomment-6994876085)
answers each one, and each answer is now part of the record. The
implementing child an answer names owns its follow-up.

1. **D10, Memory inside `state.json`: accepted for the MVP, with no gate.**
   One save is a local fsync of the file and its directory. Even at the
   1 MiB ceiling it is small next to an agent launch measured in minutes,
   and Memory is off by default, so only an evaluation arm comes near the
   ceiling. #317 still measures one save at the ceiling and reports the
   number in its PR. A number that matters is a reason to reopen D10, not
   a precondition for building it.
2. **D9.1, Memory scoped to one run: kept.** A lesson that outlives its run
   belongs to layer 3, under approval (#325). `run --force` usually means
   the earlier run's state is not to be trusted, and inheriting its Memory
   automatically would undo that. The evaluation also needs each run's
   Memory to start from a known point: empty, or the operator's seed. An
   operator who wants a lesson carried forward can seed it (D9.5).
3. **D13, no projection for `REVIEW`: kept for the MVP.** A later record
   may propose a projection of observed records only. It needs its own ADR
   and an evaluation result showing that the reviewer gains something it
   cannot already read on the PR. Proposals and operator records never
   reach the reviewer.
4. **D7.4, read-only roles refused under `disabled`: accepted.**
   Multi-agent execution needs an enforcing Linux host and has no macOS
   support. A read-only role that is only asked not to write is logical
   isolation, and D7.3's staleness rules depend on read-only being
   enforced. macOS in CI (#501) runs the suite and enforces nothing. A
   macOS-capable enforcing backend, ADR 0013's container backend for
   example, lifts this without a new decision.
5. **D7.7, keeping the last verified candidate reachable: a hidden ref, as
   an explicit exception.** #319 keeps that candidate reachable with a ref
   under `refs/autoforge/` (for example
   `refs/autoforge/<run>/verified/<step>`). It is the one exception to
   AutoForge creating no local refs, bounded as D7.7 says, and #319
   amends `AGENTS.md` and the relevant guide to name it. *After review
   round 3 of PR #512,* each `verified` candidate has a ref of its own,
   which AutoForge creates and never moves: one ref per step, moved
   before the save, made a crash lose the kept candidate. That stays
   within the answer's bounds (D7.7).
6. **D5.3, the lost clean-exit reply: closed by #332, for the single-agent
   path too.** A crash in that window costs a full charged relaunch, and a
   closed laptop or `Ctrl-C` during a long validation is an expected
   failure, not an edge case. #319 needs receipts anyway, and reusing them
   keeps one crash semantics for both paths. It takes its own protocol
   bump, announced on #23, and the Memory MVP does not wait for it.
7. **D7.5, the residual and the cost: both accepted for #319, with one
   requirement.** The residual is accepted. `verified` qualifies a
   candidate to be kept or to reproduce a counterexample, nothing more.
   The review stays the independent judgment, and the merge gate is
   unchanged. A runner-independent check would be a separate project and
   does not block #319. The cost is accepted too, but an `inconclusive`
   caused by replaced checker inputs must be distinguishable from every
   other `inconclusive`: the step's conclusion, the reducer's input and
   `status` name `checker-altered` and the changed paths. Otherwise a
   legitimate change to `pyproject.toml` or a test reads the same as a
   broken run.

## 6. Consequences

- The Memory MVP can be built from the existing save, loader, read context
  and adapters. It adds two state fields (Memory and `launch_seq`), one
  recorded setting and an operator command. Nothing of #332, #319 or EPIC
  #331 is on its path.
- #332 and #319 inherit a contract instead of a blank page:
  - the task states and their table;
  - an attempt identity that no re-entry repeats;
  - the result's five stages;
  - the receipts;
  - the reducer's types;
  - the admission rules;
  - each role's staleness rule;
  - the one mutating worker;
  - an independent verification whose absence is never a pass.
- Three gaps the code has today are named and assigned:
  - a session-resuming flag passes a Claude Code or OpenCode profile's
    validation (D14, for #317);
  - a clean exit's reply is lost to a crash before its checks, which #332
    closes for both paths (D5.3);
  - `state.attempt` repeats after an `unblock` into the same step, so it
    can name no attempt (D2.1, for whichever of #317 and #332 first
    persists an attempt's identity).
- Multi-agent execution is Linux-only behind an enforcing sandbox, a cost
  of refusing logical isolation for read-only roles that the maintainer
  accepted (D7.4).
- AutoForge gains one kind of local ref, under `refs/autoforge/`, to keep
  the last verified candidate reachable (D7.7). There is one per `verified`
  candidate, so that keeping a new candidate never moves the kept one's.
  None is ever moved, pushed or trusted, and the operator cleans them up
  by the run's prefix, as they clean up the per-issue worktree.
