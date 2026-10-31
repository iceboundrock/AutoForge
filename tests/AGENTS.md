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
  use real local Python subprocesses only. One exception is an opt-in
  probe, skipped unless its variable names an installed runtime
  (`AUTOFORGE_PI_E2E_BIN`, `AUTOFORGE_CLAUDE_SDK_PROBE_CLI`,
  `AUTOFORGE_OPENCODE_SDK_PROBE_SERVER`). Such a probe uses a temporary home
  and synthetic credentials, and calls no model: the Claude probe answers
  from a loopback stand-in. The other is the live Pi smoke test
  (`tests/test_live_pi.py`, #135), whose one test (a REMOTE REVIEW, one
  model call) is the only one that makes a model call: marked `live`, deselected by default (`pyproject.toml` adds
  `-m 'not live'`), and skipped unless `AUTOFORGE_LIVE_PI=1`. It uses the
  credential the operator gave Pi (a DeepSeek API key, or the ChatGPT
  sign-in: `AUTOFORGE_LIVE_PI_AUTH` names the route), which it never
  reads, and writes nothing to GitHub. A new live test takes the marker
  and its own variable, and never runs in `make check` or CI.
- Never merge a real PR, push to a real remote, or touch a checkout outside
  `tmp_path`.
- Never weaken an assertion, a safety gate, or a verification step to make an
  implementation pass. A test that documents a deliberately unfixed edge case
  says so in its docstring, as `tests/test_replan.py` does.
