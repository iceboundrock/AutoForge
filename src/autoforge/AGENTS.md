# Source scope: src/autoforge/

Applies to production code in this package. The repository contract in the
root `AGENTS.md` (precedence, untrusted data, branch safety, boundaries,
conventions, routing rules) also applies here and is not repeated below.

## Module map

Read the module docstring before changing a module; this map only says where
a concern lives so the routing rules can be applied.

```text
cli.py              subcommands run / step / resume / unblock / status / doctor / local, dry-run flags;
                    the `epic` group (run / resume / status / stop / archive, #183) and the
                    hidden `epic _worker` entry point of a scheduler's worker (#182)
config.py           configuration loading and validation; owns config defaults and keys
doctor.py           read-only environment checks; the scheduler rows (settings, and with
                    `--epic` the coordination root and the EPIC's graph, #183)
doctor_sandbox.py   doctor's outer-sandbox rows: capability and mode outcome, policy by
                    class, provider declarations and sandboxed --version, operator paths,
                    hard links, sandbox directories, TCP listeners; read-only (#169)
engine.py           ControllerEngine: phase orchestration, verification calls, recovery
transitions.py      Phase enum, LEGAL_EDGES / LOCAL_LEGAL_EDGES / UNBLOCK_TARGETS, decide_next_phase
profiles.py         execution-profile routing (which provider/model/effort per phase/round)
loop_guard.py       REVIEW/FIX loop bounds and stagnation (pure logic)
replan.py           replan policy and review-history collection (pure logic)
replan_txn.py       REPLAN_REEXECUTE durable transaction state and verifiers
providers.py        provider adapters: the only place that knows real CLI flags
pi_rpc.py           Pi RPC wire protocol: JSONL codec, request ids, event reducer,
                    outcome classification; provider layer, no flags and no processes (#131)
claude_stream.py    Claude stream-json reducer: result text, failure, summary, progress
                    events; provider layer, no flags and no processes (#192)
progress.py         provider-neutral ProgressEvent, cleaning, and the line renderer the
                    engine reports a launch through (#192)
loop_detect.py      provider-neutral loop detector: action fingerprints, line keys,
                    cycle / novelty / repeated-line / retry signals, warnings,
                    verdict and calibration; pure, reads no clock (#194)
executor.py         subprocess lifecycle, timeouts, a caller's stop, capture, optional
                    stdin payload, optional outer sandbox (no workflow semantics)
executor_duplex.py  duplex child handle for RPC transports: stdin records out, LF-framed
                    stdout records in, idle timeout and max runtime, ADR 0002 teardown;
                    bytes only (#130, #193)
sandbox.py          outer sandbox: the backend-neutral launch description, the policy
                    compiler (ADR 0005 D4.2, D4.3, D7.2) and the facts; executor layer,
                    no workflow semantics (#167)
sandbox_bwrap.py    the Linux backend: the only place that knows a `bwrap` flag; the
                    launch's keyring and Landlock scope, setup-failure detection, the
                    typed capability probe (#167, #169)
sandbox_policy.py   which host resources a launch gets by launch class (ADR 0005 §2.5):
                    LaunchFacts from the engine, ProviderRequirements from an adapter,
                    the SandboxPlan, the path-valued environment rewrite (D8.2),
                    executable and install-root resolution; no provider branch, no
                    workflow decision, launches nothing (#168)
agent_worktree.py   the agent worktree's git surface in every sandbox.mode: registry
                    binding and listing (D6.4, #223), candidate read through the
                    common dir (D6.3),
                    strict import of private objects (D6.5); git only through the
                    executor's hardened local request (#168)
github.py           GitHubClient over `gh`, the only holder of a GitHub credential:
                    verification reads, the reads behind an agent's read context, typed
                    controller writes (never retried on an ambiguous outcome) +
                    controller-owned merge
child_env.py        what every agent and repository-command launch gets instead of a
                    credential: forbidden env names, per-launch overrides (empty gh
                    config, identity-only git config, command-scope resets, empty
                    curl and wget config, empty ZDOTDIR, a POSIX SHELL), the
                    preflight and its login-shell probe; provider-neutral, no
                    flags (#165)
read_context.py     an agent's GitHub read context: rendering, redaction and bounds,
                    no I/O; the engine collects and writes it (ADR 0004 D10, #165)
effects.py          ADR 0004 effect records: kinds, stages, attempt bound, drive/reconcile,
                    entry observation, UPDATE_EPIC completion context (#160)
effect_ops.py       per-kind effect operations: identity read, precondition, write (#160)
git_transport.py    controller git push: sanitized config, explicit URL, lease, no hooks;
                    the hardened request every local controller git process uses (#160)
claims.py           durable GitHub claims: marker schemas, renderers, scan, cardinality
validation.py       typed GitHub URL parsing and run-argument validation
premerge.py         controller-produced pre-merge evidence (check definitions, tree export)
materialize.py      byte-exact export of a candidate commit from the shared object store
                    and its hash-back proof; the pre-merge export's writer (ADR 0006 D6, #171)
evidence.py         candidate validation evidence: definitions, policy P, ValidationRecords,
                    the attempt marker, their bounds and integrity checks; pure (ADR 0006, #172)
certificate.py      review certificates: issuance from the review binding, applicability
                    (certificate_problem, also against the live PR the merge gate reads, #175)
                    and the not-a-merge-permission wording; pure (ADR 0006 D12, D13, #174)
result_parser.py    CONTROL_RESULT extraction and strict per-phase validation
state.py            persisted controller state, atomic writes, schema/protocol checks
run_namespace.py    the parallel EPIC scheduler's per-issue run identities (RunKey) and
                    namespaces under <common>/autoforge/scheduler/issues/<n>: layout,
                    binding.json, read-only lookup and listing, the state archive, the
                    standalone state-dir refusal; no lock, claim or GitHub read (#178)
run_contract.py     the durable contract of a LOCAL run
config_fingerprint.py  the recorded configuration of a REMOTE run (profile routing,
                    agent worktree root) that step/resume/unblock compare (#219)
local_workspace.py  LOCAL mode working-tree verification
safefs.py           filesystem capability boundary for every controller write
locking.py          the lock domains of ADR 0008 (#180): L0 (ControllerLock, keyed by the git
                    common dir; shared and adopted close-only), L2 (IssueRunLock and its
                    lock-free probe), L1 (SharedGitLock around each common-dir git write),
                    the order assertion and the scheduler's filesystem refusal
ownership.py        who may act on an issue (ADR 0008 D17.2, #180): the forward guard's
                    ownership predicate (an open scheduler claim included, #181) and the
                    reverse guard's standalone-state read
scheduler_state.py  the scheduler's durable bookkeeping (ADR 0008 D6, D7, D14, #181):
                    claim.json (claims, generations, the closing rule, compare-and-write
                    under L2), scheduler.json, the pure restart classification,
                    reconciliation, worker finalization and open-claim queries; recovery
                    data, never liveness; no spawn, no GitHub, no per-issue state write
epic_graph.py       the parallel EPIC scheduler's view of GitHub's native graph (ADR 0008 D3,
                    D4, #179): the complete-or-refused EpicSnapshot of an EPIC's direct
                    sub-issues and their blocked_by lists, cycles, completed-only blocker
                    satisfaction, the pure evaluate (verdicts, LocalFact, ready order), the
                    snapshot and node digests, the admission re-read; four client reads,
                    no GitHub write, no lock, no file
scheduler.py        the parallel EPIC scheduler (ADR 0008 D2, D9, D16, #182): holds L0 for its
                    life, admits ready issues one transaction at a time under L2 (re-read,
                    binding, claim, spawn), reaps and finalizes workers, drain, stop and
                    Ctrl-C; #179's four reads only, no GitHub write, no provider name, no
                    effect record, no CONTROL_RESULT, never SIGKILL
worker.py           a scheduler's issue worker, `epic _worker` (#182): adopts the inherited
                    L0 and L2 by proof, checks the claim, invocation and config digest, then
                    runs the unchanged engine on the issue's namespace with the merge gate
                    closed and MERGE / UPDATE_EPIC as stop phases, proving L2 and reading
                    the stop request at each step boundary
executor_supervised.py  one supervised child: spawned in a session of its own with only the
                    passed descriptors and a log as stdout and stderr, signalled (never
                    SIGKILL, never once reaped), polled; executor layer, no workflow (#182)
scheduler_stop.py   `epics/<n>/stop-request` (ADR 0008 D16.4, #182): published by link(2),
                    never replaced, read strictly (one that does not parse is a drain),
                    removed only by the L0 holder
epic_commands.py    the `epic` commands' logic (ADR 0008 D9.8, D14.4, D16.4, D17.4, #183):
                    the lock-free `epic status` (one category per child, the
                    autoforge.epic-status/1 document), the dry-run plan, `epic run`'s
                    open-claim refusal and --force, `epic stop`'s outcomes and `epic
                    archive` under L0 and L2; graph reads only, no signal, no GitHub write
redaction.py        secret redaction for anything that reaches logs
runlog.py           per-invocation run logs, redacted
errors.py           typed error taxonomy
prompts/            file-based templates ({{VAR}}): common.md (trust boundary), phase
                    templates, update_epic_rerequest.md, local_*.md, correction.md,
                    worktree_state.md (#187); renderer in prompts/__init__.py
```

