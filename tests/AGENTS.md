# Test scope: tests/

Applies to everything under `tests/`. The root `AGENTS.md` contract applies
here too; this file adds only what is specific to writing tests. The
coverage contract (which behaviours must be tested, and the scripted
end-to-end loop that must keep working) is `docs/agent-guides/testing.md`;
read it before adding tests for a listed area.

## What a test may never do

- Never call real Claude Code, OpenCode, or GitHub write APIs. Agents are
  replaced by scripted providers and GitHub by an in-memory fake; the
  executor tests use real local Python subprocesses only.
- Never merge a real PR, push to a real remote, or touch a checkout outside
  `tmp_path`.
- Never weaken an assertion, a safety gate, or a verification step to make an
  implementation pass. A test that documents a deliberately unfixed edge case
  says so in its docstring, as `tests/test_replan.py` does.

## Fixtures and fakes (`tests/conftest.py`)

- `ScriptedProvider` (`src/autoforge/providers.py`) handlers stand in for
  every agent call and mutate `FakeGitHub` the way the real agent would
  (create PR, post comment, push fix); `ExplodingGitHub` fails a test if
  LOCAL mode reaches for GitHub.
- `engine`, `fake_github`, `tmp_state_dir`, `repo` and `make_engine` /
  `make_local_engine` build a controller against those fakes; `block(...)`
  and `review_comment_body(...)` build well-formed `CONTROL_RESULT` blocks and
  review comments. Reuse these rather than hand-rolling fakes.
- Recovery tests reload state from disk after each step; a crash-recovery
  test performs the external side effect first and only then interrupts,
  because that is the window the controller must survive.

## Where a behaviour is tested

```text
transitions, routing, loop bounds     test_transitions.py, test_routing.py, test_loop_guard.py
engine verification, gate, recovery   test_engine.py, test_engine_locking.py, test_integration.py
replan policy and transaction         test_replan.py
GitHub client, pre-merge evidence     test_github.py, test_premerge.py
state, contract, filesystem boundary  test_state.py, test_durable_run_contract.py, test_safefs.py
CONTROL_RESULT, prompts, providers    test_result_parser.py, test_prompts.py, test_providers.py
executor, lock, redaction, run logs   test_executor.py, test_lock.py, test_redaction.py, test_runlog.py
LOCAL mode                            test_local*.py
config, CLI, doctor, CI drift guards  test_config.py, test_cli.py, test_doctor.py, test_ci_workflow.py, test_gitignore.py
```

Add a new test next to the behaviour's existing file; create a new file only
for a new subsystem. Run `make test` (or `uv run pytest tests/<file>`) and
`make lint`; `make check` runs the same commands as the hosted CI, which
additionally installs from the lockfile and tests on every interpreter in its
matrix (`.github/workflows/ci.yml`).