- Never depend on running alone. `make test-parallel` runs the suite on
  pytest-xdist workers (#340): processes that share the host and its load,
  with every `tmp_path` under a `popen-gw<N>/` of its own. Each of these
  passed serially and failed there: a wall-clock budget for what is a
  cost (measure the process's CPU time, `time.process_time()`); a scan of
  `/proc` for a child that had not yet said it runs; a snapshot of a
  repository that `git commit`'s detached automatic maintenance could
  still write to (conftest's commits pass `NO_AUTO_MAINTENANCE`); an
  AF_UNIX path past `sun_path`'s 107 bytes (bind it with
  `conftest.bind_unix`).

## Fixtures and fakes (`tests/conftest.py`)

- `ScriptedProvider` (`src/autoforge/providers.py`) handlers stand in for
  every agent call and do what the real agent would (commit, push a fix,
  answer with a result).
- `engine`, `fake_github`, `tmp_state_dir`, `repo` and `make_engine`
  build a controller against those fakes; `block(...)`
  builds a well-formed `CONTROL_RESULT` block. A `make_engine` engine never
  sleeps: a read-back waiting for GitHub to move a PR head after the
  controller's push (#403) records each pause in `eng.sleeps`, and
  `FakeGitHub.pr_heads_lag` keeps the PR heads where they are. The
  controller posts the review comment (#162): a reviewer returns
  `review_result(...)` (the full REMOTE `REVIEW` payload, prose included)
  and posts nothing, and
  `controller_review_comment(eng, round)` returns the one comment carrying
  that round's marker. `review_comment_body(...)` builds a comment someone
  else posted: an earlier round's evidence for a test that starts past
  `REVIEW`, or an unjournaled comment that blocks the round. Reuse these
  rather than hand-rolling fakes.
- `REVIEW` and `FIX` fetch the bound HEAD (and `REVIEW` the merge base)
  before launching the agent. An engine without an origin records that
  fetch in the `offline_fetches` fixture and fetches nothing; with
  `origin=True` it fetches from the origin. Offline, a fetched head the
  store does not hold reads as a stand-in commit whose tree is
  `offline_tree(sha)`, so the REVIEW entry's `review_evidence` binding
  (#173) can name it; a test that runs `validation.commands` at REVIEW
  needs a real commit (`origin=True`).
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
  heads, PR heads and ancestry reads. Its own git (`isolated_git`) reads
  none of the operator's git configuration or `GIT_*` variables and seeds
  and publishes over no transport (#386 R1-F1), so no URL rewrite can send
  a test's push anywhere else. `github_origin` and the live smoke test's
  own writes use the same git (#386 R2-F1), so a `GIT_DIR` of the
  operator's shell cannot make them write to another checkout, and no hook
  of the operator's runs. `implement` is the implementation
  agent (one commit on the worktree's detached `HEAD`, reported by
  `analyze_payload`), and `scripted(...)` answers successive calls of one
  agent. An autouse fixture fails any test whose engine would reach the
  network git remote.
- `gh` prints every title and body through its JSON sanitizer (#327).
  `FakeGitHub.gh_sanitizes = True` makes the fake's `gh` reads do the same
  (`gh_sanitized`, pinned to go-gh's own vectors in `tests/test_github.py`)
  while `get_pr_exact`, `get_issue_exact` and `get_comment_exact` return
  what the fake holds; `exact_read_error` makes every exact read raise. A
  real `GitHubClient` takes its HTTPS transport as `https=`; the autouse
  `_no_network_exact_read` fixture fails a test whose client would reach
  `api.github.com`, so give it `https=...` or use the fake.
- `FakeGitHub` answers the native graph reads `UPDATE_EPIC` makes (#287):
  `get_issue_node`, `list_sub_issues` and `list_titled_sub_issues`, from
  `parents`, each issue's parent. An issue a test adds is a sub-issue of
  `EPIC` unless `add_issue(..., parent=...)` names another parent or
  `None`; the EPIC and an issue the controller creates have none.
  `graph_error` makes all three reads raise, `sub_issues_error` the two
  listings alone, and `hidden_sub_issues` counts sub-issues an issue's
  summary reports but its listing leaves out, as GitHub does for one the
  token may not read. A `null` selection is held to the listing, so a test
  that ends a run with `null` leaves no other open sub-issue of `EPIC`.
  The scheduler's tests serve the graph from their own fake `gh` instead
  (`tests/test_epic_graph.py`).
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
  interval, or close its stdout and run on (`close_stdout`); given `later`
  scripts, each launch after the first plays the next one
  (`launches(home)` counts them), and `api_failure` is a run claude 2.1.314
  ended on a model API error (#395); the real
  `ClaudeCodeProvider` launches it, so the stream path is tested end to end
  without a real agent. `fake_claude_bash_tool` there is a fake `claude`
  whose Bash tool runs one command the way 2.1.296 does: settings `env` by
  `--setting-sources`, its choice of shell, the shell snapshot of
  `~/.bashrc` or `~/.zshrc` (or a login shell without one),
  `CLAUDE_ENV_FILE` and `CLAUDE_CODE_SHELL_PREFIX`, with the real bash and
  zsh.
- `tests/scheduler_worker.py` is the worker process of the scheduler tests
  (#182): `python -P tests/scheduler_worker.py FIXTURES epic _worker ...`,
  the `worker_command` a test gives the scheduler, runs the production
  `run_worker` after doing in its own process what the autouse fixtures do
  in the test's, with a FakeGitHub seeded from `FIXTURES/<issue>.json` over
  the test's bare origin and a `ScriptedProvider` playing that fixture's
  steps (`implement`, `review`, `blocked`, `garbage`, `hold`; a step's
  `started` and `gate` files are the synchronization). In-process patches
  of a test never reach a worker: what a worker must see goes in its
  fixture or the configuration file. A fixture's `world` file keeps its
  FakeGitHub across worker processes (saved after every call, so a killed
  worker leaves what GitHub would hold), and its `gh_crash` kills the worker
  right after one GitHub call (#183).
- `tests/epic_cli.py` runs `autoforge.cli.main` in a process of its own for
  `tests/test_epic_recovery.py` (#183): the graph is the test's pickled
  `Graph`, the workers are `tests/scheduler_worker.py`, and each `crashes`
  entry SIGKILLs that process once, before or after a named call
  (`module:attribute`, or `spawn`), so a scheduler or `epic archive` dies at
  an exact ADR 0008 D14.5 window while its workers live on.
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
- `tests/operator_comment_fidelity.py` is no test: it is the tool a
  maintainer runs by hand, with their own `gh` login, to capture the
  byte-fidelity fixture of an operator comment (#243;
  `docs/development.md`). It renders payloads and reads a scratch issue,
  and never writes to GitHub. `tests/test_operator_comment_fidelity.py`
  replays the committed fixture (the maintainer's capture on scratch issue
  #264) offline and tests the tool against a fake runner; never write or
  edit the fixture by hand.
- `tests/parallel_bench.py` is no test either: the benchmark a maintainer
  runs by hand to time the suite serially and on pytest-xdist workers, and
  to reconcile every run, test by test, with the first serial one that
  passed (#340; `docs/development.md`, "The suite on several workers").
  `tests/test_parallel_bench.py` tests its parsing, reconciliation,
  statistics and resume on synthetic reports and stand-in runs, and runs a
  three-test suite serially and on two workers, and with worker options
  that override the configuration's.
- `tests/fast_targets.py` is no test either: the tool behind
  `make test-fast` (#342, ADR 0010), which selects the test modules a
  change's paths map to, for feedback while working. Its `COVERAGE` needs
  an entry for every file under `src/autoforge/` and every helper module
  here, so a new module or helper gets one: the test modules that check it
  beyond those importing it, which the tool adds itself, or `FULL` when
  almost every test runs through it. A test module whose tests are all
  `live` needs a `LIVE_ONLY` entry naming its offline tests, so a change
  to it alone still runs something. `tests/test_fast_targets.py` fails
  without either. A FAST run is never evidence and never a gate.
- `watched_fifo(path)` makes a FIFO and yields the list of times
  something opened it to read (a watchdog releases the reader, so a read
  that follows a planted link records itself instead of hanging the
  test); read it after the `with` block (#223).
- The state pins of LOCAL's removal (ADR 0009 D8, D12 S1b; #385) are
  written against the code before it. The `local_fields` fixture runs a
  state-file test twice: its callable gives the dict a test is about to
  write LOCAL's twelve fields (`LOCAL_STATE_FIELDS`) at their empty
  values, or none of them. Every refusal D8 keeps takes it.
  `state_tree(root)` snapshots a directory (each entry's kind, inode,
  modification time and bytes), so two equal snapshots mean nothing there
  was written, replaced or created and removed again.
  `unloadable_state(kind, saved)` makes a state file that loads into one of
  `UNLOADABLE_STATES`: an unknown mode, an unknown label, invalid JSON and,
  since LOCAL was removed (#387), a LOCAL state.
  They keep passing unchanged after the removal, with only the expected
  protocol label moved to 13 (#388).
- The shared coverage LOCAL's removal must not lose (ADR 0009 A.2.3, A.2.4,
  D12 S1a; #386) is REMOTE or mode-neutral tests. Each docstring carries
  `ADR 0009 A.2.3 <row> (#386)` (or A.2.4) and the node id of the LOCAL
  test it replaced. The LOCAL tests, their helpers and LOCAL's fixtures
  went with LOCAL's code (#387, #388).
- Recovery tests reload state from disk after each step; a crash-recovery
  test performs the external side effect first and only then interrupts,
  because that is the window the controller must survive.
- `os.fsync` waits for no disk outside the modules of
  `REAL_FSYNC_MODULES`, the tests of the durable writes themselves (#341):
  the autouse `_fsync_without_the_disk` puts `fsync_without_the_disk` in
  its place, which refuses what `fsync(2)` refuses (`tests/test_safefs.py`
  pins each answer to the real call's) and flushes nothing. No test can
  tell a flushed file from one that was not, a crash-recovery test
  included: what a killed process wrote is there either way, and only a
  power loss would differ. A test of what a write survives belongs in one
  of those modules; a test that patches `os.fsync` patches over the
  stand-in. Child processes (git, a scheduler worker) flush as always.
- A passed test's `tmp_path` is deleted when the test ends, and one that
  failed or errored in setup, call or teardown keeps it
  (`tmp_path_retention_policy = "failed"` in `pyproject.toml`, decided by
  `tests/tmp_path_retention.py` once the teardown report is made). That
  module also numbers each `tmp_path` without listing the basetemp, and
  never hands out a name twice in a session (#341). A directory a test
  needs outside its `tmp_path` (outside the repository `git_repo(tmp_path)`
  makes, for instance) comes from `tmp_path_factory.mktemp(...)`, whose
  names are never reused in a session either, never from `tmp_path.parent`:
  a sibling named after `tmp_path.name` outlives its test, since only
  `tmp_path` itself is removed.

## Where a behaviour is tested

```text
transitions, routing, loop bounds     test_transitions.py, test_routing.py, test_loop_guard.py
engine verification, gate, recovery   test_engine.py, test_engine_locking.py, test_integration.py
replan policy and transaction         test_replan.py
GitHub client, pre-merge evidence     test_github.py, test_premerge.py
byte-exact export and its proof       test_materialize.py (the merge gate's: test_premerge.py,
                                      test_engine.py)
effect operations, git transport      test_effects.py, test_git_transport.py
state, filesystem boundary            test_state.py, test_state_root.py, test_safefs.py
the controller's git reader           test_git_workspace.py (hardened reads)
the shared coverage of LOCAL's        test_git_workspace.py (A), test_state_fuzz.py (B),
removal (ADR 0009 S1a, #386)          test_state_root.py (C), test_findings_fence.py (D),
                                      test_doctor_sandbox.py, test_sandbox_routing.py,
                                      test_sandbox_policy.py (E, F), test_live_pi_support.py (G),
                                      test_validation_evidence.py, test_engine.py (H),
                                      test_runlog.py, test_engine.py (the recovery matrix's
                                      journal cases)
the state pins of LOCAL's removal     test_state.py, test_replan.py (the refusals D8 keeps, with and
(ADR 0009 S1b, #385)                  without LOCAL's fields), test_cli.py (a state that does not
                                      load: every command, run --force), test_scheduler_state.py,
                                      test_epic_commands.py (the scheduler's passes)
LOCAL's ways in, closed (ADR 0009     test_cli.py (autoforge local, --help, a local: key,
S2, #387)                             a LOCAL state under every label and in the lock's
                                      directory, no read of <git dir>/autoforge/state),
                                      test_config.py (the key),
                                      test_state.py (the loader),
                                      test_ownership.py, test_epic_commands.py (the scheduler,
                                      the epic commands' refusal of the key)
protocol 13: LOCAL's state fields     test_state.py (the drop step, one refusal per field, a
and evidence kinds removed (ADR 0009  protocol-13 or unlabelled file carrying one),
S3, #388)                             test_cli.py (status --json carries none of them),
                                      test_evidence.py, test_certificate.py,
                                      test_review_certificate.py (LOCAL kinds refused as
                                      unknown), test_local_removal.py (structural: no LOCAL
                                      topology, launch class, mode or deleted module),
                                      test_packaging.py (no local_*.md packaged)
scheduler namespaces (#178)           test_run_namespace.py (RunKey, layout, binding, lookup,
                                      listing, archive, engine binding); the standalone
                                      state-dir refusal: test_cli.py
lock domains (#180)                   test_lock_domains.py (L0 sharing and adoption, L2, L1,
                                      the order, the filesystem refusal, descriptor hygiene;
                                      real processes), test_shared_git.py (ADR 0008 D12.5,
                                      real git); ownership guards: test_ownership.py,
                                      test_cli.py, test_engine.py (next_issue_url)
scheduler claims, scheduler.json and  test_scheduler_state.py (strict files, the closing rule,
restart reconciliation (#181)         compare-and-write, classification, every crash window
                                      with real L0/L2 holders, finalization, archive);
                                      issue_settlement: test_transitions.py; the open-claim
                                      guard: test_ownership.py, test_cli.py
EPIC dependency graph (#179)          test_epic_graph.py (snapshots, refusals, cycles, verdicts,
                                      order, digests, the re-read, no write; a fake `gh` serving
                                      graphs to the real client, and tests/fixtures/github_graph);
                                      the typed reads: test_github.py; API URLs: test_validation.py
parallel EPIC scheduler and its       test_scheduler.py (bounded admission in ready order, slow,
workers (#182)                        BLOCKED, failing and READY_FOR_MERGE workers, drain, Ctrl-C,
                                      the grace, SIGHUP, exits with a live worker, the graph race,
                                      spawn failures, resume, a run lock a status probe held at
                                      startup or at a close_unready reconciled and retried at the
                                      next snapshot, once per issue, isolation, no write /
                                      effect / provider name; real worker subprocesses through
                                      tests/scheduler_worker.py), test_worker.py (in process:
                                      startup refusals, the step boundary, signals, the recorded
                                      configuration, provider and #158 gate parity),
                                      test_executor_supervised.py, test_scheduler_stop.py; the
                                      engine's hooks: test_engine_locking.py; scheduler keys:
                                      test_config.py
`epic` commands and end-to-end        test_epic_commands.py (run, resume, status categories and
recovery (#183)                       JSON keys, stop, archive, dry run, --force, exit codes, GitHub
                                      changes between ticks, standalone guards, no merge;
                                      the scheduler in process), test_epic_recovery.py (every
                                      D14.5 crash window by SIGKILL of a real scheduler, worker or
                                      archive; tests/epic_cli.py); doctor --epic: test_doctor.py
REMOTE run's recorded configuration   test_config_fingerprint.py (CLI: test_cli.py; load and
                                      protocol: test_state.py)
sandbox pin and config placement      test_sandbox_pin.py (strength, the record, the engine's
(#280)                                refusal, opt-in log, strengthen and upgrade; CLI:
                                      test_cli.py; load and protocol: test_state.py);
                                      the placement rule (the providers' writable
                                      directories included): test_sandbox_policy.py, doctor:
                                      test_doctor.py, worker: test_worker.py, real backend:
                                      test_sandbox_routing.py; the `..` spellings CLI and
                                      doctor share: conftest.dotdot_config_paths
candidate validation evidence         test_evidence.py (pure), test_validation_evidence.py
                                      (engine; load and protocol: test_state.py)
candidate validation gates (#173)     test_validation_gates.py (producers, REVIEW entry,
                                      override, provider parity; REPLAN rejection:
                                      test_replan.py)
review certificates (#174)            test_certificate.py (pure: issuance, applicability,
                                      wording), test_review_certificate.py (engine: the
                                      READY_FOR_MERGE write, the #162 journal,
                                      drift, unblock, status, provider parity); hand-built
                                      clean reviews hold one through conftest.certify_review
merge gate certificate check (#175)   test_merge_certificate.py (each requirement, set and
                                      policy drift, the invalidation matrix, MERGE crash
                                      windows, legacy states, the resume banner, provider
                                      parity); the existing merge gate: test_engine.py
merge.delete_branch, gh's working     test_merge_branch.py (the remote-only deletion, each
directory (#283)                      refusal, no deletion before a verified merge, read-back,
                                      the crash windows, no git in the checkout through MERGE
                                      with a `gh` that runs git: conftest.local_git_gh); every
                                      `gh` in an empty directory, the scoped ref read and
                                      DELETE: test_github.py; the INITIALIZING origin read:
                                      test_engine.py
CONTROL_RESULT, prompts, providers    test_result_parser.py, test_prompts.py, test_providers.py
Pi RPC reducer and adapter (fake pi)  test_pi_rpc.py
live Pi smoke test (#135)             test_live_pi.py (opt-in: -m live, AUTOFORGE_LIVE_PI=1; a
                                      REMOTE flow, its checks in tests/live_pi.py),
                                      test_live_pi_support.py (offline: deselection, the
                                      credential scan, the checks, the flow rehearsed on the
                                      fake pi)
Claude stream-json reducer            test_claude_stream.py (adapter on the fake: test_providers.py)
SDK policy (#149): options, tools,    test_sdk_policy.py (opt-in probes of an installed Claude
roots, credentials, environment,      Code CLI or OpenCode runtime against sentinel
interaction data, the adapters        configuration, each with a control run)
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
                                      test_sandbox_gate.py, test_doctor_sandbox.py,
                                      test_sandbox_host.py (the host measurement)
config, CLI, doctor, CI drift guards  test_config.py, test_cli.py, test_doctor.py, test_ci_workflow.py, test_gitignore.py
fast feedback targets and what keeps  test_fast_targets.py (the mapping, the selection, never a
them from evidence (#342, ADR 0010)   gate, the default suite's options); test_evidence.py,
                                      test_certificate.py, test_validation_gates.py (a narrower
                                      run or an agent's claim is no FULL evidence)
tmp_path retention (#396)             test_tmp_path_retention.py (a synthetic suite in a pytest
                                      process of its own, wired as conftest.py wires this one)
packaging, built artifacts (#256)     test_packaging.py (metadata, license, version, declared package
                                      data, resource reads, classifiers; the artifact checks on
                                      synthetic files, the REMOTE dry-run check, the release
                                      tag check, the digests handed on and the hashed build
                                      constraints), tests/dist_check.py (no test: what
                                      `make dist-check`, CI's `dist` job and the release build
                                      run); the PyPI workflow (#295): test_publish_workflow.py
launch environment, no credential     test_child_env.py (engine and doctor: test_engine.py,
                                      test_integration.py, test_validation_evidence.py,
                                      test_doctor.py)
agent read context                    test_read_context.py (collection: test_engine.py,
                                      test_integration.py)
```

Add a new test next to the behaviour's existing file; create a new file only
for a new subsystem. Run `make test` (or `uv run pytest tests/<file>`) and
`make lint`; `make test-fast` runs the modules your changed paths map to,
for feedback while working, and replaces neither. `make check` runs the
same commands as CI (including the lockfile check) on one local
interpreter, and `make check-matrix` runs `pytest` on every CI Python. `test_ci_workflow.py` keeps the Makefile and
`.github/workflows/ci.yml` matching in both directions, and pins the
`sandbox` job to `make test-sandbox-measured` and the `dist` job to
`make dist-check` on every CI Python. `make test-sandbox` runs the
real-backend sandbox modules on a host that must be able to enforce;
`make test-sandbox-measured`, which CI runs, first measures the host
(`tests/sandbox_host.py`) and requires them exactly where it can. The autouse `sandbox_probe` fixture stands in for the outer
sandbox's capability probe everywhere else ("supported", no launch); a test
of the gate calls `sandbox_probe.unsupported(...)`.
