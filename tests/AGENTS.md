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
- `REVIEW` and `FIX` fetch the bound HEAD (and `REVIEW` the merge base)
  before launching the agent. An engine without an origin records that
  fetch in the `offline_fetches` fixture and fetches nothing; with
  `origin=True` it fetches from the origin.
- The controller publishes `FIX` itself (#163): the fixer commits on its
  worktree's detached `HEAD` and the controller creates the follow-up
  issues, appends the markers and pushes the commit. A test that reaches
  `FIX`'s publication builds its engine with `origin=True` and starts the
  PR at a real commit (`publish_pr_head`; a human's push to the PR branch
  is the same helper). `fixer(...)` is the fixer (one commit on the
  reviewed HEAD, or none with `commit=False`, then the result), answering
  with resolutions built by `fixed`, `new_follow_up`, `deferred_to` and
  `no_change`; `fix_commit` and `fix_payload` build a handler by hand.
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
- The controller publishes `REPLAN_REEXECUTE` too (#164): the replan agent
  commits on the base its prompt names and the controller pushes it to the
  derived replacement branch and creates the replacement PR. In
  `tests/test_replan.py`, `_park_at_hard_threshold` builds the engine with
  `origin=True` and the source PR at a real commit (`eng.source`, built on
  `eng.merge_base`), and `_replan_agent(...)` is the reviewer that triggers
  the replan followed by that agent (`commit=False` for none, `during` for
  a write while it runs, `payload_over` to change its result).
  `_published_marker` and `_controller_body` give the marker and body the
  controller renders.
- `tests/claude_fake.py` holds a fake `claude` that prints a scripted
  stream-json transcript (and logs its argv, cwd and stdin; `$HEAD` in the
  transcript is replaced by the `HEAD` of its cwd, such as a commit made
  before the run), and can leave
  a helper holding its stdout after it exits, silent or writing lines on an
  interval; the real
  `ClaudeCodeProvider` launches it, so the stream path is tested end to end
  without a real agent. `fake_claude_bash_tool` there is a fake `claude`
  whose Bash tool runs one command the way 2.1.296 does: settings `env` by
  `--setting-sources`, its choice of shell, the shell snapshot of
  `~/.bashrc` or `~/.zshrc` (or a login shell without one),
  `CLAUDE_ENV_FILE` and `CLAUDE_CODE_SHELL_PREFIX`, with the real bash and
  zsh.
- `tests/pi_fake.py` holds the engine-level Pi harness: `PiFake` (a fake
  `pi` executable outside the repository that logs every start, argv, cwd,
  environment name and RPC record) and `ScriptedPi` (the real
  `PiProvider`, preceded by a `ScriptedProvider`-style handler that may
  return a `PiTurn` to script a failure); `make_pi_engine` / `route_to_pi`
  route profiles to it.
- Every launch gets no GitHub credential (#165). Autouse fixtures point
  the preflight's home (its `~/.netrc` and the `HOME` its login-shell probe
  runs with), OpenCode's data directory (where the organization check
  looks) and Claude Code's settings and global configuration (where its
  configuration check looks) at empty directories, so a test never reads
  the operator's. The
  probe runs the real `sh`, `bash` and `zsh`: a fake runner hands a request
  `is_shell_probe(req)` recognises to the real `execute`, as a fake runner
  does for nothing else, and a launch without a fake runner reuses the
  answer of an earlier one with the same home contents and environment
  (`_one_shell_probe_per_answer`); `tests/test_child_env.py` calls the
  probe itself. `pollute_operator_environment` fills the controller's
  environment with every credential route; `assert_isolated_launch(eng,
  req)` / `launch_environment` check what a launch would get,
  `env_probe_command` is a validation or pre-merge command that records its
  environment (`env_probe_records`), and `read_context_of(prompt)` returns
  the read-context directory a rendered prompt names. `tests/pi_fake.py`
  logs an `isolation` record for every Pi start. `tests/test_child_env.py`
  has PATH-injected fake `gh`, `ssh`, `curl`, `wget` and credential helper
  (`Probes`) that succeed only when a credential route reaches them, and
  runs the real `curl` and `wget`, where installed, against a loopback
  server that records the header they send (`--resolve` maps `github.com`
  to it for a netrc entry). `tests/test_providers.py` has a fake
  `opencode` that resolves its configuration, organizations cached in a
  2.0.26-shaped database included, as 2.0.26 does.
- Recovery tests reload state from disk after each step; a crash-recovery
  test performs the external side effect first and only then interrupts,
  because that is the window the controller must survive.

## Where a behaviour is tested

```text
transitions, routing, loop bounds     test_transitions.py, test_routing.py, test_loop_guard.py
engine verification, gate, recovery   test_engine.py, test_engine_locking.py, test_integration.py
replan policy and transaction         test_replan.py
GitHub client, pre-merge evidence     test_github.py, test_premerge.py
byte-exact export and its proof       test_materialize.py (the merge gate's: test_premerge.py,
                                      test_engine.py)
effect operations, git transport      test_effects.py, test_git_transport.py
state, contract, filesystem boundary  test_state.py, test_durable_run_contract.py, test_safefs.py
REMOTE run's recorded configuration   test_config_fingerprint.py (CLI: test_cli.py; load and
                                      protocol: test_state.py)
candidate validation evidence         test_evidence.py (pure), test_validation_evidence.py
                                      (engine, REMOTE and LOCAL; load and protocol: test_state.py)
CONTROL_RESULT, prompts, providers    test_result_parser.py, test_prompts.py, test_providers.py
Pi RPC reducer and adapter (fake pi)  test_pi_rpc.py
Claude stream-json reducer            test_claude_stream.py (adapter on the fake: test_providers.py)
live progress, its redaction          test_progress.py
loop detector (unit and end to end)   test_loop_detect.py (adapters: test_providers.py,
                                      test_pi_rpc.py, test_claude_stream.py)
executor, lock, redaction, run logs   test_executor.py, test_executor_duplex.py, test_lock.py,
                                      test_redaction.py, test_runlog.py
outer sandbox: compiler, wiring on a  test_sandbox.py (real bwrap, skipped with the probe's
fake bwrap, the typed probe; the real reason where the host cannot enforce:
backend; routing; every provider's    test_sandbox_bwrap.py, test_sandbox_routing.py,
boundary alike; the gate and modes;   test_sandbox_parity.py; `make test-sandbox` fails
doctor's rows                         instead), test_engine_sandbox.py,
                                      test_sandbox_gate.py, test_doctor_sandbox.py
LOCAL mode                            test_local*.py
config, CLI, doctor, CI drift guards  test_config.py, test_cli.py, test_doctor.py, test_ci_workflow.py, test_gitignore.py
launch environment, no credential     test_child_env.py (engine and doctor: test_engine.py,
                                      test_integration.py, test_local.py, test_doctor.py)
agent read context                    test_read_context.py (collection: test_engine.py,
                                      test_integration.py)
```

Add a new test next to the behaviour's existing file; create a new file only
for a new subsystem. Run `make test` (or `uv run pytest tests/<file>`) and
`make lint`; `make check` runs the same commands as CI (including the
lockfile check) on one local interpreter, and `make check-matrix` runs
`pytest` on every CI Python. `test_ci_workflow.py` keeps the Makefile and
`.github/workflows/ci.yml` matching in both directions. `make test-sandbox`
runs the real-backend sandbox modules on a host that must be able to
enforce. The autouse `sandbox_probe` fixture stands in for the outer
sandbox's capability probe everywhere else ("supported", no launch); a test
of the gate calls `sandbox_probe.unsupported(...)`.
