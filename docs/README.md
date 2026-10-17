# AutoForge documentation

Start with the [project README](../README.md) for what AutoForge is,
installation and a quick start. This index lists the rest by what you want
to do. The documents under `agent-guides/` are written as the repository's
contract for contributors and coding agents; they are also the most precise
description of how AutoForge behaves.

## Using AutoForge

| Document | Read it for |
|---|---|
| [Getting started](../README.md#quick-start) | prerequisites, installation, `doctor`, a dry run, a first run, `resume` |
| [Running the remote workflow](usage.md) | every command, where a run stops (`READY_FOR_MERGE`, `BLOCKED`), opt-in merge, `unblock`, how agents are separated from your checkout (not sandboxed unless `sandbox.mode: required`), troubleshooting and recovery |
| [Local mode](local-mode.md) | the GitHub-free workflow over a feature Markdown file: the workspace fingerprint, dirty trees, the review/fix bound, validation commands, dry run and resume |
| [Configuration](configuration.md) | creating and passing a config file, formats, strict key validation, profiles, the `local:` block, the outer sandbox (`sandbox:`) and the provider credentials it shows; [`autoforge.example.yaml`](../autoforge.example.yaml) is the exhaustive key reference |

## Understanding AutoForge

| Document | Read it for |
|---|---|
| [Architecture](agent-guides/architecture.md) | the boundaries between the engine, provider adapters, executor, GitHub client and prompt system |
| [Workflow and state machine](agent-guides/workflow.md) | phases and legal transitions, review-round routing, loop bounds and stagnation, replan policy, review binding to PR / HEAD / base / merge base, re-entering a phase, leaving `BLOCKED` |
| [State and recovery](agent-guides/state-and-recovery.md) | the state file and its schema versions, atomic writes and corrupt-state handling, crash recovery, the repository lock, runtime artifacts and the state directory layout, error classification |
| [Module map](../src/autoforge/AGENTS.md) | which source module owns which concern |

## Safety and protocols

| Document | Read it for |
|---|---|
| [GitHub safety](agent-guides/github-safety.md) | GitHub as source of truth, per-phase verification of agent claims, dry-run guarantees, agents holding no GitHub credential (the operation classification, credential routes, threat model, read context), the controller-owned merge and its gate, `doctor`'s branch-rule check, EPIC updates |
| [CONTROL_RESULT protocol](agent-guides/control-result-protocol.md) | the machine-readable result block, per-phase required fields, payload bounds, findings versus observations |
| [Replan transaction](agent-guides/replan-transaction.md) | the durable `REPLAN_REEXECUTE` transaction: provenance, the checkpointed close, compensation and recovery |
| [Secrets and logging](agent-guides/secrets-and-logging.md) | what is redacted, and what may never be logged or committed |
| [Pi policy](pi-policy.md) | what an unattended Pi run may load (project trust, extensions, packages, MCP, context files, tools), its credentials, and the evidence for each decision |

## Contributing

| Document | Read it for |
|---|---|
| [Development](development.md) | `make` targets, CI, and what a green `ci` does and does not prove |
| [Testing](agent-guides/testing.md) | the coverage contract and high-priority test areas |
| [Test scope](../tests/AGENTS.md) | fixtures and fakes, and what a test may never do |
| [AGENTS.md](../AGENTS.md) | the repository contract: precedence, safety invariants, routing rules for which guide to read before a change |

## Architecture decisions

| Record | Decision |
|---|---|
| [ADR 0001](adr/0001-local-mode-workspace-identity-and-filesystem-boundary.md) | Local mode: workspace identity (a controller walk, not `git status`), the filesystem capability boundary, the durable run contract, threat model and known limitations |
| [ADR 0002](adr/0002-executor-nothing-outlives-the-invocation.md) | Executor: nothing an agent starts outlives its invocation |
| [ADR 0003](adr/0003-pi-agent-provider.md) | Pi agent provider: RPC over stdio, `provider/model` naming and read-back verification, `--no-session` with fresh-process corrections, a provider-neutral failure channel, coordination with #126 |
| [ADR 0004](adr/0004-authority-boundary-and-typed-external-effects.md) | Authority boundary: agents get no GitHub write authority, every externally visible write is a typed controller effect with a durable record and reconciliation before re-issue, a hardened controller git transport, a published-content policy, the agent read path, threat model and upgrade rules |
| [ADR 0005](adr/0005-outer-sandbox-and-host-resource-capability-model.md) | Outer sandbox (partly implemented: the policy compiler and Linux backend in #167; launch routing, provider declarations, the mode-independent git hardening and `sandbox.mode`'s `disabled` and `required` in #168; `preferred`, the pre-launch gate and the `doctor` rows not yet, #169): the isolation vocabulary, threat model, an allow-list root with canonicalization rules, the per-launch-class resource table, a read-only git common dir with a private object directory and strict import, provider auth projection, the private home, the network position, composition with ADR 0002, `sandbox.mode` and platform capability |
| [ADR 0006](adr/0006-candidate-validation-evidence-and-review-certificates.md) | Candidate validation evidence (partly implemented: the records, runner and schema in #172; the producer, REVIEW and LOCAL gates, the REVIEW evidence section and the override watermark in #173; the review certificate and the merge gate not yet, #174 and #175): what a `ValidationRecord` and a `ReviewCertificate` mean and do not mean, distinct REMOTE and LOCAL candidate identities, trusted validation definitions and execution policy, a byte-exact REMOTE export with a hash-back proof (also for the merge gate), the LOCAL convergence rule, controller gates around the producers, REVIEW and the merge gate with no new phase, the record and certificate schemas in `state.json`, the invalidation matrix, crash windows and the one state migration |
| [ADR 0007](adr/0007-sdk-providers-isolated-helpers-and-interaction-contract.md) | SDK providers (design, not yet implemented): the Claude Python `ClaudeSDKClient` and an OpenCode Node bridge as isolated helper processes under the existing executor, sandbox and containment, an OpenCode server the bridge owns, exact SDK and runtime pins that fail closed, the provider-neutral interaction contract and broker (#134) consumed by #126's single state bump, the `prompt` / `pause` / `fail` policies and their limits, accounting and session binding, recovery proof obligations, and a capability and recovery gate matrix |