## Rules for production code

- Every behavioural change ships with tests under `tests/` (see
  `tests/AGENTS.md`). Do not change orchestration semantics, verification, or
  a safety gate as an incidental part of another change.
- Keep the boundaries: no CLI flags outside `providers.py`, and no
  provider wire-protocol names outside the provider layer (`providers.py`
  plus the protocol modules it owns, `pi_rpc.py` and `claude_stream.py`);
  no workflow semantics or JSON in `executor.py` or `executor_duplex.py`,
  and none in `sandbox.py` or `sandbox_bwrap.py` either; no `bwrap` flag
  outside `sandbox_bwrap.py`; no provider branch or process in
  `sandbox_policy.py` (an adapter declares `sandbox_requirements` as data
  and never emits a mount, and `sandboxed()` is the one place an
  adapter's launch is described); a REMOTE agent's candidate (what the controller verifies,
  pushes or publishes) is read only through `agent_worktree.py`, never
  through the worktree's agent-writable `.git` file;
  no raw `gh --json` dictionaries crossing out of `github.py`; no large
  prompt text in Python string literals. The scheduler modules
  (`scheduler.py`, `worker.py`, `executor_supervised.py`,
  `scheduler_stop.py`, `epic_commands.py`) name no provider, call no GitHub
  write, construct no effect record and parse no `CONTROL_RESULT`:
  everything an issue does is the engine's, in its worker (#182, #183;
  structural tests in `tests/test_scheduler.py`).
