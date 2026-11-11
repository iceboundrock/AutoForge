# Source scope: src/autoforge/

Applies to production code in this package. The repository contract in the
root `AGENTS.md` (precedence, untrusted data, branch safety, boundaries,
conventions, routing rules) also applies here and is not repeated below.

## Module map

Read the module docstring before changing a module; this map only says where
a concern lives so the routing rules can be applied.

```text
cli.py              subcommands run / step / resume / unblock / status / doctor, dry-run flags;
                    the `epic` group (run / resume / status / stop / archive, #183;
                    recheck, #447) and the
                    hidden `epic _worker` entry point of a scheduler's worker (#182)
config.py           configuration loading and validation; owns config defaults and keys
doctor.py           read-only environment checks; the scheduler rows (settings, and with
                    `--epic` the coordination root and the EPIC's graph, #183)
doctor_sandbox.py   doctor's outer-sandbox rows: capability and mode outcome, policy by
                    class, provider declarations and sandboxed --version, operator paths,
                    hard links, sandbox directories, TCP listeners; read-only (#169)
engine.py           ControllerEngine: phase orchestration, verification calls, recovery
transitions.py      Phase enum, LEGAL_EDGES / UNBLOCK_TARGETS, decide_next_phase
profiles.py         execution-profile routing (which provider/model/effort per phase/round)
loop_guard.py       REVIEW/FIX loop bounds and stagnation (pure logic)
replan.py           replan policy and review-history collection (pure logic)
replan_txn.py       REPLAN_REEXECUTE durable transaction state and verifiers
providers.py        provider adapters: the only place that knows real CLI flags
pi_rpc.py           Pi RPC wire protocol: JSONL codec, request ids, event reducer,
                    outcome classification; provider layer, no flags and no processes (#131)
claude_stream.py    Claude stream-json reducer: result text, failure, summary, progress
                    events; provider layer, no flags and no processes (#192)
sdk_policy.py       SDK providers' unattended policy: tools, permissions, setting sources,
                    roots, credentials, environment, interaction-data bounds; provider
                    layer, pure: no process, file, SDK import or os.environ change (#149)
sdk_assets.py       the SDK helpers' packaged manifest (sdk_helpers/manifest.json: each
                    helper program, its envelope and protocol, whether it ships, the
                    pinned pair and interpreter floor), the helper digest check, and
                    doctor's read-only probes: the Claude environment probe and its
                    parser, the ._pth check (beside the interpreter, its venv's base,
                    its libpython: pyvenv.cfg, ELF dynamic section, loader cache), the
                    OpenCode runtime's package.json read (the runtime is never run);
                    provider layer, reads files only, runs nothing, imports no SDK (#150)
interaction.py      provider-neutral interaction contract (requests, decisions,
                    capabilities, lifecycle, session identity) and the per-launch
                    broker (policy, limits, degradation, capability check, run-log
                    record); no transport, no provider name, persists nothing (#134)
progress.py         provider-neutral ProgressEvent, cleaning, and the line renderer the
                    engine reports a launch through (#192)
loop_detect.py      provider-neutral loop detector: action fingerprints, line keys,
                    cycle / novelty / repeated-line / retry signals, warnings,
                    verdict and calibration; pure, reads no clock (#194)
executor.py         subprocess lifecycle, timeouts, a caller's stop, capture, optional
                    stdin payload or stdin file (never held in memory), optional outer
                    sandbox (no workflow semantics)
executor_duplex.py  duplex child handle for RPC transports: stdin records out, LF-framed
                    stdout records in, idle timeout and max runtime, ADR 0002 teardown;
                    bytes only (#130, #193)
sandbox.py          outer sandbox: the backend-neutral launch description, the policy
                    compiler (ADR 0005 D4.2, D4.3, D7.2) and the facts; executor layer,
                    no workflow semantics (#167)
sandbox_bwrap.py    the Linux backend: the only place that knows a `bwrap` flag; the
                    launch's keyring and Landlock scope, setup-failure detection, the
                    typed capability probe (#167, #169)
sandbox_backend.py  the backend seam (ADR 0013 D13): the backend-neutral lifecycle
                    interface (runtime check, reconcile, probe; plan, seed, create,
                    stage, the run step's prepared launch, export, removal), the
                    per-launch data its steps hand on (inputs, seed, the handle of
                    what was made, the binding a compiled launch carries to the
                    executor), its objects and their removal outcomes, bwrap behind
                    it unchanged; the only module that imports sandbox_bwrap;
                    executor layer, no journal, no workflow (#268)
sandbox_lifecycle.py  the engine's sequence of that interface: a command's entry
                    (runtime check, reconciliation, probe) and one launch's steps in
                    D13's order (plan, then the environment it establishes),
                    journaled before every create and removal, each container removed
                    before what it mounts, the release on every path, the volume kept
                    until verify's outcome is saved, an
                    uncertain removal raised as SandboxCleanupUncertainError; the
                    journal and verify-step protocols (#272, #269 implement them);
                    the seed step's end binding the git input to the seed's digest,
                    saved before any journal entry, and the seed handed on only
                    while its manifest keeps it (#269); no workflow (#268)
git_input.py        a launch's git input (ADR 0013 D4): the bound revision, typed and
                    immutable (phase, launch, repository, object format, seed, lower
                    boundary, carried commit, seed manifest digest), its entry and
                    persisted identity, the seed manifest's digest; plain data (#269)
candidate_artifact.py  an exported artifact (manifest and pack), the export bound, and
                    its strict, bounded, idempotent import into the shared store
                    (quarantine, index-pack --strict, ancestry and closure checks,
                    publication streamed under L1, D7.5 re-hash); the store checks of
                    a bound revision; git only, no workflow (#269)
candidate_source.py the engine's candidate source (ADR 0013 D5): each phase's bound
                    revision as a git input, the launch's export directory, the verify
                    step for an exported artifact (its origin authenticated first), the
                    run's candidate receipts beside state.json (seed identity, outcome,
                    carried commit), the two candidate reads (worktree HEAD for bwrap
                    and disabled, the imported artifact otherwise) (#269)
sandbox_policy.py   which host resources a launch gets by launch class (ADR 0005 §2.5):
                    LaunchFacts from the engine, ProviderRequirements from an adapter,
                    the SandboxPlan, the path-valued environment rewrite (D8.2),
                    executable and install-root resolution; no provider branch, no
                    workflow decision, launches nothing (#168); where a --config file
                    may lie under an enforced mode (config_file_problem, #280) and
                    what a provider's declaration makes writable (declared_writable)
agent_worktree.py   the agent worktree's git surface in every sandbox.mode: registry
                    binding and listing (D6.4, #223), candidate read through the
                    common dir (D6.3), the uncommitted-change reads through the bound
                    admin dir and against the reviewed tree's bytes (#428),
                    strict import of private objects (D6.5); git only through the
                    executor's hardened local request (#168)
github.py           GitHubClient over `gh`, the only holder of a GitHub credential:
                    verification reads, the reads behind an agent's read context, typed
                    controller writes (never retried on an ambiguous outcome) +
                    controller-owned merge and merged-head-branch deletion; every
                    `gh` runs in an empty directory of its own (`gh_workdir`, #283)
child_env.py        what every agent and repository-command launch gets instead of a
                    credential: forbidden env names, credential-looking allow-list
                    entries (#286), per-launch overrides (empty gh
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
run_binding.py      the EPIC a new standalone run records: the issue's native parent, else
                    `run --epic`; get_issue_node reads only (the issue again last), fails
                    closed, no write (#293)
epic_rebind.py      rebind-epic's decision for an existing standalone run: request,
                    safe phases (READY_FOR_MERGE, a quiescent BLOCKED), refusals of
                    EPIC-tied persisted state and of the run's own counted merge, the
                    parent reads and the recorded EPIC's progress comments, the journal
                    of applied rebinds and what a repeat is, the report and its run-log
                    record, status's parent warning; no I/O of its own (#435)
review_provenance.py  status's read-only report of an unjournaled review comment (ADR 0004
                    D9.6, #416): the state directories of the checkout that journaled it,
                    the PR's other review markers, the commits made after it, the PR's
                    implementation marker; GitHub reads and strict local loads only, names
                    no author, nothing that decides imports it
premerge.py         controller-produced pre-merge evidence (check definitions, tree export)
materialize.py      byte-exact export of a candidate commit from the shared object store
                    and its hash-back proof; the pre-merge export's writer (ADR 0006 D6, #171);
                    a working tree's blob-by-blob comparison with a commit, a gitlink
                    matching only as an empty directory (tree_differences)
evidence.py         candidate validation evidence: definitions, policy P, ValidationRecords,
                    the attempt marker, their bounds and integrity checks; pure (ADR 0006, #172)
certificate.py      review certificates: issuance from the review binding, applicability
                    (certificate_problem, also against the live PR the merge gate reads, #175)
                    and the not-a-merge-permission wording; pure (ADR 0006 D12, D13, #174)
result_parser.py    CONTROL_RESULT extraction and strict per-phase validation
state.py            persisted controller state, atomic writes, schema/protocol checks; the
                    protocol label's classification (protocol_support, #265)
provenance.py       the running controller's identity (imported package file, interpreter,
                    version, protocol) and the protocol-mismatch diagnostic the CLI prints;
                    reads and writes nothing (#265)
run_namespace.py    the parallel EPIC scheduler's per-issue run identities (RunKey) and
                    namespaces under <common>/autoforge/scheduler/issues/<n>: layout,
                    binding.json, read-only lookup and listing, the state archive, the
                    standalone state-dir refusal; no lock, claim or GitHub read (#178)
config_fingerprint.py  the recorded configuration of a REMOTE run (profile routing,
                    agent worktree root) that step/resume/unblock compare (#219)
sandbox_pin.py      the sandbox.mode every run is pinned to; step/resume/unblock refuse
                    a weaker one unless --accept-config-change (#280)
git_workspace.py    the controller's git reader of a working tree: root, git dirs, HEAD,
                    branch, status; hardened, no optional lock, reads no configuration
                    (ADR 0009 C1, #386)
safefs.py           filesystem capability boundary for every controller write
locking.py          the lock domains of ADR 0008 (#180): L0 (ControllerLock, keyed by the git
                    common dir; shared and adopted close-only; its lock-free probe for a
                    preview, #435), L2 (IssueRunLock and its lock-free probe), L1
                    (SharedGitLock around each common-dir git write),
                    the order assertion and the scheduler's filesystem refusal
ownership.py        who may act on an issue (ADR 0008 D17.2, #180): the forward guard's
                    ownership predicate (an open scheduler claim included, #181) and the
                    reverse guard's standalone-state read (a state of a protocol this
                    controller does not read admits nothing, #265)
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
                    open-claim refusal and --force, `epic stop`'s outcomes, `epic
                    archive` under L0 and L2, and `epic recheck` (#447): under L0 and L2,
                    one closed-gate engine step on an issue settled at READY_FOR_MERGE,
                    which reads its PR and launches nothing; no signal, no GitHub write
redaction.py        secret redaction for anything that reaches logs
runlog.py           per-invocation run logs, redacted
errors.py           typed error taxonomy
stop_classification.py  typed stop classification (ADR 0011, #369): outcomes, reason
                    codes, their source and the operator's routes, from a sink's code,
                    a CONTROL_RESULT status, an error's class or a loaded state (with
                    the code its sink saved, stop_code, #374); advice that nothing
                    deciding imports; pure, persists nothing itself
recovery_package.py status's recovery action package (#374): from a loaded state, its
                    classification, the configuration and, for 'status --live',
                    unblock's dry-run decision: the diagnosis of the saved stop code,
                    what is proven, pending (exact ids, targets, payload references),
                    forbidden and not to be touched, and the next commands as one
                    sequence unblock accepts, each claim labelled snapshot, live,
                    configuration or unknown, every quoted text redacted before it is
                    bounded; the labels status gives K9, K10 and K12 records; only the
                    CLI imports it; pure, persists nothing
prompts/            file-based templates ({{VAR}}): common.md (trust boundary), phase
                    templates, update_epic_rerequest.md, correction.md,
                    worktree_state.md (#187), claude_background_work.md (#440, the
                    section ClaudeCodeProvider names); renderer in prompts/__init__.py
```

