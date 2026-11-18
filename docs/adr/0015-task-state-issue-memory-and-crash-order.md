# ADR 0015. Task state below a phase, Issue Memory, and what wins after a crash

- **Status:** proposed, for the maintainer's decision on #316. Planning
  only. Written at `main` `9baed0a` (2026-11-18), where every reference
  below was read. #316's "Current scope" was reconciled on 2026-10-25 at
  `f04db5f` (state protocol 12); §1.2 says what has moved since and how
  this record reads the issue where the code moved under it.
  - *Implemented with it:* nothing in the package. No phase, edge, state
    field, protocol number, configuration key, prompt, effect kind, launch
    class, stop code or default changes. `tests/test_adr_0015.py` keeps
    this record honest: every test it names exists
    (`test_every_test_adr_0015_names_exists`); it claims no protocol
    number and its links resolve
    (`test_adr_0015_claims_no_state_protocol_number_and_its_links_resolve`);
    D3's transition table uses only its own states and leaves none out
    (`test_adr_0015_task_table_is_closed_over_its_states`); D14's table
    has a row for every provider the registry knows, and the argv facts
    it states hold (`test_adr_0015_fresh_context_table_takes_every_provider`,
    `test_adr_0015_fresh_context_proofs_hold_in_the_adapters`,
    `test_adr_0015_resume_flags_pass_validation_today`); D2's identities
    are the code's (`test_adr_0015_identities_are_the_codes`); and what
    D10 says of SQLite holds
    (`test_the_package_opens_sqlite_only_for_the_opencode_probe`).
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
- **Attempt:** one physical launch for a task.
- **Producing task:** the task whose accepted result is the phase's
  result. A step has exactly one.
