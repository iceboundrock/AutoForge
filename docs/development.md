# Development

How to set up a development environment, run the checks CI runs, and what
the hosted `ci` check does and does not prove. The testing contract (what
must be covered, and what a test may never do) is in
[testing.md](agent-guides/testing.md) and
[tests/AGENTS.md](../tests/AGENTS.md).

## Commands

```bash
make sync         # uv sync (.venv + dev tools)
make test         # uv run pytest
make lint         # uv run ruff check src tests
make fmt          # uv run ruff format src tests
make fmt-check    # uv run ruff format --check src tests
make typecheck    # uv run mypy src
make lock-check   # uv lock --check (what CI's `uv sync --locked` enforces)
make check        # lock-check + the four CI commands, on the local interpreter
make check-matrix # pytest on every CI Python (3.11, 3.12), opt-in
```

Run `make check` before declaring a change complete, and `make check-matrix`
when a change could be interpreter-sensitive.

## Continuous integration

The same checks run hosted on every pull request and every push to
`main` (`.github/workflows/ci.yml`), each after a locked install
(`uv sync --locked`): `pytest` on Python 3.11 and 3.12, and `ruff check` /
`ruff format --check` / `mypy` once. `make check` runs the same commands
plus the lockfile check, but only on the interpreter the local environment
resolves (`.python-version`); `make check-matrix` runs `pytest` on each CI
interpreter in its own isolated environment (`uv run --isolated --locked
--python <v>`), leaving `.venv` alone. The versions live in the Makefile's
`CI_PYTHON_VERSIONS`; `tests/test_ci_workflow.py` fails when that list, the
workflow's matrix, or the mirrored commands drift apart.

The workflow needs no secrets and is granted none. Its aggregate `ci` job is
a single stable check name that survives adding or renaming a matrix entry,
and it is required on `main` by a repository ruleset. That is what gives the
controller's pre-merge gate ("every check on the PR succeeded") something
real to verify instead of a vacuously green PR. The same ruleset requires a
pull request (with zero required approvals, since GitHub forbids
self-approval and any higher count would deadlock the controller's own
merge) and blocks force-push and deletion of `main`. Because that ruleset is
repository configuration rather than code, `autoforge doctor` re-reads it
(see [GitHub safety: doctor's branch-rule check](agent-guides/github-safety.md#doctors-branch-rule-check))
so that disabling it, renaming the context or adding a bypass actor is
noticed before an unattended run relies on it.

## What a green `ci` does and does not prove

It proves the suite passed on GitHub's runners for that commit, which is
strictly more than an agent's claim that it ran the tests. It is not a
signal independent of the PR: the workflow that defines the check, and the
code the check runs, both come from the PR. Four things bound that.

- The controller refuses to merge a PR that touches
  `safety.protected_merge_paths` (default `.github/workflows/`), so a PR
  cannot redefine the check that clears it; that refusal lives in the
  controller, in version control and under test, rather than in a repository
  setting that can drift unnoticed.
- With `safety.verify_check_definition` it also compares the jobs and steps
  the `ci` run actually executed with the base branch's own run of the same
  workflow, so a check that was redefined through something the protected
  paths do not cover is refused too.
- With `merge.verification_commands` set, the controller runs the
  repository's own checks (`pytest`, `ruff`, `mypy`, …) on an export of the
  reviewed commit before it merges: evidence it produced itself, not a check
  name it read.
- A PR that weakens what its tests assert still has to pass the review
  phase, whose findings are what the loop bounds act on.

None of this replaces a human reading the diff (the local commands still run
the PR's tests, just under the controller's eye rather than the PR's
workflow), which is why `safety.allow_merge` is off by default. The gates
themselves are specified in
[GitHub safety: merge safety](agent-guides/github-safety.md#merge-safety).

## Testing

Tests never call real Claude Code, OpenCode or GitHub write APIs. Agents are
replaced by a `ScriptedProvider` and GitHub by an in-memory fake; the
executor tests use real local Python subprocesses only.

- [testing.md](agent-guides/testing.md): the coverage contract, including
  the high-priority areas every behavioural change there must test.
- [tests/AGENTS.md](../tests/AGENTS.md): fixtures and fakes, what a test may
  never do, and where each behaviour is tested.

## Where the code lives

The module map (which module owns what) is in
[src/autoforge/AGENTS.md](../src/autoforge/AGENTS.md); the responsibility
boundaries between the engine, provider adapters, executor, GitHub client
and prompt system are in [architecture.md](agent-guides/architecture.md).
Contributor rules for the whole repository are in
[AGENTS.md](../AGENTS.md).