## Rules for production code

- Every behavioural change ships with tests under `tests/` (see
  `tests/AGENTS.md`). Do not change orchestration semantics, verification, or
  a safety gate as an incidental part of another change.
- Keep the boundaries: no CLI flags outside `providers.py`, and no
  provider wire-protocol names outside the provider layer (`providers.py`
  plus the protocol modules it owns, `pi_rpc.py` and `claude_stream.py`);
  no workflow semantics or JSON in `executor.py` or `executor_duplex.py`,
  and none in `sandbox.py`, `sandbox_bwrap.py`, `sandbox_backend.py` or
  `sandbox_lifecycle.py` either; no `bwrap` flag outside `sandbox_bwrap.py`,
  and no import of it outside `sandbox_backend.py`: the engine, the
  executors and `doctor` reach a backend through the seam (#268); no
  provider branch or process in
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
- `safefs.py` is the capability boundary of the controller's writes to a
  state directory, its run logs and read context, and of the scheduler's
  namespaces. Its design is recorded in §5.4 of
  `docs/adr/0001-local-mode-workspace-identity-and-filesystem-boundary.md`,
  the part of that record ADR 0009 keeps; read it before changing
  `safefs.py`. LOCAL mode is removed
  (`docs/adr/0009-removing-local-mode-strict-refusal-protocol-and-sequencing.md`):
  a LOCAL artifact meets only the generic refusals (argparse's subcommand
  choices, the unknown configuration key, the state loader's mode and
  field checks, the evidence parsers' kind checks). Do not add code that
  recognises, shows or converts one.

## Routing by module

The root **Routing rules** decide which reference to read; this is the same
table keyed by file, for a task that starts from a module name.

```text
transitions.py, profiles.py, loop_guard.py, replan.py,
certificate.py,
engine.py (phase sequencing, resume)             -> docs/agent-guides/workflow.md
replan_txn.py, engine.py (REPLAN_REEXECUTE),
prompts/replan_reexecute.md                       -> docs/agent-guides/replan-transaction.md (+ workflow.md)
github.py, claims.py, validation.py, run_binding.py,
epic_rebind.py, review_provenance.py, premerge.py,
materialize.py, effect_ops.py,
git_transport.py, child_env.py, read_context.py, engine.py (post-phase
verification, merge gate, dry-run, read context),
prompts/update_epic*.md                           -> docs/agent-guides/github-safety.md
                                                     (+ docs/adr/0004-authority-boundary-and-
                                                     typed-external-effects.md for effects)
state.py, provenance.py, effects.py, evidence.py,
certificate.py, safefs.py, config_fingerprint.py,
sandbox_pin.py, locking.py, errors.py,
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
interaction.py, cli.py (TerminalResolver),
claude_sdk.py, opencode_sdk.py (planned),
sdk_helpers/ (the manifest; the helper programs
planned)                                          -> docs/agent-guides/architecture.md
                                                     + docs/adr/0007-sdk-providers-isolated-
                                                     helpers-and-interaction-contract.md
sdk_policy.py, sdk_assets.py, providers.py
(ClaudeSdkProvider, OpenCodeSdkProvider),
doctor.py (the SDK rows)                          -> docs/sdk-policy.md + docs/adr/0007-sdk-
                                                     providers-isolated-helpers-and-
                                                     interaction-contract.md
sandbox.py, sandbox_bwrap.py                      -> docs/agent-guides/architecture.md
                                                     (Executor) + docs/adr/0005-outer-
                                                     sandbox-and-host-resource-capability-
                                                     model.md and ADR 0002 §4e
sandbox_backend.py, sandbox_lifecycle.py,
git_input.py, candidate_artifact.py,
candidate_source.py                               -> the same, + docs/adr/0013-container-
                                                     sandbox-backend-lifecycle-trust-
                                                     boundaries-and-rollout.md (D4, D5, D13,
                                                     D16)
sandbox_policy.py, providers.py
(sandbox_requirements), engine.py (launch
routing, sandbox directories, the pre-launch
gate), doctor_sandbox.py                          -> docs/agent-guides/architecture.md
                                                     (Launch routing) + ADR 0005 and
                                                     docs/configuration.md (Outer sandbox)
agent_worktree.py, git_workspace.py, engine.py
(strict import, candidate read, checkout anchor) -> docs/agent-guides/github-safety.md
                                                     and state-and-recovery.md + ADR 0005
                                                     D6
result_parser.py, prompts/*.md                    -> docs/agent-guides/control-result-protocol.md
stop_classification.py, engine.py (_block and
_blocked codes, the agent-status sink),
cli.py (stop lines), errors.py (a new class),
recovery_package.py, engine.py (unblock_preview) -> docs/adr/0011-typed-stop-classification-
                                                     and-recovery-baseline.md (+ docs/adr/0012-
                                                     recovery-and-escalation-stop-authority-
                                                     and-advisor-gates.md for the package)
redaction.py, runlog.py, doctor.py (env rows),
error messages                                    -> docs/agent-guides/secrets-and-logging.md
```
