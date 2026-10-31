# AutoForge documentation

Start with the [project README](../README.md) for what AutoForge is,
installation and a quick start. This index lists the rest by what you want
to do. The documents under `agent-guides/` are written as the repository's
contract for contributors and coding agents; they are also the most precise
description of how AutoForge behaves.

## Using AutoForge

| Document | Read it for |
|---|---|
| [Getting started](getting-started.md) | a first run on a repository of your own, step by step: prerequisites, installing, a repository (a new private one, or an existing one you may change) with an EPIC and an issue, a starter configuration ([`examples/`](../examples)), what each `doctor` row means, a dry run, a real run, `READY_FOR_MERGE` and `BLOCKED`, and the common first-run failures with their fixes |
| [Compatibility](compatibility.md) | what AutoForge runs on and the evidence for each claim: OS and architecture, Python, git, `gh` and its token, each agent CLI's minimum and verified versions, the outer sandbox's host requirements, the filesystem, provider authentication and billing |
| [Running the remote workflow](usage.md) | every command, where a run stops (`READY_FOR_MERGE`, `BLOCKED`), opt-in merge, `unblock`, how agents are separated from your checkout (not sandboxed unless `sandbox.mode: required`), troubleshooting and recovery |
| [Pi operator guide](pi.md) | running phases through Pi on a DeepSeek API key (its first target) or a ChatGPT subscription with no API key: installing Pi and giving it a credential yourself, a profile to copy for each, model and thinking level, `doctor`'s Pi rows and their remedies, security, limitations, unattended use, and the opt-in live smoke test |
| [Configuration](configuration.md) | creating and passing a config file, formats, strict key validation, profiles, the outer sandbox (`sandbox:`) and the provider credentials it shows; [`autoforge.example.yaml`](../autoforge.example.yaml) is the exhaustive key reference |

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
| [Security boundaries](security.md) | start here before a first run: what AutoForge protects and what it does not (agents run as you, the same-user residual, prompt injection on public repositories, the opt-in sandbox, the redaction baseline, what goes to model vendors), the GitHub token permissions it needs, the runtime files to keep out of git, and recommended settings |
| [Security policy](../SECURITY.md) | supported versions, how to report a vulnerability privately, what to expect, what is in and out of scope |
| [GitHub safety](agent-guides/github-safety.md) | GitHub as source of truth, per-phase verification of agent claims, dry-run guarantees, agents holding no GitHub credential (the operation classification, credential routes, threat model, read context), the controller-owned merge and its gate, `doctor`'s branch-rule check, EPIC updates |
| [CONTROL_RESULT protocol](agent-guides/control-result-protocol.md) | the machine-readable result block, per-phase required fields, payload bounds, findings versus observations |
| [Replan transaction](agent-guides/replan-transaction.md) | the durable `REPLAN_REEXECUTE` transaction: provenance, the checkpointed close, compensation and recovery |
| [Secrets and logging](agent-guides/secrets-and-logging.md) | what is redacted, and what may never be logged or committed |
| [Pi policy](pi-policy.md) | what an unattended Pi run may load (project trust, extensions, packages, MCP, context files, tools), its credentials, and the evidence for each decision |
| [SDK policy](sdk-policy.md) | what an unattended `claude-sdk` or `opencode-sdk` launch may load (settings, hooks, MCP, plugins, skills, instructions), its tools and permissions, configuration and session roots, credentials and environment, the interaction-data bounds, and the evidence for each decision |

## Contributing