- **`PhaseResult`:** today's `CONTROL_RESULT`, the one input to a phase
  transition. **`TaskResult`:** what any task's attempt returns, a claim.
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
| `TaskID` | `(RunID, StepKey, ordinal)`, the ordinal of the task in its step, from 1 | derived in the single-agent path (always 1); persisted by #332 |
| `AttemptID` | `(TaskID, n)`, where n is `state.attempt` as the launch-intent save leaves it | yes, today (`attempt`) |
| role | a closed enum (D7.1) | derived in the single-agent path; persisted by #332 |
| candidate | base: the revision the entry observation required (ADR 0004 D4.4); candidate: a `RemoteCandidate` (ADR 0006 D2.1), the commit the phase's candidate check accepted (D2.4) | yes, today, where ADR 0006 records it |
| phase | `Phase` | yes |
| policy version | `PolicySnapshot`: the controller version, `prompt_version`, the recorded configuration (`config_fingerprint`, #219), and the validation `policy_digest` and each `definition_digest` (ADR 0006 D8.1, D3.5) | its parts today; its digest by #332 |
| provider profile | profile name, provider, model and effort, as each launch's run-log record holds them, and D14's fresh-context proof | the run log today; the proof's kind with the task, by #332 |

- **`RunID` is not ADR 0008's `RunKey`.** A `RunKey` outlives runs. Tasks
  and Issue Working Memory belong to one run (D9.1).
- **Every identity comes from values the controller holds,** never from an
  agent's report. A result that names a task or attempt other than the one
  launched is refused (D4.3).
- **None is a GitHub object.** No task is a GitHub sub-issue, and the
  EPIC's graph is unchanged.

### D3. The task lifecycle

A task is in one of seven states. `ACCEPTED` and `CANCELED` are terminal.

| From | Event | To |
|---|---|---|
| `PROPOSED` | the controller's admission checks pass (D7.2) | `ADMITTED` |
| `PROPOSED` | an admission check refuses it (`not-admitted`) | `CANCELED` |
| `ADMITTED` | the launch-intent save (D5.1) | `RUNNING` |
| `ADMITTED` | an input moved, or the step ended without it | `CANCELED` |
| `RUNNING` | the launch ended with a reply the controller holds | `VERIFYING` |
| `RUNNING` | the launch ended with no usable reply, or a crash left its outcome unknown | `RETRYABLE` |
| `RUNNING` | an input moved and the controller stopped the launch (a read-only task, D7.4) | `CANCELED` |
| `VERIFYING` | the controller's checks accept the result | `ACCEPTED` |
| `VERIFYING` | the checks refuse it | `RETRYABLE` |
| `VERIFYING` | an input moved while it was checked | `CANCELED` |
| `RETRYABLE` | a bounded relaunch: a correction, a transient retry, D11's automatic relaunch, or the operator's `resume` | `RUNNING` |
| `RETRYABLE` | an input moved, or the step ended without it | `CANCELED` |

`RETRYABLE` means a relaunch would be legal by the existing rules, not that
one will happen: with no bound left, the step stops through its existing
sink and the task waits in `RETRYABLE` for the operator. A `CANCELED`
task's output is never used, whatever it later turns out to have been.

**Persisted and derived.** The single-agent path persists nothing new.
Every state of its one task is read from fields the state already holds,
and `PROPOSED` never occurs, because the phase's entry admits its task by
construction:

| State | How it is read in the single-agent path |
|---|---|
| `ADMITTED` | an agent phase with `attempt == 0` |
| `RUNNING` | in the process that launched it: `attempt >= 1` and the launch under way |
| `VERIFYING` | in that process: the reply under `_check_reply`; on disk: a `delivered_reply` not yet applied |
| `ACCEPTED` | the phase's plan saved (`completion_context`), or the phase moved on through `decide_next_phase` |
| `RETRYABLE` | on disk: `attempt >= 1`, no plan and no kept reply. A crash during `RUNNING` reads the same, and both resolve the same way: the entry's reconciliation, then a charged launch |
| `CANCELED` | the phase moved on without an accepted result: the entry's reconciliation routed it elsewhere (a push by someone else turns `FIX` into `REVIEW`) |

Under #319 a step holds tasks the existing fields cannot describe, so
#332 persists each task's state, attempts and receipts (D19).

**Where a task state mirrors the workflow:**

| Workflow | Task | How the two relate |
|---|---|---|
| an agent phase is entered | its producing task is `ADMITTED` | the entry admits it; under #319, the other tasks are admitted one by one |
| the phase transitions | the producing task is `ACCEPTED`; every other open task is `CANCELED` (`step-concluded`) | the transition is still `decide_next_phase`'s |
| `BLOCKED` | none | not a task state. Open tasks keep their last state; `unblock` re-enters a phase through `UNBLOCK_TARGETS`, and that entry reconciles them: a `RUNNING` task's outcome is unknown, so it is `RETRYABLE`, or `CANCELED` when the phase re-entered is another step |
| `FAILED` | none | terminal; no open task is resumed |
| `AWAITING_INPUT` | none | reserved for #126 (D15) |

*Rejected:* `BLOCKED` and `FAILED` as task states. A task-level `BLOCKED`
would be a second stop the operator has to leave, with no route of its
own, and could disagree with the phase's. A task cannot proceed only
because its step cannot, and the step's stop is the workflow's.

### D4. `TaskResult` and `PhaseResult`

1. **One producing task per step.** Only its accepted `TaskResult` is
   handed to the phase's existing acceptance path (`_check_reply`, the
   plan, the effects) as the `PhaseResult`. In the single-agent path the
   two are one object, and `parse_control_result` stays its parser.
2. **Another role's reply is never parsed as a phase result.** It is
   framed with markers of its own, and each parser refuses the other's
   markers in its input. A `CONTROL_RESULT` block in an ideator's reply is
   therefore refused, not read. #332 defines the framing.
3. **A `TaskResult` is bound to its attempt.** It carries the task and
   attempt it answers and the input fingerprint it was launched with
   (D7.3). One whose binding differs, or that arrives after its task was
   `CANCELED`, is discarded and logged.
