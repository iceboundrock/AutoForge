# Test scope: tests/

Applies to everything under `tests/`. The root `AGENTS.md` contract applies
here too; this file adds only what is specific to writing tests. The
coverage contract (which behaviours must be tested, and the scripted
end-to-end loop that must keep working) is `docs/agent-guides/testing.md`;
read it before adding tests for a listed area.

## What a test may never do

- Never call real Claude Code, OpenCode, or Pi, and never call GitHub write
  APIs. Agents are replaced by scripted providers (Pi by the fake `pi` of
  `tests/pi_fake.py`, Claude's stream by the fake `claude` of
  `tests/claude_fake.py`) and GitHub by an in-memory fake; the executor tests
  use real local Python subprocesses only.
- Never merge a real PR, push to a real remote, or touch a checkout outside
  `tmp_path`.
- Never weaken an assertion, a safety gate, or a verification step to make an
  implementation pass. A test that documents a deliberately unfixed edge case
  says so in its docstring, as `tests/test_replan.py` does.

## Fixtures and fakes (`tests/conftest.py`)

- `ScriptedProvider` (`src/autoforge/providers.py`) handlers stand in for
  every agent call and do what the real agent would (commit, push a fix,
  answer with a result); `ExplodingGitHub` fails a test if LOCAL mode
  reaches for GitHub.
- `engine`, `fake_github`, `tmp_state_dir`, `repo` and `make_engine` /
  `make_local_engine` build a controller against those fakes; `block(...)`
  builds a well-formed `CONTROL_RESULT` block. The controller posts the
  review comment (#162): a reviewer returns `review_result(...)` (the full
  REMOTE `REVIEW` payload, prose included) and posts nothing, and
  `controller_review_comment(eng, round)` returns the one comment carrying
  that round's marker. `review_comment_body(...)` builds a comment someone
  else posted: an earlier round's evidence for a test that starts past
  `REVIEW`, or an unjournaled comment that blocks the round. Reuse these
  rather than hand-rolling fakes.
- `REVIEW` fetches the bound HEAD and merge base before launching the
  reviewer. An engine without an origin records that fetch in the
  `offline_fetches` fixture and fetches nothing; with `origin=True` it
  fetches from the origin, so a `FIX` there pushes a real commit
  (`push_fix`) rather than setting a fake HEAD.
- The controller pushes the implementation commit itself (#161), so a
  REMOTE test that reaches `ANALYZE_EXECUTE`'s publication builds its engine
  with `make_engine(..., origin=True)`: an `Origin` is a seeded bare
  repository under `tmp_path` that the controller's git transport fetches
  from and pushes to over `file://`, and that backs `FakeGitHub`'s branch
  heads, PR heads and ancestry reads. `implement` is the implementation
  agent (one commit on the worktree's detached `HEAD`, reported by
  `analyze_payload`), and `scripted(...)` answers successive calls of one
  agent. An autouse fixture fails any test whose engine would reach the
  network git remote.
- `tests/claude_fake.py` holds a fake `claude` that prints a scripted
  stream-json transcript (and logs its argv, cwd and stdin; `$HEAD` in the
  transcript is replaced by the `HEAD` of its cwd, such as a commit made
  before the run), and can leave
  a helper holding its stdout after it exits, silent or writing lines on an
  interval; the real
  `ClaudeCodeProvider` launches it, so the stream path is tested end to end
  without a real agent.
- `tests/pi_fake.py` holds the engine-level Pi harness: `PiFake` (a fake
  `pi` executable outside the repository that logs every start, argv, cwd,
  environment name and RPC record) and `ScriptedPi` (the real
  `PiProvider`, preceded by a `ScriptedProvider`-style handler that may
  return a `PiTurn` to script a failure); `make_pi_engine` / `route_to_pi`
  route profiles to it.
- Recovery tests reload state from disk after each step; a crash-recovery
  test performs the external side effect first and only then interrupts,
  because that is the window the controller must survive.

## Where a behaviour is tested

```text
transitions, routing, loop bounds     test_transitions.py, test_routing.py, test_loop_guard.py
engine verification, gate, recovery   test_engine.py, test_engine_locking.py, test_integration.py
replan policy and transaction         test_replan.py
GitHub client, pre-merge evidence     test_github.py, test_premerge.py
effect operations, git transport      test_effects.py, test_git_transport.py
state, contract, filesystem boundary  test_state.py, test_durable_run_contract.py, test_safefs.py
CONTROL_RESULT, prompts, providers    test_result_parser.py, test_prompts.py, test_providers.py
Pi RPC reducer and adapter (fake pi)  test_pi_rpc.py
Claude stream-json reducer            test_claude_stream.py (adapter on the fake: test_providers.py)
live progress, its redaction          test_progress.py
loop detector (unit and end to end)   test_loop_detect.py (adapters: test_providers.py,
                                      test_pi_rpc.py, test_claude_stream.py)
executor, lock, redaction, run logs   test_executor.py, test_executor_duplex.py, test_lock.py,
                                      test_redaction.py, test_runlog.py
LOCAL mode                            test_local*.py
config, CLI, doctor, CI drift guards  test_config.py, test_cli.py, test_doctor.py, test_ci_workflow.py, test_gitignore.py
```

Add a new test next to the behaviour's existing file; create a new file only
for a new subsystem. Run `make test` (or `uv run pytest tests/<file>`) and
`make lint`; `make check` runs the same commands as CI (including the
lockfile check) on one local interpreter, and `make check-matrix` runs
`pytest` on every CI Python. `test_ci_workflow.py` keeps the Makefile and
`.github/workflows/ci.yml` matching in both directions.