- The outcome of a GitHub write is read back from GitHub, never inferred
  from an exit status or an agent claim, and a destructive write is
  checkpointed in persisted state before it is performed. Persisted state is
  validated on load and corruption fails loudly; a new persisted field
  follows the same rule. The routed references specify both.
- LOCAL mode (`autoforge local`) makes zero `gh` invocations and has its own
  prompt templates (`prompts/local_*.md`) and transition table; do not let a
  GitHub-only concern leak into it. Its workspace and filesystem boundary is
  recorded in `docs/adr/0001-local-mode-workspace-identity-and-filesystem-boundary.md`;
  read that ADR before changing `local_workspace.py`, `run_contract.py` or
  `safefs.py`.

## Routing by module

The root **Routing rules** decide which reference to read; this is the same
table keyed by file, for a task that starts from a module name.

```text
transitions.py, profiles.py, loop_guard.py, replan.py,
certificate.py,
engine.py (phase sequencing, resume)             -> docs/agent-guides/workflow.md
replan_txn.py, engine.py (REPLAN_REEXECUTE),
prompts/replan_reexecute.md                       -> docs/agent-guides/replan-transaction.md (+ workflow.md)
github.py, claims.py, validation.py, premerge.py,
materialize.py, effect_ops.py, git_transport.py,
child_env.py, read_context.py, engine.py (post-phase
verification, merge gate, dry-run, read context),
prompts/update_epic*.md                           -> docs/agent-guides/github-safety.md
                                                     (+ docs/adr/0004-authority-boundary-and-
                                                     typed-external-effects.md for effects)
state.py, effects.py, evidence.py, certificate.py,
run_contract.py, safefs.py, config_fingerprint.py,
locking.py, errors.py,
engine.py (recovery, retry)                       -> docs/agent-guides/state-and-recovery.md
run_namespace.py, ownership.py, locking.py (lock
domains), scheduler_state.py,
engine.py (namespace binding)                     -> docs/agent-guides/state-and-recovery.md
                                                     + docs/adr/0008-parallel-epic-scheduler-
                                                     authority-run-identity-claims-and-locks.md
epic_graph.py, github.py (graph reads)           -> docs/agent-guides/github-safety.md
                                                     ("EPIC dependency graph") + docs/adr/0008-
                                                     parallel-epic-scheduler-authority-run-
                                                     identity-claims-and-locks.md
providers.py, pi_rpc.py, claude_stream.py,
progress.py, loop_detect.py, executor.py,
executor_duplex.py,
prompts/__init__.py                               -> docs/agent-guides/architecture.md
                                                     (+ docs/adr/0003-pi-agent-provider.md and
                                                     docs/pi-policy.md for Pi)
interaction.py, claude_sdk.py, opencode_sdk.py,
sdk_helpers/ (all planned)                        -> docs/agent-guides/architecture.md
                                                     + docs/adr/0007-sdk-providers-isolated-
                                                     helpers-and-interaction-contract.md
sandbox.py, sandbox_bwrap.py                      -> docs/agent-guides/architecture.md
                                                     (Executor) + docs/adr/0005-outer-
                                                     sandbox-and-host-resource-capability-
                                                     model.md and ADR 0002 §4e
sandbox_policy.py, providers.py
(sandbox_requirements), engine.py (launch
routing, sandbox directories, the pre-launch
gate), doctor_sandbox.py                          -> docs/agent-guides/architecture.md
                                                     (Launch routing) + ADR 0005 and
                                                     docs/configuration.md (Outer sandbox)
agent_worktree.py, engine.py (strict import,
candidate read)                                   -> docs/agent-guides/github-safety.md
                                                     and state-and-recovery.md + ADR 0005
                                                     D6
result_parser.py, prompts/*.md                    -> docs/agent-guides/control-result-protocol.md
redaction.py, runlog.py, doctor.py (env rows),
error messages                                    -> docs/agent-guides/secrets-and-logging.md
```
