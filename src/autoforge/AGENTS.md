# Source scope: src/autoforge/

Applies to production code in this package. The repository contract in the
root `AGENTS.md` (precedence, untrusted data, branch safety, boundaries,
conventions, routing rules) also applies here and is not repeated below.

## Module map

Read the module docstring before changing a module; this map only says where
a concern lives so the routing rules can be applied.

```text
cli.py              subcommands run / step / resume / status / doctor / local, dry-run flags
config.py           configuration loading and validation; owns config defaults and keys
doctor.py           read-only environment checks
engine.py           ControllerEngine: phase orchestration, verification calls, recovery
transitions.py      Phase enum, LEGAL_EDGES / LOCAL_LEGAL_EDGES, next_phase
profiles.py         execution-profile routing (which provider/model/effort per phase/round)
loop_guard.py       REVIEW/FIX loop bounds and stagnation (pure logic)
replan.py           replan policy and review-history collection (pure logic)
replan_txn.py       REPLAN_REEXECUTE durable transaction state and verifiers
providers.py        provider adapters: the only place that knows real CLI flags
executor.py         subprocess lifecycle, timeouts, capture (no workflow semantics)
github.py           GitHubClient over `gh`: verification reads + controller-owned merge
claims.py           durable GitHub claims: marker schemas, renderers, scan, cardinality
validation.py       typed GitHub URL parsing and run-argument validation
premerge.py         controller-produced pre-merge evidence (check definitions, tree export)
result_parser.py    CONTROL_RESULT extraction and strict per-phase validation
state.py            persisted controller state, atomic writes, schema/protocol checks
run_contract.py     the durable contract of a LOCAL run
local_workspace.py  LOCAL mode working-tree verification
safefs.py           filesystem capability boundary for every controller write
locking.py          repository lock keyed by the git common dir
redaction.py        secret redaction for anything that reaches logs
runlog.py           per-invocation run logs, redacted
errors.py           typed error taxonomy
prompts/            file-based templates ({{VAR}}); renderer in prompts/__init__.py
```

## Rules for production code

- Every behavioural change ships with tests under `tests/` (see
  `tests/AGENTS.md`). Do not change orchestration semantics, verification, or
  a safety gate as an incidental part of another change.
- Keep the boundaries: no CLI flags outside `providers.py`; no workflow
  semantics in `executor.py`; no raw `gh --json` dictionaries crossing out of
  `github.py`; no large prompt text in Python string literals.
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
engine.py (phase sequencing, resume)             -> docs/agent-guides/workflow.md
replan_txn.py, engine.py (REPLAN_REEXECUTE),
prompts/replan_reexecute.md                       -> docs/agent-guides/replan-transaction.md (+ workflow.md)
github.py, claims.py, validation.py, premerge.py,
engine.py (post-phase verification, merge gate,
dry-run), prompts/update_epic.md                  -> docs/agent-guides/github-safety.md
state.py, run_contract.py, safefs.py, locking.py,
errors.py, engine.py (recovery, retry)            -> docs/agent-guides/state-and-recovery.md
providers.py, executor.py, prompts/__init__.py    -> docs/agent-guides/architecture.md
result_parser.py, prompts/*.md                    -> docs/agent-guides/control-result-protocol.md
redaction.py, runlog.py, error messages           -> docs/agent-guides/secrets-and-logging.md
```