4. **What a `TaskResult` may carry is closed per role:** a task proposal
   (a Manager's, D7), a finding or counterexample claim (D7.6), the
   worker's candidate, which the controller reads from the worktree as
   today and never takes from the text, and Memory proposals (D9.8). It
   never carries a phase, an effect, a GitHub write, a merge, a
   permission, a budget change or a stop code.
5. **It is bounded and redacted at parse,** as `CONTROL_RESULT` is today:
   a block bound and per-field bounds, enforced by refusal, never by a
   silent cut.

### D5. Launch intent and the attempt protocol

**D5.1. Before a launch, one save.** It holds the attempt number, the step
charge, the launch label and the entry observation, as today; under #319
also the task, its role, its profile and its input fingerprint. Nothing
is launched before that save is durable.

**D5.2. After a launch, a reply is kept before anything follows from it,
in two cases.** A reply delivered before a failed end is kept today (ADR
0012 D12). Every attempt of a #319 task keeps its `TaskResult` receipt
(the validated, bounded result object, its digest and its binding) in the
save that ends `RUNNING`, so a restart re-checks the receipt and launches
nothing.

**D5.3. Single-agent parity.** A clean exit's reply to a phase task is
checked in process, and not saved before its checks. A crash in that
window costs one charged relaunch (W4, D11). Closing the window by keeping
every reply is #332's decision, with its own bump. The Memory MVP does not
need it.

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
                             attempts, how its last attempt ended (typed), receipt digest
  policy                     PolicySnapshot (D2), the role table, D7.2's bounds
  capabilities               per routed profile: D14's proof, the interaction
                             capabilities (ADR 0007 D5.3), whether its launch is
                             enforced (ADR 0005), whether D7.4's read-only class exists
  verification               candidate checks, import outcomes, and the ValidationRecords
                             of the candidates in play under the frozen plan (D7.5)
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

**D7.3. Input fingerprints are immutable.** A task's fingerprint covers the
base, the pinned candidate it reads (commit and tree), the read context's
digest, the Memory revision and digest it was shown, the prompt template's
digest, and the receipts of its prerequisites. If any of these moves before
the task is accepted, the task is `CANCELED` (`stale-input`).

**D7.4. One mutating worker.** At most one task of a step writes the
worktree, and it is the producing task. Read-only tasks read an immutable
export of a pinned candidate, never the live worktree, and may run while
the worker does. A read-only result is accepted only while its pinned
candidate is still the step's current candidate.

Read-only is enforced, not asked for. Read-only roles run in a launch class
of their own (an ADR 0005 amendment by #319, shaped like ADR 0012 D7's
advisor class): the pinned export read-only, no worktree, no git admin
directory, no state directory but their own read context, a private home.
Under `required` the launch must be enforced (`enforced: true`). Under
`disabled` it is refused before it starts, with no fallback to an
unisolated launch: a prompt that says "do not write" is logical isolation.
So multi-agent execution needs a Linux host that can enforce the sandbox,
and has no macOS support.

**D7.5. The verification plan is frozen at the step's entry.** It is the
trusted definitions from configuration (ADR 0006 D3), bound by their
digests, and no role can change it. A definition runs the candidate's own
tests, which the worker can edit, so a pass alone does not show the
candidate left its checkers alone. A candidate counts as independently
verified (D7.7) only when the same definitions also pass with the step
base's existing test files laid over it, its added test files kept, as the
evaluation's pristine run does (evaluation protocol §3.4). The
single-agent path's validation is unchanged.

**D7.6. A counterexample is a claim.** One an ideator or verifier reports
is unverified until the controller reproduces it, by running a definition
of the frozen plan on the pinned candidate and recording the failure. A
test the agent wrote that agrees with its own output proves nothing (the
evaluation's `invented_expected_answer` fixture).

**D7.7. The last independently verified candidate is kept.** The controller
records the step's last candidate that D7.5 counts as independently
verified, with its records. A later candidate never erases it. Whether the step concludes with
it after a regression is #332's policy, and never silent: the conclusion
names its candidate. How its objects stay reachable when AutoForge creates
no local branch is open (§5).

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
reads it again.

**D9.2. A record** has a controller-assigned id, monotonic in the run; a
kind from a closed set (#317 names it; at least a validation failure, a
refused candidate, a typed launch end, an agent's note and an operator's
seed); its provenance (`controller`, `agent` or `operator`) and trust
(`observed`, `proposed` or `operator`); bounded, redacted text; a citation,
required when observed; its applicability (the phases, and the candidates:
exactly one commit, descendants of a base, or any); the step and attempt
that created it; a status (`current`, `superseded` or `retracted`); and
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

**D9.8. Agent proposals come only from an accepted result:** the phase's
plan save, or an accepted `TaskResult`. A refused, corrected or lost reply
leaves nothing in Memory. The MVP may ship without agent proposals, and
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
file that is a few tens of kilobytes today. Nobody has measured it. And
every change to Memory's schema is a state protocol bump.

*Rejected: SQLite as authoritative storage.* SQLite opens the database and
its rollback journal, WAL and shared-memory files by pathname, through
none of the controller's descriptors, so a link or a replaced directory
between two opens can redirect them; and with the journal turned off or
kept in memory, a crash can corrupt the database. It inherits none of
`SafeRoot`'s guarantees, and nothing in the package shows otherwise. The
package opens SQLite once, read-only: OpenCode's organization probe
(`providers.py:2113`). Planning FTS5 as the search backend does not change
this: a search index is a projection (D12), never the store.

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

1. **Budget and intent:** one save (D5.1).
2. **The launch.** The agent's commits land in the worktree, or in a
   container launch's exported artifact.
3. **The reply,** held in process; kept in the checkpoint for a failed end
   (ADR 0012 D12) and, under #319, for every task attempt (D5.2).
4. **Verification:** the strict import, the candidate checks and each
   `ValidationRecord` (each its own save, under ADR 0006's markers). A
   layer 2 record derived from a record is saved in the same save as the
   record it cites.
5. **Acceptance:** the phase plan (completion context and `intended`
   effects), the task `ACCEPTED`, and the accepted result's Memory
   proposals: one save.
6. **The effects,** through ADR 0004's journal, then the transition save.

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
| Memory and attempt result | Memory from a result is saved with its acceptance | neither without the other: a lost or refused reply leaves no Memory |
| Memory and budget | a Memory write launches nothing | neither: Memory's byte bound refuses a write, and it never charges or refunds a launch |
| candidate and attempt result | both outside the checkpoint until saved | neither: the next launch is told of the worktree's state, and the reply is lost |
| candidate and budget | the budget first | the budget: the launch that made the candidate was charged |
| attempt result and budget | the budget first | the budget |

The crash windows of one attempt, and what the next command does:

| Window | On disk | The next command | Launches | Memory |
|---|---|---|---|---|
| W1. before the intent save | as before the step | the step as if new | the step's first, charged then | unchanged |
| W2. after the intent save, before the process starts | the attempt charged | the entry's reconciliation, then a charged launch | one more | unchanged |
| W3. while the agent runs | the attempt charged; the worktree may hold commits | the lock, the worktree's state, the strict import, the entry's reconciliation, a charged launch told what the worktree holds | one more | unchanged: nothing was observed |
| W4. the reply received, not kept (a clean exit) | as in W3 | as in W3 (D5.3) | one more | unchanged |
| W5. the reply kept, under verification | the kept reply or receipt; a validator's marker, perhaps | the reply re-checked; an interrupted validator recorded (ADR 0006) | none | the records saved with each verification record |
| W6. verified, before the plan save | the records | the kept reply re-checked, or W4's relaunch, reusing the same candidate's records | none, or W4's | the records derived from them, already saved |
| W7. the plan saved | the plan and its `intended` effects | the journal completes the effects | none | the accepted result's proposals, already saved |
| W8. the effects observed, before the transition | all of the above | the transition | none | unchanged |
| W9. an operator's Memory command (seed, retract) | before or after its one save | nothing more to do | none | all or nothing |

Under #319 every task attempt has these windows, each read-only attempt
charged in an intent save of its own, and W4 is closed by the receipt
(D5.2): a crash after it re-checks the receipt and launches nothing.

### D12. The projection and its freshness

- **One file per launch.** The MVP renders `memory.md` into the launch's
  read-context directory, and the prompt names it. It is rendered from the
  checkpoint the intent save committed, written once before the launch and
  not changed while it runs and, like every read-context file, never read
  back.
- **The header is the catalog:** the projection's format version, the
  scope (repository, issue, `RunID`),
  the Memory revision and its digest, the `StepKey` and attempt it was
  rendered for, the current candidate or none, how many records are
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
  is visible.
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
  implementer's view. A later record may add a projection of observed
  records only.
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

- **Static:** a configuration whose Memory or role settings route a phase
  to a profile that cannot prove what the phase or role needs (D14's
  proof, D7.4's enforced read-only class, an interaction capability) is
  refused when it is loaded, as profile validation refuses a bad option
  today.
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

No number is assigned. Each item is its own bump, numbered when its pull
request is ready
([state-and-recovery.md](../agent-guides/state-and-recovery.md), "Numbering
a new protocol").

1. **Issue Working Memory** (#317, #333): one field of the state. A file of
   the previous protocol loads with empty Memory; an older-labelled file
   that carries the field is refused; a file of the new protocol carries
   it, empty when Memory is off.
2. **The run's Memory setting,** recorded with the run (in
   `config_fingerprint` or beside it), so that no later command switches a
   run's arm without `--accept-config-change`. The evaluation needs this
   to show a run's settings.
3. **The task table** (#332, #319): tasks, attempts, input fingerprints,
   receipts, and the last verified candidate.
4. **Every reply kept,** if #332 closes D5.3's window: an extension of
   `delivered_reply`'s contract.

Coordinated, though not state: a `memory:` configuration section is
classified in `tests/evaluation.py` as a Memory arm's treatment (evaluation
protocol §4.1); the prompt change that names `memory.md` moves
`prompt_version`, which is an evaluation amendment (§11); #319's read-only
launch class amends ADR 0005; and a new stop code amends ADR 0011.

### D20. What the Memory MVP needs

| Needs | Does not need |
|---|---|
| one state field and one bump (D19.1, D19.2) | #332's reducer or task table |
| the existing atomic save and strict loader (D10) | #319's roles, or D7.4's launch class |
| a deterministic render into the read context (D12) | #324, #325 or #326; SQLite, FTS5 or LTSearch; an embedding service |
| D14's proof from `claude`, `opencode` or `pi`, refused otherwise (D18) | an SDK-only provider |
| `sandbox.mode` `disabled` or `required` with bwrap | Docker or Podman |
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

- **#317 and #333 (Memory):** with Memory off, prompts and effects are
  byte-identical to today's; a seed's digest survives the round trip; each
  crash window of D11 leaves Memory as its table says; a retraction or
  correction shows at the next launch and never comes back; a tombstoned
  digest is refused; a record for another candidate or phase is omitted
  and counted; the projection's and the store's bounds hold, and a write
  past one is refused; a refused or lost reply leaves no proposal; no
  module that decides a phase, write, budget or merge imports Memory; a
  profile without D14's proof is refused before any save; and the
  evaluation's owed fixtures: obsolete, current, conflicting and corrected
  memory, volatile GitHub state, scope boundary, a negative debugging
  lesson, a hallucinated lesson, a prompt-injection string, and an
  incomplete index watermark.
- **#332 (the reducer):** purity (the same input, the same proposals);
  totality; every proposal refused by the engine's checks changes nothing;
  every row of D3's table and no other transition; each D11 window for a
  persisted task; parity of the single-agent path through the reducer.
- **#319 (roles):** timeout, crash, canceled work, tampering and candidate
  drift for each role; a cycle and every bound refused at admission; a
  read-only role refused under `disabled` and enforced under `required`;
  a stale read-only result canceled; a `CONTROL_RESULT` block in a role's
  reply refused; a counterexample unverified until reproduced; a candidate
  that passes only with its own edited copy of an existing test not
  counted as verified; the last verified candidate kept through a
  regression; a Manager unable to admit, accept or cancel.

## 5. Open questions for the maintainer

1. **D10's storage cost is unmeasured.** Memory inside the checkpoint
   rewrites up to 1 MiB per save. Accept it for the MVP, or have #317
   measure the save's cost at the ceiling first?
2. **D9.1 scopes Memory to one run.** A rerun of the same issue after
   `run --force` starts empty. Is carrying a lesson across runs layer 3's,
   as decided, or wanted in layer 2?
3. **D13 gives `REVIEW` no projection.** Should a later record consider a
   projection of observed records only?
4. **D7.4 refuses read-only roles under `disabled`.** Multi-agent
   execution then needs an enforcing Linux host and has no macOS support.
5. **D7.7's last verified candidate** must stay reachable in the object
   store while AutoForge creates no local branch. #319 needs a way to
   keep it reachable that is neither a branch nor a garbage-collection
   setting the operator owns.
6. **D5.3 keeps today's window** in which a clean exit's reply is lost to a
   crash before its checks. Should #332 close it for the single-agent
   path too?

## 6. Consequences

- The Memory MVP can be built from the existing save, loader, read context
  and adapters, plus one state field, one recorded setting and an operator
  command. Nothing of #332, #319 or EPIC #331 is on its path.
- #332 and #319 inherit a contract instead of a blank page: the task
  states and their table, the result split, the receipts, the reducer's
  types, the admission rules and the one mutating worker.
- Two gaps the code has today are named and assigned: a session-resuming
  flag passes a Claude Code or OpenCode profile's validation (D14, for
  #317), and a clean exit's reply is lost to a crash before its checks
  (D5.3, for #332).
- Multi-agent execution is Linux-only behind an enforcing sandbox, a cost
  of refusing logical isolation for read-only roles (D7.4).
