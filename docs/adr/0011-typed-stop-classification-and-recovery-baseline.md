# ADR 0011. Typed stop classification: what a stop is, never what it permits

- **Status:** proposed with #369's pull request. Written after the
  implementation it describes, at `main` `d38bc72` (2026-11-02), where
  every reference below was read.
  - *Implemented with it:* `src/autoforge/stop_classification.py`, a code at
    every `BLOCKED` sink of the engine, the agent-status sink and the CLI's
    `stop:` lines (D1 to D6); the read-only corpus tool
    `tests/stop_corpus.py` (D7); the fixtures of D8.
  - *Not done here:* running the corpus protocol over the review report's
    history (D7.5), and any persisted code (D5).
- **Decides:** #369 (REC-AR0), for #354, under EPIC #184.
- **Amended by #414:** the `pr-owner` code, in the identity family (ADR
  0004 D9.4a). It is the stop of a run whose bound PR's implementation
  marker does not name it (another run, no run, no marker, another
  issue's, a malformed or duplicate marker, an empty body), of a fresh
  run that found the issue's marked PR without any record of it, of a K3
  plan whose PR another run's marker claims, and of a run whose id cannot
  be published in a marker. A replan refused for its owner keeps the code:
  the transaction is rejected as any other replan refusal is, but the stop
  is `pr-owner`, not `replan-refused`. `PROwnerError` carries it; the step
  that meets the error blocks, and so does `run`'s read of a held
  `READY_FOR_MERGE`, the one read made outside a step, so it ends a command
  only if another path outside a step raises it. A K11 takeover that meets a changed body stops with
  `effect-conflict`, like any other effect conflict.
- **Amends:** nothing. Every phase, edge, exit rule, budget, retry, effect
  and the state file are unchanged; seven `BLOCKED` reasons that told the
  operator to `resume`, which refuses a `BLOCKED` run, now say `unblock`.
- **Related:** ADR 0004 (the authority boundary), ADR 0007 and #126 (the
  durable pause), ADR 0008 D15 (the scheduler's `Settlement`), ADR 0009
  (LOCAL's removal), #260 (the troubleshooting guide), #258, #265, #343.

## 1. Context

A command of a run stops in one of a few ways, and each allows something
different next. Before this change the only typed facts about a stop were
the persisted phase and the exit status; *why* it stopped was free text
(`block_reason`, an error message), and so was the advice. That text was
not always right: seven `BLOCKED` reasons told the operator to `resume`
once the cause was fixed, and `resume` refuses a `BLOCKED` run
(`test_no_blocked_reason_advises_resume_without_unblock` now holds every
reason a sink blocks with to `unblock`). #354's review found
recovery advice that a reader could take for an authorization, and #370,
#374 and #375 need a typed baseline before any recovery work can be
measured.

The issue asks for one controller-trusted classification over the existing
control flow, kept apart from authorization, an inventory of every stop
sink, a read-only corpus protocol, and a decision on whether a code has to
be persisted.

## 2. Decisions

### D1. The outcomes

`StopOutcome` names how a command of a run stopped:

| Outcome | What it is | Exit | Its routes |
|---|---|---|---|
| `blocked` | persisted; left only by the operator's `unblock` | 1 | `unblock` (then `resume`), or a new run |
| `failed` | persisted; no outgoing edge | 1 | investigate, or a new run |
| `resumable_error` | the command failed and the persisted phase is unchanged | 1 (2 for `run-log-refused`) | `resume` |
| `refused` | the command refused before it acted on the run | 2 | by its code (D4) |
| `awaiting_input` | #126's durable pause | — | none until #126 lands |
| `ready_for_merge` | the merge gate is closed at a certified clean review | 0 | merge by hand, or (a standalone run only) open the gate and `resume` |
| `handoff` | a scheduler worker's stop at `MERGE` or `UPDATE_EPIC` | 0 | `epic resume` / `epic archive` |
| `done` | the workflow completed | 0 | none |
| `in_progress` | the command stopped at its own limit (`step`, `--max-steps`) | 0 | `resume` (`epic resume` for a namespace) |
| `unknown` | nothing typed says how | 1 | investigate |

`resumable_error` is not a phase or a settlement: it is a fact about how a
command ended, and leaves the state as the last save left it. `refused`
is separate from it because the remedy differs: a configuration drift is
fixed by running the same command with the recorded configuration, a
corrupt state file is repaired or moved aside, and neither is a `resume`.

`awaiting_input` is reserved for #126. `transitions.durable_pause` is
`False` for every state today, so nothing produces it; no route is
invented for it, and no pause protocol is defined here.

### D2. The codes, and where they come from

`StopReasonCode` says why, and `ReasonSource` says which typed fact said
so. There are four sources, and the text of a reason is never one of them:

- **`controller`**: a sink in the engine named its code. Every
  `ControllerEngine._block` call passes one, and every `(Phase.BLOCKED,
  reason)` a verify or apply step returns is built by `_blocked(code,
  reason)`, whose reason is a `CodedReason`: a `str` that carries its code
  to the one `_block` that persists it. A helper whose reasons have
  different causes (`_merge_readiness_problem`, `_gate_producer`,
  `_review_loop_stop_reason`, `_drive_progress_records`) codes each reason
  where it is decided.
- **`agent`**: a validated `CONTROL_RESULT` whose `status` is `blocked` or
  `failure`. The outcome is the one the controller applied; the agent's
  message is quoted in `block_reason` and is never evidence.
- **`exception`**: the class of the `AutoForgeError` that ended `run`,
  `step` or `resume`, nearest mapped class first along its MRO
  (`EXCEPTION_CLASSES`). Its message is never read. `refused` is exactly
  the four bases `cli.main` exits 2 for (`ConfigurationError`,
  `StateError`, `StateTransitionError`, `LockError`), with one exception
  that a class cannot carry: a class says an error exits 2, not that
  nothing ran. A run log's refusal the engine re-raises after it saved a
  launch's attempt (`_begin_step` before the launch, `_record_invocation`
  after the agent returned) keeps its class, the filesystem cause, and its
  exit 2, and the engine sets the typed `StateError.interrupted_attempt` on
  it. The command acted on the run (the agent may have run, its effects may
  be on GitHub) and left the phase for repair-then-`resume`, so it is
  `resumable_error` / `run-log-refused`, never `refused` / `state-invalid`
  with a new run among its routes
  (`test_a_post_agent_run_log_refusal_is_a_resumable_stop_not_a_refusal`).
  An error of `unblock` is not classified: the run is still `BLOCKED`
  after it, and a `resume` route would be wrong.
- **`state`**: a loaded state's phase and settlement, through
  `transitions.issue_settlement` (so the scheduler's view and #126's
  predicate are the same ones ADR 0008 D15.1 uses).

A reason built on the way from a coded one (`f"{reason}. More"`) is a plain
`str` and classifies `unknown`: losing a code can only make it unknown,
never wrong. No code is derived from text by any pattern.

### D3. Classification is not authorization

Nothing that decides a phase, a transition, a retry, a write, a model's
permission or a budget reads a classification. Only `engine.py` (to name a
sink's code and put it on `StepOutcome.stop`) and `cli.py` (to print it)
import the module, and the engine never reads a classification's routes
(`test_only_the_sinks_and_the_cli_import_the_classification`,
`test_the_engine_only_names_codes_and_never_reads_a_route`). `unblock`
takes no classification and re-reads GitHub whatever the code;
`resume` reconciles the phase entry with GitHub and is held to the step
budget; the merge gate re-verifies.

### D4. Routes restate the exit rules and change none

A route is a documented operator command, advice about which existing
command applies. For every outcome but `refused`, the routes follow from
the outcome alone, so a code can never add, remove or reorder one
(`test_routes_follow_the_outcome_never_the_code`): `blocked` is left only
by `unblock` or abandoned for a new run, and never routed to `resume`;
`failed` gets neither `resume` nor `unblock`; `unknown` gets "investigate".
A refusal's routes depend on its code (`REFUSAL_ROUTES`), and none of them
is `resume` or `unblock`. Whether acting on the route needs live facts
re-read first is per outcome, and conservative for `unknown`.

### D5. No code is persisted

The issue asks to decide first whether a persisted code is necessary. It is
not, for this baseline:

- the outcome is already persisted, as the phase, and the routes of a
  persisted stop follow from it (D4);
- `unblock`, the only way out of `BLOCKED`, decides from live GitHub and
  must not depend on a code, so a persisted code would add nothing a
  decision may use;
- a persisted field needs a protocol bump coordinated on #23, and an older
  state would need its absence strictly validated, for a value that is
  advice.

So `StepOutcome.stop` carries the code of the command that stopped, the
CLI prints it, and the state file is unchanged
(`test_nothing_of_a_classification_is_persisted`). A state loaded later
classifies its `BLOCKED` or `FAILED` with code `unknown` (`status`,
`resume`): the code is lost across processes, by design. `block_reason`
remains text for a human, and unknown or legacy text in it is never read as
a code (`test_a_persisted_reason_is_never_read_for_a_code`). The run log
`events.jsonl` stays outside recovery inputs and records no code.

A consumer that needs the code across processes (#370, #374, #375) brings
the persisted field and its protocol bump through #23 at that time, with
the protocol number current then.

### D6. The inventory of stop sinks

Every place a run stops, with the codes it can name. The engine's `_block`
calls and `BLOCKED` returns are exhaustive (54 and 57 at `d38bc72`; one
test fails if a raw `return Phase.BLOCKED, ...` returns); `errors.py` is
exhaustive by `test_every_error_class_is_mapped_explicitly`.

| Family | Sink | Outcome | Codes |
|---|---|---|---|
| bounds | step entry (`_step_once`) | `blocked` | `step-budget`, `review-round-cap`, `producer-validation-bound` |
| bounds | REVIEW's apply (round with findings) | `blocked` | `review-round-cap`, `review-stagnation`, `replan-refused` |
| bounds | candidate validation (ADR 0006) | `blocked` | `validation-interrupted`, `validation-inconclusive`, `producer-validation-bound` |
| bounds | REPLAN_REEXECUTE's reducer | `blocked` | `replan-refused` |
| bounds | UPDATE_EPIC's re-selection | `blocked` | `selection-rejected` |
| K9 / K10 | FIX's prefix pass and drive | `blocked` | `acceptance-action`, `operator-actions` |
| identity | entry reconciliations, plan completion, read-backs | `blocked` | `unjournaled-write`, `effect-conflict`, `claim-ambiguous`, `pr-owner` (#414), `default-branch`, `read-back-mismatch`, `fix-plan-refused`, `epic-roadmap` |
| GitHub | a conclusive read that failed | `blocked` | `github-read-failed`, `github-checks-refused` (#291) |
| merge gate | `READY_FOR_MERGE`, `MERGE` | `blocked` | `merge-inconclusive`, `pr-identity`, `pr-not-open`, `unreviewed-merge`, `merge-checks`, `merge-conflict`, `merge-not-ready`, `merge-write`, `premerge-verification`, `review-certificate`, `review-not-backed` |
| workspace | around an agent launch | `blocked` | `checkout-drift`, `sandbox-tamper` |
| agent | the agent-status sink | `blocked`, `failed` | `agent-blocked`, `agent-failure` |
| execution | an error ending `run` / `step` / `resume` | `resumable_error` | `execution-failed`, `execution-timeout`, `sandbox-launch-refused`, `interaction-required` (SDK questions and permissions, #134), `control-result-invalid`, `verification-failed`, `github-unavailable`, `github-error`, `github-checks-refused`, `git-transport`, `effect-conflict`, `claim-ambiguous`, `pr-owner`, `checkout-drift`, `sandbox-tamper`, `validation-*`, `run-log-refused` (a run log refused after a launch's attempt was saved; exit 2) |
| refusals | an error before the command acted | `refused` | `configuration-invalid`, `configuration-drift` (#219), `sandbox-capability` (ADR 0005 D12.2), `scheduler-owned`, `state-invalid` (LOCAL's state among them), `state-protocol` (#265), `state-transition`, `lock-held` |
| intentional | the saved state | `ready_for_merge`, `handoff`, `done`, `in_progress` | the outcome's own code |
| unknown | an unmapped error, an uncoded reason, a persisted stop | `unknown` or the sink's | `unknown` |

Existing typed errors and their owners are reused, not replaced: #258's
naming of a request an agent CLI refused, #343's limit and last activity of
a killed agent and a provider's transient failure stay in the error and the
run log, and the error's class decides the code; #265's protocol refusal is
`state-protocol`. #311 is not referenced anywhere in the repository at
`d38bc72`, and this record does not know what it owns; it is left to the
review of this change to say whether one of the codes above is its.

LOCAL is removed (ADR 0009): its state is refused by the loader as an
unknown mode, classifies `refused` / `state-invalid` with no route that acts
on the run, and gains no `unblock` edge by implication
(`test_a_local_state_is_refused_never_given_an_unblock`). Its agent-status
sink no longer exists.

The scheduler's modules are unchanged (they parse no `CONTROL_RESULT` and
name no stop, ADR 0008 D2). `classify_state(..., scheduler=True)` is the
scheduler's view: its outcome agrees with `issue_settlement` for every
phase, and where a standalone run's route would be `resume` or a new run, a
namespace's is the scheduler's (`epic resume`, `epic archive`), since both
refuse a namespace's state directory (ADR 0008 D17.3;
`test_a_loaded_state_classifies_by_its_phase_and_settlement`); so is a
refusal's, which only the corpus (D7) classifies in a view, since the CLI
refuses a namespace's state directory before it loads one
(`test_a_refusal_takes_the_view_it_is_given`). A
namespace's `ready_for_merge` has no merge-gate route: its worker stops
there whatever the configuration says (ADR 0008 D15.3), so a human merges
(`test_a_namespace_ready_for_merge_is_merged_by_hand_never_resumed`).

Which view applies is decided on the state directory's resolved path
against the scheduler root, which needs the git common dir. Where there is
none to read (`status` outside a git repository) or none is read (a dry
run, which runs no git), the view is `unknown` and a stop gets only the
routes both views share: `unblock` for `blocked`, a merge by hand for
`ready_for_merge`, "investigate" for every other outcome with a route
(`test_an_unknown_view_gives_only_the_routes_both_views_share`). The
read-only commands work there as they did before the stop line
(`test_read_only_stop_lines_need_no_repository`); `status --json` names the
view in `stop.view`.

### D7. The corpus protocol

`tests/stop_corpus.py` is a maintainer's tool, run by hand over the state
directories of past runs:

```text
uv run python -m tests.stop_corpus [--json] PATH [PATH ...]
```

1. **Read only.** A state file is read by the controller's strict loader,
   which writes nothing; a run log descriptor-relative, links unfollowed,
   and only once its entry is a regular file within its size bound, which
   the read enforces again on the opened file. A FIFO, a socket, a device or
   a link at a state file or a run log is never opened. Nothing is locked,
   launched, sent or written (`tests/test_stop_corpus.py` snapshots the
   tree). No model is called. A state directory that cannot be read, a
   state file the loader refuses and a run log that cannot be read are each
   reported, and every other run is still counted; so is a path given that
   is missing, a link or no directory, and a directory on the way down that
   cannot be listed, at discovery, with the summary saying its input is
   incomplete
   (`test_a_state_dir_given_that_cannot_be_listed_is_reported_never_dropped`).
2. **Which runs.** A state directory keeps evidence of more runs than its
   current `state.json`: a state `epic archive` moved aside
   (`state.json.archived-*`), one `run --force` quarantined
   (`state.json.corrupt-*`), and the run log `logs/<run id>/` of every run
   it held, a run a new standalone run replaced included. Each is found, and
   a run is one **run id**, wherever its evidence lies: a directory given
   twice or copied elsewhere is still one run, classified by one state file
   (one that loads before one that does not, the current one before one
   moved aside, the newest of those first) and counted from one run log; the
   copies are reported, never added. Each state is classified in the view
   of the directory that holds it, read from where it lies: a namespace's
   own `.../autoforge/scheduler/issues/<n>/state`, by the controller's
   predicate (`namespace_issue_of_state_dir`), is the scheduler's, so a
   `MERGE` or `UPDATE_EPIC` there is a `handoff` and every route the
   scheduler's (a refusal's too); namespace evidence without that layout (a
   directory named `state`, one under a scheduler root, one beside a
   `binding.json` or `claim.json`) is an unknown view, where a phase the two
   views classify apart is `undecidable`; any other directory is a
   standalone run's (`test_a_namespace_is_classified_in_the_schedulers_view`,
   `test_a_view_nothing_establishes_is_unknown_and_a_hand_off_undecidable`).
3. **Units and denominators.** Each *run* has one eligibility:
   `intentional`, `deterministic` (a phase `resume` re-enters),
   `needs_human` (`blocked`, `failed`, a pause) or `undecidable`: a run
   whose state the loader refuses (classified by the refusal) or whose
   state is gone, which classifies `unknown`, its run log still counted. A
   state file that does not load and names no run id that can be read is
   counted apart, never as a run, since it may be one of the runs counted.
   A *launch* is one run log line of an agent or a controller-run command,
   told apart by the producer the line names: an agent's provider and
   profile, a controller command's `(controller ...)` profile and no
   provider. A limit says neither (an agent's line carried its wall-clock
   limit in `timeout_seconds` until #193), and a line that names neither
   producer, or both, is a launch of kind `unknown`
   (`test_a_legacy_agent_launch_is_an_agent_whatever_its_timeout_seconds`).
   A failed agent launch's code comes from `timed_out`, `ended_by` and
   `exit_code`. An *operator action* is one run log line of an `unblock`
   (applied or refused) or of an accepted configuration change, and is no
   launch. A *bookkeeping line* is one the controller wrote of itself and
   that records no invocation: a `(...; no agent)` profile no operator
   action has, with no provider and no command, such as the EPIC a new run
   binds (#293). It is no launch, so it is in no launch total and no
   failure rate; a line that says no agent and names a provider or a
   command contradicts itself and is a launch of kind `unknown`
   (`test_a_bookkeeping_line_is_no_launch_and_dilutes_no_failure_rate`).
   Each unit is counted in its own denominator and never summed with
   another; a line whose fields have unexpected types is counted as
   malformed, never converted.
4. **What it cannot say**, printed with every summary: no persisted code
   (D5), so a saved `BLOCKED` or `FAILED` is `unknown`; a failed launch that
   exited 0 (a malformed result, a failed verification) has no typed cause;
   a launch whose line names no producer it recognizes is kind `unknown`;
   a line is bookkeeping only by its profile's `(...; no agent)`, no
   provider and no command;
   a state directory's view is read from its path, so a namespace copied
   out of its layout is `unknown` and one renamed with nothing of its
   namespace beside it reads as a standalone run's; a path that cannot be
   read is reported and counted nowhere;
   a run with no state that loads is `undecidable`; a state that names no
   run is counted apart; a moved, deleted, torn, oversized or irregular run
   log, or one with a foreign or malformed line, leaves its run flagged
   incomplete; and the corpus is the directories given, so whatever chose
   them is its selection bias.
5. **The review report's corpus** (51 local dogfooding runs, 15 applied
   unblocks, about 36 failed launches) is *reported* by #354's review and
   not validated here: this change did not read the operator's run history,
   so it publishes no summary of it. A maintainer runs the tool over those
   state directories and posts the summary on #369; LOCAL runs, if any are
   still on disk, are counted `undecidable` (their state does not load),
   with their launches and unblocks counted from their run logs.
   No non-AutoForge cases were accessible.

### D8. The fixtures

Each is a real engine run (or the CLI) that stops, with its outcome, code,
source and routes asserted (`tests/test_stop_classification.py` unless
named):

| Fixture | Outcome / code / source |
|---|---|
| review round cap, at the entry and after a round with findings | `blocked` / `review-round-cap` / controller |
| review stagnation | `blocked` / `review-stagnation` / controller |
| step budget | `blocked` / `step-budget` / controller |
| K10 operator actions (the shape of the reported #280 → #23 case) | `blocked` / `operator-actions` / controller |
| a true PR conflict at the merge gate | `blocked` / `merge-conflict` / controller |
| failing checks reported before a conflict | `blocked` / `merge-checks` / controller |
| a pending effect in conflict (PR #314's, before #327) | `blocked` / `effect-conflict` / controller |
| #291, regression negative: refused checks | `blocked` / `github-checks-refused`, never a pass |
| #327 / PR #330, regression negative: a body `gh` sanitizes | no stop: `ANALYZE_EXECUTE -> REVIEW` |
| an agent's `blocked` / `failure` | `blocked` / `agent-blocked`, `failed` / `agent-failure` / agent |
| a resumable timeout | `resumable_error` / `execution-timeout` / exception, phase unchanged |
| a post-agent journal refusal (#55), and a refused step directory after the attempt was saved | `resumable_error` / `run-log-refused` / exception, phase and attempt kept |
| a conclusive read failure of the next issue in `UPDATE_EPIC` | `blocked` / `github-read-failed` / controller; its saved reason says `unblock` |
| a plain reason at a funnel | `blocked` / `unknown` / controller |
| `run`, `resume`, `status`, an exit-2 refusal, `unblock` (`test_cli.py`) | the printed `stop:` line, or none for `unblock` |

## 3. What does not change

Phases, edges, `UNBLOCK_TARGETS`, budgets, review rounds, retries,
corrections, effect records, candidate preservation, crash replay, ids,
the state file and its protocol, the run log, the scheduler, the prompts
and every default. The CLI adds one `stop:` line to what it already prints
(stdout after a run's final line; stderr after an error), `status` a
`Stop:` line for `BLOCKED` and `FAILED`, and `status --json` a derived
`stop` object beside the raw fields.

## 4. Open questions

- **Persisting the code** (D5): when #370, #374 or #375 needs it across
  processes, through #23.
- **Failed launches that exit 0**: the journal records only their error
  text. A typed `ended_by` for a malformed result or a refused run log would
  let the corpus tell them apart; it is a run-log change and not made here.
- **Finer codes.** `github-read-failed` and `unjournaled-write` group many
  sinks; the reason's text still names the object. Splitting a code changes
  no route (D4), so it can be done when a consumer needs it.