| Document | Read it for |
|---|---|
| [Development](development.md) | `make` targets, fast feedback while working, CI, the built-distribution check, and what a green `ci` does and does not prove |
| [Releasing](releasing.md) | the distribution name, the version and tag scheme, the changelog, and the maintainer's procedure for building, tagging, publishing and verifying a release; the `pypi-publish` workflow, Trusted Publishing's one-time PyPI and GitHub setup, troubleshooting, and why the first upload is not the Public Alpha's Go |
| [Clean-room first run](clean-room.md) | checking that the published documents alone get a newcomer from nothing to a first run (Gates 1 and 2 of #255): the environment to start from, the target repository (new or existing, and what an existing one requires), the steps, what to capture and redact, and the friction log |
| [Testing](agent-guides/testing.md) | the coverage contract and high-priority test areas |
| [Test scope](../tests/AGENTS.md) | fixtures and fakes, and what a test may never do |
| [AGENTS.md](../AGENTS.md) | the repository contract: precedence, safety invariants, routing rules for which guide to read before a change |
| [Non-code artifacts](non-code-rules.md) | writing and publishing issues, PR descriptions, specs, plans and reviews; language and translation rules |

## Architecture decisions

| Record | Decision |
|---|---|
| [ADR 0001](adr/0001-local-mode-workspace-identity-and-filesystem-boundary.md) | Local mode (superseded by ADR 0009 except §5.4, §5.5's run-log and state-file bounds, §8.8 and §8.10, which still describe `safefs.py`): workspace identity (a controller walk, not `git status`), the filesystem capability boundary, the durable run contract, threat model and known limitations |
| [ADR 0002](adr/0002-executor-nothing-outlives-the-invocation.md) | Executor: nothing an agent starts outlives its invocation |
| [ADR 0003](adr/0003-pi-agent-provider.md) | Pi agent provider: RPC over stdio, `provider/model` naming and read-back verification, `--no-session` with fresh-process corrections, a provider-neutral failure channel, coordination with #126 |
| [ADR 0004](adr/0004-authority-boundary-and-typed-external-effects.md) | Authority boundary (implemented: #160–#165, and the amendments of #211, #243 and #283): agents get no GitHub write authority, every externally visible write is a typed controller effect with a durable record and reconciliation before re-issue (and #243's operator-performed comment, which the controller only verifies), a hardened controller git transport, a published-content policy, the agent read path, threat model and upgrade rules |
| [ADR 0005](adr/0005-outer-sandbox-and-host-resource-capability-model.md) | Outer sandbox (implemented: the policy compiler and Linux backend in #167; launch routing, provider declarations, the mode-independent git hardening and `sandbox.mode`'s `disabled` and `required` in #168; `preferred`, the pre-launch capability gate, `sandbox.read_only_paths` and the `doctor` rows in #169): the isolation vocabulary, threat model, an allow-list root with canonicalization rules, the per-launch-class resource table, a read-only git common dir with a private object directory and strict import, provider auth projection, the private home, the network position, composition with ADR 0002, `sandbox.mode` and platform capability |
| [ADR 0006](adr/0006-candidate-validation-evidence-and-review-certificates.md) | Candidate validation evidence (implemented: the byte-exact REMOTE export and its hash-back proof in #171; the records, runner and schema in #172; the producer, REVIEW and LOCAL gates, the REVIEW evidence section and the override watermark in #173; the review certificate, entering `READY_FOR_MERGE` and LOCAL `DONE` in #174; the merge gate's certificate check, drift and recovery in #175; its LOCAL decisions superseded by ADR 0009): what a `ValidationRecord` and a `ReviewCertificate` mean and do not mean, distinct REMOTE and LOCAL candidate identities, trusted validation definitions and execution policy, a byte-exact REMOTE export with a hash-back proof (also for the merge gate), the LOCAL convergence rule, controller gates around the producers, REVIEW and the merge gate with no new phase, the record and certificate schemas in `state.json`, the invalidation matrix, crash windows and the one state migration |
| [ADR 0007](adr/0007-sdk-providers-isolated-helpers-and-interaction-contract.md) | SDK providers (partly implemented: #149's policy, configuration and roots, [SDK policy](sdk-policy.md), and #134's interaction contract, broker and terminal resolver; the helpers and pause not yet): the Claude Python `ClaudeSDKClient` and an OpenCode Node bridge as isolated helper processes under the existing executor, sandbox and containment, an OpenCode server the bridge owns, exact SDK and runtime pins that fail closed, the provider-neutral interaction contract and broker (#134) consumed by #126's single state bump, the `prompt` / `pause` / `fail` policies and their limits, accounting and session binding, recovery proof obligations, and a capability and recovery gate matrix |
| [ADR 0008](adr/0008-parallel-epic-scheduler-authority-run-identity-claims-and-locks.md) | Parallel EPIC scheduler (implemented: #178's run identities and namespaces, #179's dependency snapshots and ready set, #180's lock domains, #181's scheduler claims and restart reconciliation, #182's bounded worker supervisor and #183's `epic` commands and end-to-end recovery tests): the scheduler decides eligibility, capacity and ownership while each issue runs through the unchanged engine in a worker process, the GitHub-native graph and ready set, a fixed per-issue namespace under the git common dir, durable scheduler claims, live ownership as a per-issue `flock`, the lock domains L0 / L2 / L1 and the shared-Git critical sections, why no extra fence is needed before controller effects, restart classification and crash windows, workers stopping before `MERGE`, Ctrl-C and `epic stop`, compatibility with standalone commands |
| [ADR 0009](adr/0009-removing-local-mode-strict-refusal-protocol-and-sequencing.md) | Removing LOCAL mode (complete breaking removal approved on #384 for EPIC #383; implemented through S3: the public surface removed in #387, LOCAL's code, templates and state fields in #388 with state protocol 13; S4's measurement and documentation audit remain for #389): what is deleted, kept or replaced first, and where an existing REMOTE check refuses a LOCAL artifact, with the reference inventory of `src/`, the tests and the documents; no compatibility layer, no `autoforge local` stub, no LOCAL state discovery and no fallback release; a `mode: "LOCAL"` state refused by the ordinary loader with nothing written, and `run --force` audited; no LOCAL evidence becoming REMOTE evidence; `preferred` meaning `required`; the state protocol's next number with every older refusal kept; release communication and manual clean-up; the steps and their exit tests; the preservation owner of each shared safety test; the suite's baseline before the removal |
| [ADR 0010](adr/0010-fast-feedback-and-full-validation-evidence.md) | Fast feedback and full validation (proposed with #342; the repository tooling implemented, the controller FAST pre-check designed and gated): a FAST run is feedback and never a `ValidationRecord`, binding, certificate or CI result; the controller alone selects FULL from trusted configuration; CI unchanged; `make test-fast` and its explicit coverage mapping, which never narrows what it does not know; a change-based selection is base-bound; the decision table for candidate changes, failures, stale evidence, resume and merge; what a controller FAST pre-check must meet and the measurement that decides whether to build it |
