# ADR 0008. Parallel EPIC scheduler: authority, run identity, durable claims and lock decomposition

- **Status:** accepted for implementation. This is a design record; nothing
  in it is implemented. It is implemented by #178 (run identities and
  namespaces), #179 (dependency snapshots and the ready set), #180 (lock
  domains), #181 (scheduler claims and restart reconciliation), #182 (the
  bounded worker supervisor) and #183 (the `epic` commands and the
  end-to-end recovery tests). The number is assigned when the record
  merges: if another ADR merges first, this record takes the next free
  number, and its index entries move with it.
- **Note:** [ADR 0009](0009-removing-local-mode-strict-refusal-protocol-and-sequencing.md) removed LOCAL mode (#388). D17.5 (LOCAL
  excluded from scheduler runs by L0) and the LOCAL mentions in §1.1, §2.5,
  §2.12, §3 and §4 no longer apply; ADR 0009 D4's rule for a standalone
  state that does not load keeps D17.5's effect.
- **Decides:** #177, for EPIC #176 (Wave 1).
- **Amends:** no ADR. It generalizes #16's invariant (at most one controller
  acts on a repository at a time) without removing it (D10, D11), and
  `docs/agent-guides/state-and-recovery.md` points here for the
  generalization.
- **Amended by #243:** D9.4 (operator actions, ADR 0004 K10, at worker
  entry and in `epic status`).
- **Where:** placement is decided per decision and collected in §5.
  Existing code this record builds on: `src/autoforge/locking.py`,
  `engine.py`, `state.py`, `transitions.py`, `cli.py`, `executor.py`,
  `git_transport.py`, `agent_worktree.py`, `premerge.py`, `materialize.py`,
  `github.py`, `effects.py`, `replan_txn.py`, `config.py`,
  `config_fingerprint.py`, `safefs.py`, `runlog.py`, and `claims.py` (a
  name collision only, D7.4).
- **Related:** ADR 0002 (nothing an agent starts outlives its invocation),
  ADR 0004 (authority boundary and typed effects), ADR 0005 (outer sandbox),
  ADR 0006 (validation evidence and review certificates), ADR 0007 (#126's
  durable pause, which D15 reads once it lands); #16 (one controller per
  repository); #20 / #43 (complete listings); #23 (protocol numbering);
  #158 (the guarantees this wave keeps); #165 (no agent credential); #211
  (acceptance actions); #219 (recorded run configuration).

## 1. Context

### 1.1 What the code does today

#177 was planned against `main` `2fff4b9`. Every reference below is re-read
on `main` `77caf43`, after #158 closed with #175 (PR #239). §5.1 lists the
facts of #177's body that changed in between.

**One lock serializes the whole repository.**

- `ControllerLock` (`locking.py` :132) takes `flock(LOCK_EX | LOCK_NB)` on a
  descriptor of the git common dir itself (`acquire` :266–299), opened
  `O_RDONLY | O_DIRECTORY | O_CLOEXEC`. `<common dir>/autoforge/controller.lock`
  is only a PID record: it is opened `O_NOFOLLOW | O_CLOEXEC` relative to a
  validated `autoforge` directory descriptor, must have exactly one link,
  and is never unlinked.
- `release()` (:301–311) calls `flock(LOCK_UN)` and then `close`. No other
  process shares the descriptor today, so the unlock is harmless. On a
  descriptor shared with a child it would release the lock for every holder
  at once.
- `ControllerEngine.locked()` (`engine.py` :3781) holds it for a whole
  command. It is not reentrant, and contention raises `LockError` (exit 2).
  `run` (`cli.py` :587), `local init` (:623), `local run` (:663), `step`
  (:696), `resume` (:723) and `unblock` (:767) take it; `status`, `doctor`
  and dry runs take none.
- This is #16's invariant: at most one controller acts on a repository at a
  time.

**The process model.**

- ADR 0002: at most one contained invocation per controller process
  (`executor.py` `_CONTAINMENT_LOCK` :917, refused at :1025). While it
  runs, the controller is a child subreaper.
- Every launch starts in a session of its own (`start_new_session=True`,
  :1281, :1310) with `Popen`'s default `close_fds=True`. The only
  `pass_fds` anywhere is the sandbox backend's own two descriptors, its
  options and its status stream (`sandbox_bwrap.py` :439).
- `src/` installs no signal handler. SIGINT becomes `KeyboardInterrupt`,
  and the executor tears the agent's group down (SIGTERM,
  `_KILL_GRACE_SECONDS` = 5 s, SIGKILL). SIGTERM or SIGHUP kills the
  controller at once, which can orphan the agent. A second Ctrl-C during
  teardown escapes before SIGKILL.
- The controller's own `gh` and `git` processes go through the same
  executor and start in their own session, but only agent and validation
  launches request containment (`contain_orphans`). A `gh` or `git` call in
  flight when its controller is SIGKILLed can complete afterwards.

**State and worktrees.**

- REMOTE state is `<state_dir>/state.json`, with run logs in
  `<state_dir>/logs/<run-id>/` and read contexts in
  `<state_dir>/read-context/<run-id>/` (`state.py` `StatePaths` :1584).
  `state_dir` defaults to `.autoforge` relative to the working directory
  (`cli.py` `--state-dir` :100, `config.state_dir`). LOCAL's is
  `<git dir>/autoforge/state`.
- The state schema is `AutoForgeState` (`state.py` :390) at
  `__protocol_version__` "11": #160 took 7, #211 8, #219 9, #172 10 and
  #243 11, and #23 reserves 6 for #126. On 2026-10-23 #23 released that
  reservation: label 6 stays skipped for good
  ([state-and-recovery.md](../agent-guides/state-and-recovery.md),
  "Numbering a new protocol").
- Worktrees are one per issue (`_agent_worktree_name`, `engine.py` :1771):
  `<common dir>/autoforge/worktrees/<n>`, or `<execution.worktree_dir>/<n>`.
  Between issues, `UPDATE_EPIC` uses `epic-<n>`. The worktree root is part
  of a run's recorded configuration (#219).
- `_ensure_agent_worktree` (:1853) checks the path (no link at it or above
  it; when it exists, registered and bound to its admin dir, #223), then
  runs `git worktree add --detach <path> HEAD` (:1959). Today the
  controller lock serializes the gap between the check and the create; only
  actors outside AutoForge can race it.

**Sequential EPIC execution.**

- After `MERGE`, `UPDATE_EPIC` takes the agent's `next_issue_url`
  (`_apply_update_epic` :13864) and verifies it with
  `_verify_issue_selectable` (:5801): a GitHub issue URL of the same
  repository, not the EPIC, not the issue just finished, existing and OPEN.
  It reads no sub-issue, dependency or ownership fact.
- The state is then reused through `reset_for_new_issue` (`state.py`
  :1226), which keeps `run_id` and `step_count`. Selection is bounded by
  `MAX_NEXT_ISSUE_SELECTIONS` (`engine.py` :612).
- `github.py` reads no sub-issue and no dependency.

**The merge gate.**

- `merge_gate_open` (`engine.py` :7453) requires `safety.allow_merge` and
  `--allow-merge`.
- With the gate closed, `run()` (:3829) stops at `STOP_PHASES` =
  `TERMINAL_PHASES` ∪ {`READY_FOR_MERGE`} (`transitions.py` :101, :107;
  `engine.py` :3855), so the pre-merge verification, its fetch and `MERGE`
  never run.
- With the gate open, `MERGE` also requires an applicable review
  certificate (ADR 0006 D13, #175).

**Controller writes to the shared common dir.** There are five, not the two
#177's body lists:

1. the worktree check-then-create above, which writes the admin dir
   `<common>/worktrees/<id>/` and the worktree itself;
2. the controller's fetch, `GitTransport.fetch` (`git_transport.py` :526).
   It writes objects into `<common>/objects` through a private `GIT_DIR`,
   with no ref, no `FETCH_HEAD`, no auto-gc and no commit-graph. It is
   called before the `ANALYZE_EXECUTE` launch (`engine.py` :6089: the
   verified default-branch head and the branch head, #161), the `REVIEW`
   launch (:9224), the `FIX` launch (:9428) and the `REPLAN_REEXECUTE`
   launch (:10052); by the pre-merge verification (`premerge.py` :152); and
   by `read_commit` for a commit the store lacks (`git_transport.py` :673);
3. the strict import of an issue's private objects (`agent_worktree.py`
   `import_private_objects` :486), run before every REMOTE launch and after
   every launch that exited cleanly, in every `sandbox.mode`, whenever the
   private directory holds objects (`engine.py` `_import_before_launch`
   :2021, `_import_after_launch` :2032). Its
   `index-pack --stdin --fix-thin --strict` writes one pack into
   `<common>/objects/pack`;
4. the outer sandbox's per-issue directories
   `<common>/autoforge/sandbox/<key>/{objects,overlay-work,cache}`, created
   through `SafeRoot` (`_sandbox_dirs` :2333);
5. the lock's own `autoforge` directory and PID record, under the lock.

The controller's push (K1, `GitTransport.push`, `git_transport.py` :751)
runs in a private `GIT_DIR` that has no remote-tracking ref: it reads the
shared store and writes nothing to it. Every other controller git call
reads: `rev-parse`, the no-follow registry and candidate reads (#223),
`cat-file`, and the byte-exact exports (`materialize.py`).

**Agents no longer push or fetch.** Every REMOTE GitHub write, the push
included, is the controller's, performed under the repository lock: a
controller effect K1–K9 (`effects.py` :335, ADR 0004), or one of the writes
ADR 0004 D5.4 leaves to their own mechanisms (the merge and disabling
auto-merge, the replan close, its receipt and its compensating reopen, and
the roadmap splice). Agents hold no credential (#165) and
commit on a detached `HEAD`. Under `sandbox.mode: required`, and under
REMOTE `preferred` (which is `required`), the common dir is read-only to
the agent, which writes into its private object directory (ADR 0005 D6.1).
Under `disabled` an agent still writes the shared store directly.

**Parent-EPIC writes have no concurrency control.** The roadmap splice
(`github.py` `edit_issue_body` :1325, called from `engine.py` :14184) has
no compare-and-swap, and the progress comment (K8) and the merge counters
belong to one state. Two writers would lose updates. All of them happen in
`UPDATE_EPIC` or `MERGE`.

**Per-issue authority includes evidence.** Validation records, the review
evidence binding and the `review_certificate` live in each issue's state
(ADR 0006). Validation exports are `tempfile.mkdtemp` directories in the
system temporary directory, with the prefix `autoforge-validate-`
(`engine.py` :565) or `autoforge-premerge-` (`premerge.py` :62), removed
after use. They read the shared store and write nothing to it.

**Two refusals at the entry of an existing run.**

- #219's recorded configuration makes `step`, `resume` and `unblock` refuse
  a configuration difference (`ConfigurationDriftError`, exit 2, nothing
  written) unless `--accept-config-change` is given (`cli.py`
  `_check_configuration`, `engine.py` `check_run_configuration` :1594).
- #211's acceptance action parks a run in `BLOCKED` until
  `unblock --approve-action` or `--decline-action`.

Recovery hints repeat `--config` and `--state-dir` as typed (`engine.py`
`resume_command` :11313, `cli_command` :11327, #189).

### 1.2 Verified GitHub facts

#177's planning made these read-only calls against this repository:

- `GET /repos/{o}/{r}/issues/{n}/sub_issues` lists direct children as full
  issue objects. Each carries its own `sub_issues_summary`, so a nested
  parent is visible without another call.
- `GET /repos/{o}/{r}/issues/{n}/dependencies/blocked_by` lists blockers as
  full issue objects, with `state`, `state_reason` and `repository_url`.
- Every issue object carries `parent_issue_url`, and
  `GET /repos/{o}/{r}/issues/{n}/parent` returns the parent.
- Every issue object carries `sub_issues_summary.total` and
  `issue_dependencies_summary` (`blocked_by`, `total_blocked_by`,
  `blocking`, `total_blocking`).
- The repository has a numeric `id` (`GET /repos/{o}/{r}`).

This record did not re-measure them. #179 pins every field it depends on
against a recorded fixture before it relies on it.

### 1.3 Upstream decisions this record builds on

- **ADR 0002.** One contained invocation per process (§4b); an invocation
  is complete only when its group is empty; an unsandboxed agent outlives a
  controller killed with SIGKILL, a sandboxed one does not (§4e). A worker is a controller, not
  an agent, and runs its own contained invocations exactly as today.
- **ADR 0004.** The closed effect-kind set (D5.1, amended by #211); a body
  append's residual, a human edit inside the read-to-write window (K3,
  K6); the entry observation (D4.4); effect records live in the issue's own
  state; "an outbox table, queue or worker" rejected for a single process
  under a local lock (§2.4).
- **ADR 0005.** `<common>/autoforge` is masked in every enforced launch,
  with only the issue's own worktree and sandbox directory carved out
  (D6.1, §2.5's table); the sandbox directories are keyed by the worktree's name (D8.4);
  the strict import (D6.5).
- **ADR 0006.** A certificate means a clean, validated review of one exact
  revision, never a merge permission (D1.2, D12, D13); `READY_FOR_MERGE` is not a
  merge.
- **ADR 0007.** #126's durable pause and its `interaction_policy`; under
  `prompt`, a controller whose stdin is not a terminal degrades the wait to
  a pause, where the provider's gate and #126 allow it, or to a failure
  (D7.4).

### 1.4 Terms

- **Coordination root.** The git common dir of the checkout, the key the
  repository lock already uses.
- **Scheduler.** The foreground process of `epic run`, `epic resume` or
  `epic archive`. It admits issues and supervises workers; it runs no
  phase.
- **Worker.** One OS process running one `ControllerEngine` on one issue,
  launched by a scheduler.
- **Namespace.** `<common>/autoforge/scheduler/issues/<n>/`: one issue's
  run lock, binding, claim, state directory and worker logs.
- **RunKey.** `(repository_id, issue_number)`, the identity of a
  namespace.
- **Scheduler claim.** The record in a namespace's `claim.json` that one
  generation of one invocation owns the issue. Always local; never a GitHub
  marker.
- **Invocation.** One scheduler process, named by its `invocation_id`.
- **Generation.** A claim's ordinal within its namespace, monotonic.
- **L0, L1, L2.** The orchestration lock, the shared-Git lock and the
  per-issue run lock (D11).
- **Settlement.** The scheduler's only view of a per-issue state (D15).
- **Observed live.** An issue whose L2 another process holds (D8).

## 2. Decisions

Decisions keep #177's numbering (1–18). A sub-decision **D<n>.<m>** names
the child that implements it, or says it is documentation only; §5 collects
them.

### 2.1 Authority: scheduler versus per-issue engine (decision 1)

**D1.1 (documentation only; #181 and #182 keep it).** The split:

| The scheduler decides | The per-issue engine (one per worker) decides |
|---|---|
| which children of the selected EPIC are eligible, from a GitHub-native snapshot | the workflow phase and every transition |
| how many issues run at once | provider and profile selection, every invocation |
| whether an issue already has a live owner | `CONTROL_RESULT` validation and GitHub read-back |
| which worker owns an issue (scheduler claim plus lock handoff) | candidate verification, validation records, review certificates |
| when another worker may launch | `FIX`, replan, `READY_FOR_MERGE`, `BLOCKED` |
| stop, drain and restart classification | every controller effect (K1–K7, K9), the replan close and reopen, PR adoption and recovery |

**D1.2 (documentation only; #181 and #182 keep it).** Rules:

- **No GitHub writes or git writes by the scheduler.** It writes only its
  own files under the scheduler root (D5, D6, D7). The worker's engine
  creates worktrees, as today, under L1 (D12).
- **The scheduler never writes per-issue state.** It reads it read-only
  through #178's strict lookup. The single exception is the explicit
  operator command `epic archive` (D17), which moves a state aside with the
  existing link-then-unlink primitive. Nothing does it automatically.
- **The scheduler sees the engine only through one pure helper.**
  `issue_settlement(state) -> Settlement` is a closed enum added next to
  the phase sets in `transitions.py` (D15). The scheduler never branches on
  individual phases. It never reconstructs effect records, validation
  records or certificates, and it never parses `CONTROL_RESULT`.
  - Wrong: "the state is in `REVIEW`, so the scheduler decides whether
    `FIX` runs".
  - Right: "the state is valid and in progress, so the scheduler launches
    an ordinary worker resume, and the engine decides the next phase".
- **Exit codes, provider completion, PR existence, a review certificate
  and scheduler claims are never `READY_FOR_MERGE`,** and none of them
  satisfies a dependency (D15.2). A worker's exit status is diagnostic. The
  verdict is the strictly loaded state.
- **The scheduler contains no provider name and no provider branch.**
  Providers are chosen inside the worker's engine by profiles, as today.

*Rejected.*

- **One process with several engines, on threads or asyncio.** It breaks
  ADR 0002's one contained invocation per process, it shares mutable state,
  and a crash would not release per-issue locks.
- **A scheduler that interprets phases.** That duplicates recovery
  authority.
- **Scheduler-owned GitHub writes,** such as labels, comments or EPIC
  edits: a second effect path beside ADR 0004's.

### 2.2 Single-host consistency model and the admission transaction (decision 2)

**D2.1 (documentation only).** **The coordination root** is the git common
dir. **The guarantee:** two scheduler or controller processes on one host
that share a coordination root cannot own one issue at the same time, and
cannot bypass scheduler admission.

- Separate clones are separate coordination roots, exactly as with #16
  today. Their residual is unchanged and is covered by GitHub-side
  identities: implementation markers and the effect identities of ADR 0004.
- Live ownership rests only on kernel `flock` locks, which die with their
  process. There are no leases, TTLs, heartbeats or PID checks.

**D2.2 (#182 orchestrates; each step names its owner).** **Admission is an
observed decision.** Holding L0 (D10), the scheduler:

1. Takes a snapshot (#179, D3), evaluates the ready set (D4) and picks the
   first ready issue.
2. Tries the issue's L2 non-blocking (#180). If that fails, the issue is
   live (D8) and is skipped.
3. Holding L2, strictly loads the namespace's binding, claim and state, and
   classifies them (#181, D14). Only "no namespace", `fresh` and
   `resumable` continue.
4. Re-reads the eligibility-critical facts from GitHub (#179):
   - the issue: open, the same `id`, `parent_issue_url` still the EPIC,
     still a leaf;
   - its complete `blocked_by` list.

   It compares the re-read with the snapshot's node digest for the issue
   (D4.4). Any difference is `STALE`: no claim, and a new snapshot on the
   next tick.
5. On first admission, writes `binding.json` (#178). Then it writes
   `claim.json` atomically (#181), which is the linearization point. If an
   earlier claim is still open (`resumable`), the same atomic write closes
   it as `superseded` and installs the new one (D7.2).
6. Spawns the worker, passing L0 and L2 by inheritance (#182). It records
   the worker diagnostics in the claim and closes its own L2 descriptor, by
   `close` only (D10.3).

L2 is held without a gap from step 2 until the worker exits.

**D2.3 (#182).** **Spawn failure** means that `Popen` raised. CPython reaps
a child whose exec failed before raising, so normally no child exists.
Defensively, the scheduler closes its own L2 copy and tries L2 again,
non-blocking, on a fresh descriptor:

- L2 taken: no child exists. It closes the claim as `abandoned` while
  holding L2, releases it by `close`, and the issue is held
  `held:spawn-failed` for the rest of the invocation;
- L2 still held: a child exists. The claim stays open and the issue is
  `observed_live`.

Any failure after `Popen` returns, including a failure to record the
diagnostics, means a live worker: the claim stays open and is classified
after the worker is reaped.

**D2.4 (documentation only).** **Facts that change after the claim** are
seen by the next pass, and they do not rewrite history.

- A blocker added or reopened after admission does not stop a running
  worker. Status reports it.
- A resume is a new admission, evaluated against the graph as it is then.
- This is safe in Wave 1 because workers never merge (D9.3). An issue whose
  dependency regressed cannot land out of order through AutoForge; the worst
  case is wasted work.
- **Wave 2 requirement:** a scheduler-mode merge must re-check the issue's
  dependencies at merge time.

**D2.5 (#179).** The same snapshot, local facts and configuration
always give the same decision.

*Rejected.*

- **Distributed leases, a database or a queue.** Not needed on one host
  (`AGENTS.md`, "Runtime model").
- **GitHub labels or assignees as locks.** They are remote, racy and
  human-editable.
- **Interrupting running workers when the graph changes.** That is a phase
  decision the scheduler does not own.

### 2.3 GitHub graph source (decision 3)

**D3.1 (#179).** **Children** are the selected EPIC's **direct** native
sub-issues (`/sub_issues`, `per_page=100`, paginated to the end). The
listing is not recursive.

**D3.2 (#179).** **Blockers** come from native `blocked_by`
(`/dependencies/blocked_by`, paginated). `blocking` is not read. Prose such
as `Depends on #<n>`, checklists and the EPIC roadmap are never authority.

**D3.3 (#179).** **Completeness.**

- The child listing must reach its end, and its length must equal the
  EPIC's `sub_issues_summary.total`.
- Each child's blocker listing must equal its `issue_dependencies_summary`
  total. #179 pins the exact field against a fixture. A child whose summary
  reports no blocker has, by this rule, an empty listing, and is not
  queried; nor is a closed child, whose blockers decide nothing.
- A mismatch or a truncated listing refuses the snapshot (the #20 / #43
  rule).

**D3.4 (#179).** **Bounds.** #179 bounds the children, and the blockers per
child, at GitHub's documented limits. Exceeding a bound refuses the
snapshot.

**D3.5 (#179).** **Only these are eligibility facts:** ids, numbers,
state, `state_reason`, repository, parent and sub-issue count. Titles,
bodies, labels, assignees and milestones are not.

**D3.6 (#179).** Each shape has one rule:

| Shape | Rule |
|---|---|
| Nested EPIC (the child has sub-issues of its own) | `unsupported:nested-parent`. Never admitted, and its own children are not scheduled. Run the nested EPIC separately. |
| Child in another repository, or transferred out | `unsupported:foreign-repository` |
| Cross-EPIC or cross-repository blocker | Counts by its live state, wherever it lives. A blocker the token cannot read makes the child `blocked:unreadable-blocker` (fail closed). |
| Closed blocker | Satisfied only when `state_reason` is `completed`. Closed as `not_planned`, as a duplicate or with an unknown reason, it gives `blocked:closed-unsatisfied`, with the reason shown. The operator removes the native edge if that is intended. This is stricter than GitHub's display, on purpose. |
| Reopened blocker | Blocks again. |
| Closed child | Never admitted (`closed`). As a blocker of a sibling, it follows the closed-blocker rule. |
| Work completed or merged by a human | Completion is the child's closed state. An open child is admitted, and its worker's engine recovers an existing PR by marker (#158's reconciliation). The operator closes issues whose work is done. |
| Deleted child | Absent from the listing. A namespace for it is reported as orphaned: never launched, and its state, worktree and logs are never modified or deleted. An open claim on it is closed (D14.2), so that it cannot keep `epic run` refused. |
| Reopened child | Open again, and subject to the namespace rule (D5.5). |
| Duplicate entries or edges | De-duplicated by issue `id` before evaluation. |
| Cycle among open children, including a self-edge | The snapshot is refused. The diagnostic names one cycle in canonical order, starting at its lowest issue number. Running workers continue; admission stays closed until a later snapshot is acyclic. No agent is ever asked to resolve a cycle. |
| EPIC closed, or in another repository than the verified one | No admission. |

*Rejected.*

- **Recursive traversal.** It gives nested EPICs ambiguous ownership.
- **Trusting `blocked_by` counts without the listing.** A count is not
  identities.
- **Treating any closed blocker as satisfied.** It silently unblocks work
  after `not_planned`.

### 2.4 Ready-set semantics (decision 4)

**D4.1 (#179).** `evaluate(snapshot, local_facts) -> tuple[ChildVerdict, ...]`
is a pure function. `local_facts` is #181's per-issue classification
(D14). Each verdict carries one reason from a closed vocabulary:

- `ready`;
- `closed` (with `state_reason`);
- `blocked` (the open or unsatisfied blockers, sorted);
- `unsupported:<shape>`;
- `owned` (a live owner: this scheduler's unreaped worker, or
  `observed_live`);
- `held:<reason>`, with the reasons `settled-namespace`,
  `standalone-state`, `other-epic`, `uncertain`, `resumable` (its worker
  exited during this invocation), `spawn-failed` and `orphaned`.
  `orphaned` is reported for a namespace bound to this EPIC whose issue the
  snapshot no longer lists (deleted, transferred or re-parented); the
  namespace is never launched or modified beyond D14.2's claim closure.

A namespace classified `fresh` or `resumable` at startup (D14) contributes
no local fact, so it is evaluated like an issue without a namespace.

**D4.2 (#179).** **Order.**

1. Descending number of open EPIC children that transitively depend on the
   issue, so work that unblocks the most other work goes first.
2. Ascending issue number.

Both keys come from the snapshot alone. API response order, dictionary or
set iteration, completion timing and provider speed never influence the
order.

**D4.3 (#182).** **Capacity is not a verdict.** Admission takes ready
issues in order up to the free slots (D18.3). `max_parallel` limits claims,
never readiness.

**D4.4 (#179).** **Digests.**

- The snapshot digest is SHA-256 over canonical JSON (sorted keys, sorted
  lists) of the eligibility facts: the repository `id`; the EPIC `id`,
  number and state; every child's `id`, number, state, `state_reason`,
  repository, parent and sub-issue count; every edge as (child `id`, blocker `id`,
  blocker state, `state_reason`, repository).
- A node digest is the same computation restricted to one child and its
  blockers.
- Titles and timestamps are excluded.

### 2.5 Per-issue RunKey and state namespace (decision 5)

**D5.1 (#178).** **RunKey** = `(repository_id, issue_number)`, where
`repository_id` is GitHub's numeric repository id.

- The binding also records the issue's numeric `id`, which every admission
  compares. A mismatch is `uncertain`.
- The owner/name pair is for display only; a repository rename is tolerated
  through `repository_id`.
- A different repository behind the same common dir is global uncertainty
  (D14.3).
- Titles are never part of identity.

**D5.2 (#178).** **`binding.json`**, `autoforge.run-binding/1`, strict and
bounded, written once at first admission and never rewritten:

```text
schema          "autoforge.run-binding/1"
run_key         {repository_id, issue_number}
issue_id        GitHub numeric issue id
repository      {owner, name}                 display only
epic            {number, issue_id}            the EPIC of the first admission
worktree_path   resolved path                 diagnostic only
created_at
```

**D5.3 (#178).** **Layout.** The namespace sits at a fixed root under the
coordination root, independent of `--state-dir` and `config.state_dir`:

```text
<git common dir>/autoforge/
  controller.lock              existing PID record; L0 is flock on the common dir fd (unchanged)
  git.lock                     L1 shared-Git lock (new, #180)
  worktrees/<n>/               existing per-issue worktree (derivation unchanged)
  sandbox/<n>/                 existing per-issue sandbox directories (unchanged, ADR 0005 D8.4)
  scheduler/
    issues/<n>/
      run.lock                 L2 per-issue run lock (created once, never unlinked)
      binding.json             autoforge.run-binding/1
      claim.json               autoforge.scheduler-claim/1
      state/                   an ordinary state_dir
        state.json             AutoForgeState at the current protocol (schema unchanged)
        logs/<run-id>/         existing run-log layout
        read-context/<run-id>/ existing read-context layout
      workers/<generation>.log worker console output (bounded, redacted; diagnostic only)
    epics/<n>/
      scheduler.json           autoforge.scheduler/1
      stop-request             autoforge.scheduler-stop/1 (transient)
```

Every file is written through `safefs`, anchored at the coordination root
with each component below it opened `O_NOFOLLOW` (D18.2). A worker's
`StatePaths` for `state/` uses that anchor too, never a path resolved from
the working directory.

**Why a fixed root.**

- Recovery must find every namespace from the repository alone; a
  per-invocation `--state-dir` is unknowable later.
- Inside the git dir, the namespaces are outside every working tree and
  untracked by construction, like worktrees and LOCAL state.
- Under an enforced sandbox `<common>/autoforge` is masked except for the
  issue's own worktree and sandbox directory (ADR 0005 D6.1), so no agent
  sees a namespace. The scheduler root is controller-private
  and is never granted to an agent.
- `epic` commands reject `--state-dir`.

**D5.4 (#178).** **Worktrees and sandbox directories.**

- The worktree path is derived exactly as today, so a worker reuses the
  worktree an earlier standalone run created for the same issue, and the
  sandbox directories keyed by it (ADR 0005 D8.4).
- The binding records the resolved path for diagnostics only.
- A worker never uses the `epic-<n>` worktree or its sandbox key, because
  it never runs `UPDATE_EPIC`.
- **Validation exports need no namespace.** Each is a fresh
  `tempfile.mkdtemp` directory (mode 0700, a unique name) that one
  definition runs in and that is removed afterwards. It reads the shared
  store and writes nothing to it, holds no identity and is never read for
  recovery, so two workers' exports never share a path and need no
  coordination. What they share is CPU and disk, which `max_parallel`
  bounds (D18.3).

**D5.5 (#178; the ownership predicate is #181's, D17.2).** Namespace edge
cases:

| Case | Rule |
|---|---|
| Two issues | Two directories, no shared file. No field of one state can appear in another. |
| Multiple EPICs referring to one issue | A GitHub issue has at most one native parent, so this needs re-parenting. There is one namespace per issue. The binding records the EPIC it was first admitted under, and a scheduler for any other EPIC holds it as `held:other-epic`. |
| Settled namespace while the issue is open | Covers `READY_FOR_MERGE` awaiting a human merge, `BLOCKED` awaiting `unblock` (an acceptance action included), and a rerun after reopen. Held as `held:settled-namespace`; status shows the specific next action. A rerun needs `epic archive` (D17.4). |
| Corrupt state | `uncertain`. `epic` commands never quarantine automatically. |
| Legacy standalone state | Never moved, adopted or rewritten (D17). |
| LOCAL | Unaffected. LOCAL state never lives under `scheduler/`, and `epic` commands are REMOTE-only. |

*Rejected.*

- **A namespace per (EPIC, issue).** One issue would get two states.
- **A title-derived name.** Titles change.
- **Namespaces under `.autoforge/` in the working tree.** They would be
  unfindable, given `--state-dir`, and visible in the operator's tree.

### 2.6 Scheduler state namespace (decision 6)

**D6.1 (#181).** `scheduler/epics/<n>/scheduler.json`,
`autoforge.scheduler/1`, one per EPIC, holds run-level facts only:

- `schema`;
- `repository` (owner/name for display, `repository_id`);
- `epic` (number, issue `id`);
- `invocation`:
  - `invocation_id`, 32 lowercase hex characters, new for every
    `epic run`, `epic resume` or `epic archive` process;
  - `started_at`, `ended_at`;
  - `end_reason`, a closed enum: `nothing-admissible`, `drained`,
    `stopped`, `interrupted`, `degraded`;
- `config` (`max_parallel`, `poll_interval_seconds`, `stop_grace_seconds`,
  `config_digest`);
- `admission`: `open`, `closed` or `degraded`, with a closed-enum reason
  (`closed`: `stop-requested`, `interrupted`; `degraded`:
  `snapshot-refused`, `epic-not-admissible`);
- `last_snapshot` (digest, `observed_at`, counts), diagnostic only;
- `created_at`, `updated_at`.

**Amended by #183.** `epic archive` writes no `scheduler.json` and starts
no invocation: only `epic run` and `epic resume` processes get an
`invocation_id`. Archive acts on one issue's claim and state under L0 and
that issue's L2 (D17.4), and has none of the facts the file records (no
admission, no snapshot, no configuration digest to compare). Writing it
would also replace the record of the last scheduler invocation, so a
scheduler that crashed without finalizing would no longer show in
`epic status` as not ended, which is the hint that sends the operator to
`epic resume`. Nothing reads an invocation id from an archive: the
claim it closes keeps the invocation that opened it.

**D6.2 (#181).** **It lists no claims.** Claims live only in each issue's
`claim.json`, so the two files have no consistency relationship to maintain
and nothing to disagree about.

**D6.3 (#181).** **Corruption.** A corrupt or unknown-version
`scheduler.json` is global uncertainty: no admission. The explicit recovery
is `epic run --force`, which archives the file with the existing
link-then-unlink quarantine primitive (`state.py` `quarantine_state_file`
:1783) and creates a new one. That is safe because the file holds no
claim; per-issue claims are reconciled independently (D14).

### 2.7 Durable claim schema (decision 7)

**D7.1 (#181).** **`claim.json`**, one per issue namespace,
`autoforge.scheduler-claim/1`, strict and bounded:

```text
schema            "autoforge.scheduler-claim/1"
run_key           {repository_id, issue_number}
issue_id          GitHub numeric issue id
epic              {number, issue_id}                 EPIC under which the namespace was bound
next_generation   integer >= 1, monotonic within the file
current           null | Claim
history           [ClosedClaim], at most 16, oldest dropped
updated_at

Claim
  claim_id        32 lowercase hex, random
  generation      integer >= 1
  invocation_id   32 lowercase hex
  kind            "start" | "resume"
  admission       {snapshot_digest, node_digest, observed_at}
  state_ref       {present: bool, run_id: str | null, settlement: Settlement | null}   at admission
  created_at
  worker          null | {pid, process_start_time, spawned_at, log}    diagnostics only, never authority

ClosedClaim = Claim + closed {at, classification, settlement | null, exit_status | null, detail}
  classification  "settled" | "interrupted" | "superseded" | "abandoned" | "archived"
  detail          redacted, at most 500 characters
```

Every value is bounded and typed. There is no free-form dictionary,
provider output or transcript.

**D7.2 (#181).** **Properties.**

- **Claim before launch.** The claim is durably written (temporary file,
  fsync, replace) before the worker process exists.
- **At most one open claim per issue.** `current` is one slot. Replacing an
  open claim requires the classification `resumable`, under L2.
- **One closing rule.** `superseded` is used only in the atomic write that
  installs the replacing claim. A claim closed without a replacement is
  `abandoned` when no worker is known to have run (none was recorded, and
  this scheduler reaped none), or `interrupted` when one is. The other
  classifications are `settled` (the state's settlement is not
  `in_progress`) and `archived` (D17.4).
- **A stale claimant cannot finalize a newer generation.**
  - Only the L0-holding scheduler writes the file.
  - Before every write it compares `(claim_id, generation)` on disk with
    its own record. A mismatch is `uncertain`.
  - A worker refuses to run unless `current` equals its argv
    `(claim_id, generation, invocation_id)` (D9.2).
- **Release only after the outcome is durably observable.** A claim whose
  worker was spawned is closed only after the worker has exited, the
  scheduler has re-acquired L2 and the per-issue state has been strictly
  loaded. The worker's engine wrote that state before exiting. Every close
  holds L2; the two that need no loaded state are a claim whose worker
  never started (D2.3) and `epic archive`, which closes a claim whatever
  the state holds (D17.4).
- **Unknown is never success.** An uncertain claim stays open, its issue is
  held, and nothing is launched for it.
- **No TTL.** Timestamps and PIDs are diagnostics.

**Amended by #181.** `abandoned` versus `interrupted` is decided by the
claim alone: `abandoned` when no worker is recorded on it, `interrupted`
when one is. A worker this scheduler reaped before `record_worker` wrote
its diagnostics (a crash, or a failed write, in between) is therefore
closed `abandoned`, not `interrupted` as "this scheduler reaped none" above
and D14.2's "after this scheduler reaped the issue's worker" suggested. The
reason: the writers and the loader can enforce the rule from the file
alone, and no in-memory fact can contradict the history written to disk.
Nothing decides on the difference. Both classes leave the issue
`resumable` or `fresh` by its state, and the exit status is still
recorded. #182 records the diagnostics right after the spawn, so the
window is that short.

**D7.3 (#181).** The worker diagnostics are the PID, the process start
time as the kernel reports it (`/proc/<pid>/stat` field 22 on Linux, the
process's start time on macOS), the spawn time and the worker log's path.
They let an operator recognise a hung worker (D9.6); nothing decides on
them.

**D7.4 (documentation only).** **Naming.** "Scheduler claim" is always
local. It is never a GitHub `ai-*` marker and does not live in `claims.py`,
which owns GitHub claims.

### 2.8 Live ownership proof (decision 8)

**D8.1 (documentation only).**

> Issue N has a live owner exactly when some process holds the exclusive
> `flock` on `scheduler/issues/<n>/run.lock` (L2).

- **Standalone runs need no detection.** A standalone run takes no L2 and
  is excluded from scheduler runs by L0, so the scheduler never needs to
  detect it.
- **What is never proof:** a claim's existence or age, a stored PID, a
  process found by PID, timestamps, log activity, stdout, or a provider
  process. PIDs are reused, and a PID cannot tell a worker from an orphaned
  agent. Kernel locks are released on any death, SIGKILL included, and
  cannot expire while held.

**D8.2 (#181).** **How the L0-holding scheduler observes it:** through its
own unreaped child, to which it handed L2; or through a failed
`flock(LOCK_EX | LOCK_NB)` on `run.lock`, which counts as `observed_live`
for that tick.

**D8.3 (#180 the probe; #183 its use).** **How `epic status` observes it,**
lock-free: a `LOCK_SH | LOCK_NB` probe on a descriptor opened
`O_RDONLY | O_NOFOLLOW | O_CLOEXEC` without `O_CREAT`, released at once. A
missing `run.lock` means no owner. A probe can make the scheduler's
simultaneous L2 attempt fail; the scheduler then treats the issue as live
for one tick and retries, which has no safety impact.

**D8.4 (#180).** **Descriptor hygiene.**

- `run.lock` and `git.lock` are opened `O_CLOEXEC`, like the common-dir
  descriptor L0 is taken on.
- The worker marks its inherited descriptors non-inheritable (D9.2), and
  every launch of an agent, validator, `git` or `gh` keeps
  `close_fds=True`; the only descriptors such a launch passes are the
  sandbox backend's own two (`sandbox_bwrap.py` :439), never a lock. The
  worker spawn (D9.6) is the one launch that passes L0 and L2, and it
  passes nothing else.
- So no agent, validator, `git` or `gh` process ever holds L0, L1 or L2.
  Otherwise an orphaned agent would keep an issue live, and the repository
  closed, forever: safe, but stuck. #180 adds a regression test (§6).

**D8.5 (documentation only).** **What an agent can reach.** Under an
enforced sandbox, nothing under `<common>/autoforge` except its own issue's
worktree and sandbox directory (ADR 0005 D6.1): no namespace, no
`git.lock`. Under `disabled` an agent can open these files by absolute
path, as it can open `state.json` today (ADR 0005 §1.1). It can delay, never
forge ownership: a lock it takes is held only while its process lives,
which ADR 0002 ends with the invocation except for D13.4's orphans, and it
makes the issue look live, so nothing is launched; a file it damages is `uncertain` (D14.2), so
the issue is held. A deliberate rewrite of a claim is outside the threat
model, like a hand-edited state file.

### 2.9 Worker architecture and the scheduler loop (decision 9)

**D9.1 (#182).** **The worker.**

- A worker is one OS process running one `ControllerEngine` on one issue:
  `sys.executable -m autoforge.cli epic _worker …`, a hidden internal
  subcommand, started from the scheduler's own interpreter, never through a
  `PATH` lookup.
- Its argv carries the repository root, the configuration path (when the
  scheduler was given one), the EPIC and issue numbers, `claim_id`,
  `generation`, `invocation_id`, the inherited descriptor numbers, and
  `--accept-config-change` when the operator passed it to `epic resume`
  (D9.4). No environment variable authorizes anything.

**D9.2 (#182; the proof is #180's `ControllerLock.adopt`).** **Startup
protocol.**

1. **Prove that each inherited descriptor holds its lock.**
   `flock(LOCK_EX | LOCK_NB)` on the inherited descriptor itself proves
   nothing: on a description that already holds the lock it is a no-op,
   and on one that does not, it silently acquires. So the proof is:
   - open the same object afresh (`O_RDONLY | O_NOFOLLOW | O_CLOEXEC`,
     never `O_CREAT`, plus `O_DIRECTORY` for the coordination root): the
     coordination root for L0, this namespace's `run.lock` for L2;
   - require that `fstat` of the inherited descriptor and of the fresh one
     give the same `(st_dev, st_ino)`, and that this is the expected
     object;
   - require that `flock(LOCK_EX | LOCK_NB)` on the fresh descriptor fails
     with `EWOULDBLOCK`, so some open file description holds the lock;
   - close the fresh descriptor at once.

   The fresh descriptor is a separate open file description, so the probe
   can never acquire or release the inherited lock. On any failure the
   worker exits without touching state. The proof shows that the lock on
   the expected object is held and that the inherited descriptor names that
   object. With the claim match of step 3, it rejects launch errors. A
   worker forged by hand by the operator is outside the threat model, like
   a hand-edited state file.
2. Mark both descriptors non-inheritable.
3. Strictly load the binding and the claim, and require `current` and the
   RunKey to match argv.
4. Require the configuration digest to equal the one recorded in
   `scheduler.json`.
5. Construct the engine with `state_dir = <namespace>/state`, anchored at
   the coordination root through `StatePaths` (D5.3), the adopted L0
   (D10.2) and the scheduler execution policy (D9.3).
6. When the namespace holds a state, run #219's recorded-configuration
   gate exactly as `resume` does (D9.4); then run.

**D9.3 (#182).** **The scheduler execution policy** is a set of generic
engine inputs; the engine learns no scheduler concept.

- **The merge gate is forced closed:** `allow_merge=False`, whatever
  `safety.allow_merge` says.
- **`extra_stop_phases`,** a new generic `run()` input, is checked before
  every step, the first included. The worker passes {`MERGE`,
  `UPDATE_EPIC`}, so its stop phases are `TERMINAL_PHASES` ∪
  {`READY_FOR_MERGE`, `MERGE`, `UPDATE_EPIC`}.
  - The worker returns before executing `MERGE` or `UPDATE_EPIC`, so
    `next_issue_url` is never read and `reset_for_new_issue` never runs.
  - A worker whose state starts in `MERGE` or `UPDATE_EPIC` exits without
    a step. `MERGE` is not an unblock target (`UNBLOCK_TARGETS`,
    `transitions.py` :155): a persisted `MERGE` arises only under an open
    merge gate (a crash, or a verification left inconclusive for
    `resume --allow-merge`), which a namespace never has. `UPDATE_EPIC` is
    reachable through `unblock`.
- **Non-interactive.** stdin is `/dev/null`, so once #126 lands an
  interaction under the `prompt` policy degrades as ADR 0007 D7.4 says for
  a controller without a terminal: a durable pause where the provider's
  gate allows it, otherwise a failure. A worker never waits for a human.
- **`should_continue`,** a second generic input, is a cancellation
  predicate checked before each step (D13.3, D16.4).
- **Recovery hints name commands that work on a namespace.** The worker
  sets the engine's global flags to the scheduler's `--config` and
  `--state-dir <namespace>/state`, and a generic resume-command input to
  `autoforge [--config <path>] epic resume <epic>`. So a hint that resumes
  prints `epic resume` (with `--accept-config-change` where #219's gate
  refused), and a hint that unblocks prints `unblock` with the namespace's
  `--state-dir` and the `--config`, which D17.3 permits. A hint never
  prints a standalone `resume`, which refuses a namespace state dir
  (D17.3), and never drops the namespace or the configuration.
- **Signal handlers.** The scheduler installs SIGINT, SIGTERM and SIGHUP
  handlers that only set a flag. A worker installs one handler, which
  turns SIGTERM into `KeyboardInterrupt`, so the existing teardown runs. No
  other process installs a handler, and standalone behaviour is unchanged.

**D9.4 (#182).** **The two entry refusals are kept.**

- **#219.** A worker that finds a state runs `check_run_configuration`
  before its first step, as `resume` does. A difference refuses with exit 2 and
  nothing written, unless the operator ran
  `epic resume --accept-config-change`, which the scheduler forwards to
  every `resume` worker it launches in that invocation; the acceptance is
  then logged and recorded in each issue's own state, exactly as
  `resume --accept-config-change` does. `epic run` has no such flag. A
  worker that finds no state (a `start` claim, or a `resume` claim after a
  crash before the first save) records its configuration as any new run
  does. The
  scheduler's `config_digest` check (D9.2 step 4) is separate: it binds a
  worker to its scheduler, not a run to its history. A refused worker's
  state is unchanged and in progress, so its claim closes `interrupted`
  with the exit status, and the issue is `held:resumable` for the rest of
  the invocation (D9.6); its worker log holds the refusal with the command.
- **#211.** An acceptance action parks the state in `BLOCKED`, which is the
  settlement `terminal`, so the issue is `held:settled-namespace`. A worker
  never approves or declines. `epic status` prints the
  `unblock --approve-action` and `--decline-action` commands with the
  namespace's `--state-dir` and the `--config`; the operator runs one after
  the scheduler has exited (`unblock` takes L0), then `epic resume`.

**Amended by #243.** Operator actions (ADR 0004 K10: comments on other
issues that the operator posts and the controller only verifies) park the
state in `BLOCKED` the same way, so the issue is `held:settled-namespace`.
A worker verifies them only in the engine's `FIX` prefix pass, as a
standalone run does; it never binds or declines, and the scheduler never
interprets, performs or verifies an action. For a `BLOCKED` namespace,
`epic status` names the namespace's `unblock` and `epic resume` as the
steps, and says in the row that an operator action is posted by the
operator (`status --action X-...` prints its exact payload), bound with
`unblock --bind-evidence X-... <comment URL>` or declined with
`--decline-action X-...`, all run after the scheduler has exited. The
row names the commands' shapes; the ids, and each action's commands with
the namespace's `--state-dir` and the `--config`, are listed by the
namespace's own `status`, which the row names. That `status` names
`epic resume`, never a standalone `resume`, as the continuation (D17.3),
for an acceptance action as well. K10 is not a controller write, so D13.2's
table needs no row. The operator's steps are in
[BLOCKED on operator actions](../usage.md#blocked-on-operator-actions).

**D9.5 (#182).** **A fresh issue** starts through the same path as
`autoforge run <epic> <issue>`: `INITIALIZING` (`_verify_issue_selectable`),
then `ANALYZE_EXECUTE`, whose entry reconciliation
(`_reconcile_analyze_entry`, `engine.py` :5962) recovers an existing PR
(`_recover_pr` :5856) before anything is launched, and whose first launch
creates the worktree. An existing implementation PR is adopted by marker
exactly as in a standalone run, and ambiguity blocks.

**D9.6 (#182).** **Process relationship.**

- **Output.** The scheduler opens `workers/<generation>.log`
  (`O_APPEND | O_CREAT | O_NOFOLLOW | O_CLOEXEC`) and passes it as the
  worker's stdout and stderr.
  - No pipe connects the two processes, so the scheduler's death neither
    kills nor wedges a worker.
  - The content is the worker's ordinary, already-redacted CLI output.
    #182 bounds its size.
  - It is never read for recovery.
- **Session.** Workers start in their own session, so terminal signals
  reach only the scheduler, which forwards them deliberately (D16).
- **Supervision.** An executor-layer handle spawns with `pass_fds`, signals
  the live child, and polls or waits with a deadline. It carries no
  workflow semantics.
  - The scheduler is not a subreaper and runs no contained invocation, so
    ADR 0002's process-wide containment is untouched.
  - Each worker's own agent and validation launches are contained as
    today.
  - ADR 0002 governs what an *agent* starts. A worker is a controller, and
    it deliberately survives a scheduler crash so it can reach its own
    durable boundary (D14).
- **No respawn within an invocation.**
  - A worker that exits with an in-progress state closes its claim as
    `interrupted`. Its issue is held `resumable` for the rest of the
    invocation, and the next `epic run` or `epic resume` admits it again.
  - This needs no crash-loop counter.
  - A worker never becomes another issue's worker.
- **A hung worker.** The scheduler never kills a worker on a timeout.
  `epic status` shows each open claim's recorded worker PID and process
  start time. The operator checks them against `/proc/<pid>/stat` (or `ps`
  on macOS) and kills the process by hand. After the scheduler has died and
  its last worker has exited, unfinalized claims stay open: `epic run`
  refuses and `epic resume` is required.
- **Checkout drift fans out.** Each engine reads HEAD and branch of the
  operator's checkout before and after each of its invocations
  (`_invoke_phase_anchored`, `engine.py` :2383). If one worker's agent
  moves that checkout, every worker whose invocation spans the move detects
  it and enters `BLOCKED`, not only the one that caused it. That is safe
  and expected, and it is documented.

**D9.7 (#182).** **The scheduler loop is foreground, not a daemon.** Each
tick:

1. reap exited workers and finalize their claims (D14);
2. check signals and the stop request (D16);
3. if admission is open and slots are free, take a snapshot (at most once
   per `poll_interval_seconds`) and admit (D2.2).

The scheduler exits when no worker is live and nothing is admissible. It
never waits for a human.

**D9.8 (#183).** `epic run --dry-run` takes the snapshot, prints the
ordered verdicts and the plan, takes no lock, and creates no namespace,
claim, worktree or worker.

### 2.10 Repository orchestration ownership (decision 10)

**D10.1 (documentation only).** **L0 is the existing `ControllerLock`,
unchanged in mechanism.** There is exactly one orchestration authority per
coordination root at a time. It is one of:

- one standalone mutating command (`run`, `step`, `resume`, `unblock`,
  `local …`, and #126's decision commands once landed), exactly as today;
- one scheduler invocation (`epic run`, `epic resume`, `epic archive`) plus
  the workers it launched. The workers share its L0 by inheriting the same
  open file description.

**D10.2 (#180).** **Workers adopt L0 and never acquire it.**
`ControllerLock.adopt(fd)` proves that the inherited descriptor holds the
lock (D9.2) and wraps it. It never writes the PID record, which keeps
naming the scheduler: diagnostic only.

**D10.3 (#180).** **Shared descriptors are released by `close` only.**
Once any descriptor of L0 has been passed to a child, it is released by
`close()` only, never by `LOCK_UN`: `LOCK_UN` on a shared open file
description would release the lock for the scheduler and every sibling at
once. The scheduler switches its `ControllerLock` to a close-only release
mode before its first spawn, so the change also covers an exit by
exception. An adopted lock is always close-only. The same rule applies to
every L2 descriptor, in the scheduler and in workers. Today's `release()`
(`flock(LOCK_UN)`, then `close`) stays the standalone path, where no
descriptor is shared.

**D10.4 (#180).** **Surviving workers keep the repository closed.**

- If the scheduler dies, its live workers keep L0 until they exit.
- Until then, a new scheduler or standalone command fails fast with the
  existing `LockError` (exit 2), and the message says that an orchestrator,
  possibly surviving workers, is active.
- This prevents split-brain: no second orchestrator can start next to
  surviving workers.

**D10.5 (documentation only).** **Unlocked commands stay unlocked.**
`status`, `doctor`, `epic status`, `epic stop` and dry runs take no lock.

**D10.6 (documentation only).** **Only an inherited descriptor proves a
worker.** No environment variable (in particular nothing like
`AUTOFORGE_I_AM_THE_SCHEDULER=1`), PID file or claim authorizes one. The
claim match is a consistency check, not authority.

### 2.11 Lock hierarchy and order (decision 11)

**D11.1 (#180).** The domains:

| Domain | Lock | Who takes it | How |
|---|---|---|---|
| Orchestration | L0: `flock` on the common-dir descriptor | standalone mutating commands; the scheduler (workers inherit it) | `LOCK_EX \| LOCK_NB`, held for the command or scheduler lifetime. Close-only release once shared (D10.3). |
| Per-issue run | L2: `scheduler/issues/<n>/run.lock` | only the L0-holding scheduler, which hands it to exactly one worker | `LOCK_EX \| LOCK_NB`. Hardened open like `controller.lock` (`O_NOFOLLOW`, `O_CLOEXEC`, regular file, one link, never unlinked). The scheduler closes its copy right after the spawn, by `close` only. |
| Shared Git | L1: `<common dir>/autoforge/git.lock` | any controller process (standalone or worker) around a controller write to git's shared data in the common dir (D12) | `LOCK_EX` with a bounded blocking wait (D11.3). Held for one critical section of D12.1 (one git write, the worktree check-then-create, or the REMOTE sandbox directories' creation), never across an agent invocation, a GitHub call, a state save or another acquisition. |
| External effect | none new | — | Controller effects are per issue and are serialized by that issue's L2 (worker) or L0 (standalone); D13 shows why their targets are per issue. Independent issues' effects are not serialized against each other. No `SchedulerEffect`. |
| Parent EPIC | deferred to Wave 2 | — | Wave 1 workers never write the EPIC (no `UPDATE_EPIC`, roadmap splice or progress comment) and never merge. |
| Scheduler files | none | single writer: the L0 holder. One exception: `stop-request` (D16.4). | Lock-free readers, through atomic replace. `stop-request` is published by `link(2)` and never replaced. |

**The generalized #16 invariant.** One orchestration authority per git
common dir (L0); at most one worker per issue, and every write to a
per-issue state under that issue's exclusive authority (L2 for a worker, L0
for a standalone command); and every controller write to git's shared data
in the common dir (its object store and its worktree registry) serialized
by the shared-Git lock (L1). AutoForge's own files under
`<common>/autoforge` are not git data and are covered by L0 or L2 instead
(D12.1).

**D11.2 (documentation only).** **Order: L0 → L2 → L1.**

- L0 and L2 are only ever acquired non-blocking.
- L1 is the only blocking acquisition, and its holder acquires nothing
  while holding it.
- So no wait-for cycle can form.
- Standalone commands take L0, then L1 as needed. They never take L2: L0
  already excludes every worker.

**D11.3 (#180).** **The L1 wait is bounded** by the longest timeout of any
operation L1 guards (today the network fetch's `NETWORK_TIMEOUT_SECONDS`,
600 s) plus 60 s, so a waiter behind one operation within its own bound
never times out. A wait that exceeds it is a typed failure of the git
operation it guarded, handled exactly like that operation failing (a fetch
before a launch: nothing launched, phase unchanged, `resume` tries again);
it is never a `LockError` exit and never retried in place. #180 names the
type.

**D11.4 (#180).** **Filesystem.**

- Scheduler mode refuses to start when the coordination root is on a
  filesystem whose `flock` is not a local kernel lock: NFS, CIFS/SMB, 9p,
  AFS, cluster filesystems, and every FUSE filesystem (sshfs is one). #180
  fixes the detection list for Linux and macOS, and also checks each opened
  `run.lock` and `git.lock` descriptor (`fstatfs`), so a mount below the
  root cannot evade the refusal.
- The live-ownership proof depends on local locks.
- Standalone behaviour on such filesystems is unchanged.

*Rejected.*

- **Removing `ControllerLock`, or locking only around state writes.**
  Either reopens #16.
- **One lock per state directory.** Standalone `--state-dir` values are
  arbitrary, so two of them could own one issue.
- **A blocking L2.** It would let the scheduler wait on a hung worker.

### 2.12 Shared-Git critical sections (decision 12)

**D12.1 (#180).** The inventory, from §1.1, on `main` `77caf43`:

| Controller operation | Where | What it writes in the common dir | L1 |
|---|---|---|---|
| Worktree existence and registration check plus `git worktree add --detach` | `engine.py` `_ensure_agent_worktree` (:1853, :1959) | `worktrees/<id>/`, the worktree | yes, the whole check-then-create section. Defensive: L0 serializes it today, and in scheduler mode one issue's L2 already excludes a second creation of the same path; L1 serializes the shared administrative writes of `git worktree add` for different issues. |
| Controller fetch | `git_transport.py` `GitTransport.fetch` (:526), for every caller: `engine.py` :6089, :9224, :9428, :10052; `premerge.py` :152; `read_commit`'s re-fetch (`git_transport.py` :673) | objects (loose or one pack) in `objects/`; no ref, no `FETCH_HEAD` | yes, taken inside `GitTransport.fetch` itself, so no caller can miss it. Only standalone runs reach the pre-merge fetch: workers never open the merge gate. |
| Strict import of the private objects | `agent_worktree.py` `import_private_objects` (:486), from `engine.py` :2021 and :2032 | one pack in `objects/pack/` | yes, around `index-pack` alone. The walk, `rev-list` and `pack-objects` read the store and write only into a private scratch directory. |
| Sandbox directories | `engine.py` `_sandbox_dirs` (:2333), from `_remote_facts` | `autoforge/sandbox/<key>/{objects,overlay-work,cache}` | yes, in REMOTE, around the whole creation, the shared parent `autoforge/sandbox` included, and not the launch after it. They are not git data, and the key is the issue's, so only that issue's owner (L2 or L0) creates `<key>`; but every issue's engine writes the shared parent, and #180's acceptance criteria put every controller write to the common dir under L1 (#168's inventory note on #180). A lock not obtained in time refuses the launch as a sandbox directory that cannot be prepared. LOCAL's `<git dir>/autoforge/sandbox/local` is unchanged. |
| Controller push (K1) | `git_transport.py` `GitTransport.push` (:751) | nothing: a private `GIT_DIR`, no local ref | no. A commit it must re-fetch goes through `GitTransport.fetch`, which takes L1. |
| Lock directory and PID record | `locking.py` | `autoforge/`, `controller.lock` | no: written under L0, by its acquirer only (D10.2) |
| Worktree removal or prune | none: AutoForge never removes a worktree | — | not applicable |

**D12.2 (documentation only).** **Read-only operations take no L1:**
`rev-parse`, the registry and candidate reads (#223), `cat-file`,
`ls-tree`, the byte-exact exports (ADR 0006 D6, both prefixes; D5.4) and
`--no-optional-locks` status. Git readers are safe against concurrent
writers, because objects and refs are written atomically.

**D12.3 (#180).** **Rules.**

- A controller write to the common dir runs with automatic maintenance off
  for that call (`gc.auto=0`, `maintenance.auto=false`), so it never starts
  a background gc in the shared store. The transport already sets both and
  passes `--no-auto-gc`; `local_git_request` sets neither, so the worktree
  creation and the import gain them. #180 verifies the exact options
  against the installed git.
- #180 wraps every controller common-dir write present on `main` when it
  starts, from a fresh inventory, not only this table. Any later change
  that adds one takes L1.

**D12.4 (documentation only).** **The agent residual** depends on the
sandbox mode.

- **Enforced** (`required`, and REMOTE `preferred`): the common dir is
  read-only to the agent, and its objects land in its private object
  directory (ADR 0005 D6.1). An agent writes only its own worktree and its
  own admin dir, which no other worker touches, and git maintenance inside
  cannot modify the shared store. No residual remains.
- **`disabled`:** an agent commits into the shared store and can run any
  local git command there (it has no credential, so it cannot fetch or
  push). Concurrent agents share objects, `packed-refs` and `config`; git
  protects each with its own lock files. A collision surfaces as a
  transient git error inside that agent's phase ("cannot lock ref", "could
  not lock config file"), which the per-issue engine's existing failure
  rules handle; it is not corruption. An agent's own automatic gc can run
  beside a controller fetch or import: git keeps unreachable objects
  younger than its prune grace, and a commit the controller needs and no
  longer finds is fetched again by SHA (`read_commit`) or refused, never
  assumed. This is today's residual of one agent next to the operator's own
  git, made more likely by parallelism. The operator documentation
  recommends `sandbox.mode: required` for scheduler runs (§8).
- Branches are per issue (`autoforge/<n>`), and only the controller pushes,
  so no two workers write one branch ref.

**D12.5 (#180).** **Evidence.** #180 tests with real git in temporary
repositories (§6). Each test ends with `git fsck --full` clean, and every
failure it provokes must be typed and transient. A safety claim of this
section that no passing test backs is removed from this record by #180,
with the rationale recorded here, before #180 merges.

| Claim | Test #180 adds |
|---|---|
| Worktree creation for different issues under L1 leaves a consistent registry and a sound store | `test_l1_concurrent_worktree_add_for_different_issues` |
| Two controller fetches into one common dir, from two workers, leave a sound store and no ref | `test_l1_concurrent_controller_fetches_into_one_common_dir` |
| An import concurrent with a read-only byte-exact export of another commit neither fails the export's proof nor corrupts the store | `test_l1_import_concurrent_with_read_only_export` |
| Agents committing on distinct detached worktrees at once (the `disabled` residual) lose no commit, and any failure is a transient git error | `test_concurrent_agent_commits_on_distinct_worktrees` |
| Two `_sandbox_dirs` calls for different keys at once both succeed, each holding L1 alone | `test_l1_concurrent_sandbox_dirs_for_different_keys` |
| No launch inherits L0, L1 or L2 | `test_no_launch_inherits_a_lock_descriptor` |
| L1 writes run with automatic maintenance off | `test_l1_writes_disable_automatic_maintenance` |
| Every call site of a common-dir write holds L1 | `test_every_common_dir_write_holds_l1`, `test_remote_sandbox_dirs_are_created_under_l1_and_the_agent_runs_without_it` |

Every claim above is backed by its test (`tests/test_shared_git.py`, and
`tests/test_lock_domains.py` for the descriptors), so #180 removed none. On
git 2.56, a fetch into a store past `gc.autoPackLimit` repacks it unless
automatic maintenance is off (either option alone already stops it; both
are given), while `worktree add` and `index-pack` start no maintenance
either way and carry the options all the same. #180 also moved the REMOTE
sandbox directories' creation under L1, where D12.1 first said "no": #168's
inventory note on #180 names it a controller write to the common dir, and
#180 puts every such write under L1. Its two tests above back the change.

### 2.13 Stale-worker fencing before controller effects (decision 13)

**D13.1 (documentation only).** **Invariant:** a worker whose scheduler
claim is no longer current must not begin a new controller-owned external
effect.

**Proof that the existing locks already provide it.**

1. A claim stops being current only through a write to `claim.json`
   (superseded, closed or archived).
2. Only the L0-holding scheduler writes `claim.json`. It closes or
   supersedes a claim only while it holds that issue's L2 itself, acquired
   non-blocking.
3. A worker holds L2 from its fork until its process ends. Kernel locks do
   not expire, and no process ever calls `LOCK_UN` on a shared descriptor
   (D10.3).
4. So when a claim stops being current, its worker has already exited or
   never started. A process that has exited begins no effect.
5. The scheduler performs no external effect (D1.2).
6. A worker of an older generation cannot start after a newer claim
   exists: it verifies `claim.json` against its argv before constructing
   the engine (D9.2), and it could not hold L2 in any case.

**D13.2 (documentation only).** **Checked against the effects as they
landed.** A worker can make these controller GitHub writes, and each
targets an object derived from its own issue:

| Write | Target |
|---|---|
| K1 push | `refs/heads/autoforge/<n>`, the bound PR's verified head ref, or the replacement branch derived from the issue number and the transaction id |
| K2 implementation PR, K3 adoption | the PR carrying the issue's implementation marker, or the open PR on `autoforge/<n>` |
| K4 review comment | the bound PR, by a marker naming PR, round, HEAD, base and merge base |
| K5 follow-up issue | a new issue, by a marker naming the PR's finding |
| K6 follow-up append | an issue handed over at entry as one of this PR's follow-ups |
| K7 replacement PR | by the issue's replan transaction id |
| K9 acceptance comment | the run's own issue |
| Replan close, its receipt and the compensating reopen | the issue's own source PR |

K8 (the EPIC progress comment) and the roadmap splice run only in
`UPDATE_EPIC`, and the merge and disabling auto-merge only around `MERGE`; no worker reaches either
(D9.3). Each effect record lives in its issue's own state, so there is no
shared journal. Two workers therefore never target one object, except
when a human has planted one PR's follow-up marker in another PR's
follow-up issue, so that a K6 of each could target it. The second worker is
then one more concurrent editor inside the read-to-write window that ADR
0004 already accepts for human edits (K3, K6 "Residual"); the
body-equals-base precondition catches every edit outside that window.

**D13.3 (#182).** **Consequences.**

- No generation is propagated into the effect records, and ADR 0004's
  invocation contract is unchanged.
- The optional child "Fence controller-owned effects to the current
  scheduler claim generation" is not created.
- As a programming-error guard, not a fence, the worker proves that its L2
  is still held (the fresh-descriptor proof of D9.2) in `should_continue`
  at each step boundary, and stops before the next step if the proof fails.

**D13.4 (documentation only).** **Inherited residuals, not introduced
here.**

- An unsandboxed agent orphaned by a SIGKILLed worker can still change its
  worktree (ADR 0002 §4e; a sandboxed one dies with the worker). It holds no GitHub credential (#165),
  so it cannot perform an external effect. The scheduler never SIGKILLs a
  worker.
- A controller-owned `gh` or `git` process in flight when its worker is
  SIGKILLed is not contained, and can complete after the worker has exited.
  It can then race the next generation's reconciliation of that effect.
  This is today's residual for SIGKILL followed by `resume`, unchanged:
  ADR 0004's reconciliation reads back whatever such a call left. Only an
  operator's manual kill can cause it, since the scheduler never SIGKILLs a
  worker.
- A worker that hangs keeps L2 and L0. Nothing new starts for that issue,
  and no second orchestrator starts. The hang is visible to the operator
  (D9.6) and is never taken over by a timeout.

### 2.14 Crash and recovery classification (decision 14)

**D14.1 (#181).** **When it runs:** at every scheduler start, before any
admission, for every namespace bound to this EPIC; and again for each
reaped worker.

**D14.2 (#181).** **Per-issue procedure, holding L0.**

1. The scheduler's own unreaped child is live, and the issue is skipped.
2. Try L2 non-blocking. A failure is `observed_live`: launch nothing and
   re-check next tick. At startup this is a status probe, a leaked
   descriptor or a process outside AutoForge holding the lock (D8.5); if it
   persists, it is reported.
3. Holding L2, strictly load the binding, the claim and the state,
   read-only.
4. Classify. The classes are total and mutually exclusive.
   - **`uncertain`**, checked first, when any of these holds:
     - the state is corrupt or has an unknown protocol;
     - the binding or the RunKey does not match;
     - the claim file is corrupt or has an unknown schema;
     - an open claim's `state_ref.present` is true but the state is
       absent;
     - the state's issue or EPIC differs from the binding;
     - the claim file's `epic` differs from the binding's;
     - a claim file or a state exists but `binding.json` does not.

     Keep the claim open, hold the issue (`held:uncertain`), report the
     exact reason, and never launch. A directory with no binding, no claim
     and no state (a crash between taking L2 and writing the binding) is
     treated as no namespace.
   - **`settled`:** the state loads and its settlement is `terminal`,
     `ready_for_merge`, `handoff` or `awaiting_input`. Close an open claim
     as `settled`. The issue is held `held:settled-namespace` and never
     relaunched.
   - **`resumable`:** the state loads and is `in_progress`, whether or not
     a claim is open; or a claim is open, the state is absent, and the
     claim's `state_ref.present` is false (a crash between the claim and
     the worker's first save).
     - **After this scheduler reaped the issue's worker,** the worker's end
       is known: the claim is closed `interrupted`, and the issue is held
       `resumable` for the rest of the invocation (D9.6).
     - **Otherwise** the open claim was left by an earlier invocation, and
       it is **not** closed here (D7.2). When admission reaches the issue,
       the new claim supersedes it in one atomic write. When a snapshot
       gives the issue any verdict other than `ready` (`closed`, `blocked`
       or `unsupported`), or no longer lists it (`orphaned`), the scheduler
       takes L2 and closes the claim as `abandoned` (no worker was
       recorded) or `interrupted` (a worker was recorded). When admission
       closes first (stop or interrupt), the claim stays open, and the next
       start needs `epic resume`.
   - **`fresh`:** the binding is valid and matches, no claim is open, and
     the state is absent (after `epic archive`, say, or after an
     `abandoned` claim). Admissible exactly like an issue without a
     namespace.

   The `state_ref.present` rule applies only to an open claim. A state that
   is absent while no claim is open is never uncertain.
5. Release L2, by `close`.

**D14.3 (#181).** **Global uncertainty** means no admission at all and an
exit with a diagnostic. The triggers:

- `scheduler.json` is corrupt or of unknown version;
- `repository_id` differs between the live repository and a binding or
  `scheduler.json`;
- the coordination root is on a refused filesystem (D11.4).

**D14.4 (#181; the commands are #183's).** **`epic run` versus
`epic resume`.**

- `epic run` refuses while any claim bound to this EPIC is open, and
  points to `epic resume`.
- `epic resume` runs the classification first and then admits like
  `epic run`.
- Both admit `resumable` namespaces whose claims are closed, as `resume`
  admissions, and `fresh` namespaces as `start` admissions.

**D14.5 (#181; #183 tests each row end to end).** **Crash windows.**

| Window | Durable facts | Recovery |
|---|---|---|
| Ready issue selected, crash before the claim | nothing, or a namespace directory holding only `run.lock` | re-snapshot; the bare directory is no namespace |
| Binding written, crash before the claim | binding; no claim; no state | `fresh` |
| Claim persisted, crash before spawn | open claim without `worker`; no state, or a state unchanged since admission; L2 free | `resumable` with no progress, claim kept open. Re-admission supersedes it. If a snapshot shows the issue is no longer ready, it is closed `abandoned`. |
| Spawned, crash before the worker diagnostics are recorded | open claim without `worker`; the worker may be alive, holding the inherited L0 and L2 | no new scheduler can take L0 until the worker exits; then normal classification |
| Worker running, scheduler crashes | open claim; worker alive with L0 and L2 | the worker continues to its stop boundary and exits (no pipe to the dead scheduler). `epic resume` fails fast on L0 until then, then classifies. |
| Worker settled, crash before finalization | open claim; settled state; L2 free | `settled`: finalize without relaunch |
| Finalization written | closed claim | nothing: `scheduler.json` holds no claims |
| Worker killed mid-step | open claim; valid in-progress state | `resumable`: an ordinary engine resume, where the engine's own crash recovery picks the phase |
| Worker killed between an effect intent and its write | as above | ADR 0004's reconciliation inside the next worker |
| Worker killed with a validator attempt marker saved | as above | the next worker converts the marker into an `INTERRUPTED` record, built from the marker alone, before anything else of its step (ADR 0006 D10.7, D11.3; `state-and-recovery.md`) |
| `epic archive` crashes after closing the claim, before moving the state aside | closed `archived` claim; state still present | re-run `epic archive`: the claim part is already done, and the state is moved aside. Until then the namespace classifies by its state. |
| `epic archive` crashes after moving the state aside | closed `archived` claim; state absent | `fresh`; re-running `epic archive` reports nothing left to archive |

**What success is never inferred from:** a missing PID, an old timestamp,
absent stdout, a provider's disappearance, or a worker exit status of 0.

### 2.15 Worker stopping boundary (decision 15)

**D15.1 (#181).** **The settlement helper.** `issue_settlement(state)`
takes the whole strictly loaded state, not only its phase. #126's pause is
invocation state, not a phase, so the helper checks #126's durable pause
predicate first and only then maps the phase:

| Settlement | Condition | Scheduler meaning |
|---|---|---|
| `awaiting_input` | #126's durable pause predicate holds, once #126 has landed. Checked before the phase mapping. | Settled. |
| `terminal` | phase `DONE`, `BLOCKED` or `FAILED` | Settled. `BLOCKED` needs `unblock` (a standalone command, run after the scheduler exits, D9.4), then `epic resume`. |
| `ready_for_merge` | phase `READY_FOR_MERGE` | Settled. The stop phase was reached with the merge gate closed. |
| `handoff` | phase `MERGE` or `UPDATE_EPIC` | Settled. Never executed by a worker. |
| `in_progress` | any other phase | Resumable. |

**D15.2 (documentation only).** **What `ready_for_merge` does not mean.**
It does not mean that the merge gate was verified or that a certificate
applies, and it satisfies no dependency. The scheduler never reads a
certificate. **Downstream readiness:** a downstream child becomes ready
only when its native blocker is closed as `completed`. In Wave 1 that means
that a human merges (and closes, if the PR does not close the issue) before
downstream work begins. That is accepted.

**D15.3 (#182).** **With `safety.allow_merge: true`:**

- Workers still stop before `MERGE`.
- `epic run` and `epic resume` have no `--allow-merge` flag.
- When the configuration enables merging, they print once at start:
  "safety.allow_merge is enabled in configuration, but scheduler workers
  stop at READY_FOR_MERGE and never merge in this version; configuration is
  not changed."
- The configuration file is not modified. This is the scheduler execution
  policy (D9.3), not a configuration rewrite.
- A human merges on GitHub. Scheduler runs do not update the EPIC roadmap
  or progress comment.

### 2.16 Ctrl-C and stop behaviour (decision 16)

**D16.1 (#182).** **Signals reaching the scheduler.** The scheduler's
handlers for SIGINT, SIGTERM and SIGHUP only set a flag (D9.3). The loop
then:

1. Closes admission (`admission = closed`, reason `interrupted`). Nothing
   new is claimed or spawned.
2. Sends SIGINT to every live worker through its live handle. The child is
   unreaped, so its PID cannot have been reused.
3. Waits, bounded by `scheduler.stop_grace_seconds` (D18.4).
4. Reaps and finalizes each exited worker (D14).
5. Exits 130.

After SIGHUP the terminal may be gone, and writes to it fail with `EIO`.
Those failures are ignored, so they never abort claim finalization; durable
output goes to files.

**D16.2 (documentation only).** **A worker's answer to SIGINT is the
existing one:** `KeyboardInterrupt`; the executor tears down the agent's
process group; the engine exits without advancing the phase; the state
stays valid and in progress, the claim is closed `interrupted`, and the
issue is `resumable`.

**D16.3 (#182).** **A second Ctrl-C, or the grace expiring.**

- The scheduler forwards a second SIGINT. That is the existing standalone
  second-interrupt behaviour: teardown is abandoned, and an agent may be
  orphaned (the ADR 0002 residual).
- The scheduler then exits without waiting further. Unfinalized claims stay
  open and are classified at the next start.
- Workers still alive keep L0 until they exit.
- The scheduler never sends SIGKILL to a worker.

**D16.4 (#182; the command is #183's).** **`autoforge epic stop <epic>`**
runs from another terminal and takes no lock. `stop-request`,
`autoforge.scheduler-stop/1` (`schema`, `invocation_id`, `mode`: `drain`
or `now`, `requested_at`), is the one scheduler file with a second,
lock-free writer, so it follows its own rules:

- It is published atomically and never replaced. `epic stop` writes and
  fsyncs a uniquely named temporary file in the same directory, `link(2)`s
  it to `stop-request`, and unlinks the temporary. `EEXIST` means that a
  request already exists: `epic stop` reports it and writes nothing. A
  reader therefore never sees a partial request. Its content binds the
  `invocation_id` read from `scheduler.json`.
- After writing, `epic stop` re-reads `scheduler.json`. If the invocation
  id has changed, it reports "scheduler changed; stop not delivered".
- It refuses when `scheduler.json` is absent, and creates no directory.
- Only the L0 holder removes a request or a leftover temporary.
- A request that does not parse, or has an unknown schema, is treated as a
  drain by every reader, never as a crash, and is reported.

Modes:

- **Default (drain).** The scheduler closes admission (reason
  `stop-requested`). Each worker checks the request in `should_continue`
  between steps and returns at its next step boundary, with its state valid
  and in progress. The drain is bounded by the existing per-phase timeouts
  that bound every step.
- **`--now`.** The scheduler behaves as on Ctrl-C (D16.1).
- **No live scheduler (it crashed).** Workers of that invocation that are
  still running see a drain request at their next boundary, whatever its
  mode. `--now` cannot reach them, because AutoForge never signals a
  process by recorded PID. The command says so and lists the live
  namespaces from the lock probe (D8.3).
- **Stale requests.** The L0 holder removes a request bound to another
  invocation at start and on every tick, and one that does not parse at its
  next start.
- **No escalation through the file.** Because the request is never
  replaced, a drain cannot be turned into `--now` by a second `epic stop`.
  The operator presses Ctrl-C in the scheduler's terminal instead, which has
  the `--now` effect.

**D16.5 (documentation only).** **What stop never does:** delete
worktrees, discard candidates, reset or rewrite state, mark unfinished work
complete, or write to GitHub.

### 2.17 Migration and backward compatibility (decision 17)

**D17.1 (documentation only).** **Standalone behaviour is unchanged:**
`run`, `step`, `resume`, `unblock`, `status`, `doctor` and `local …`;
`--state-dir` and `config.state_dir`; sequential `UPDATE_EPIC` with
`next_issue_url` and `reset_for_new_issue`; the merge gate; #219's
configuration gate and #211's acceptance actions. No existing state is
migrated. **Exclusion in time** is L0 (D10).

**D17.2 (#180, #181).** **Exclusion in ownership.**

- **Forward guard: a standalone run against a scheduler-owned issue.**
  - A namespace that has an open claim or any `state/state.json` owns its
    issue until `epic archive` releases it. So does one whose run lock a
    live owner holds (the D8.3 probe), and one that cannot be read
    (fail-closed). #180 implements the state and run-lock halves and the
    reverse guard's read (`ownership.py`); #181 adds the open-claim half
    with the claim schema.
  - A standalone controller refuses to bind or continue an issue that a
    namespace owns, whatever that state's phase. A scheduler-`BLOCKED` or
    `READY_FOR_MERGE` issue therefore never gets a second, standalone
    state.
  - It is checked in two places, through one ownership predicate; the
    engine never parses scheduler files itself.
    - Under L0, standalone `run`, `step`, `resume` and `unblock` apply it to
      the issue they would act on (the argument, or the loaded state's
      `current_issue_url`) and refuse with a typed error naming
      `epic archive`. An old standalone state in any state dir therefore
      cannot continue.
    - A standalone engine is also given it as one more rejection fact
      inside `_verify_issue_selectable`, so a sequential run's
      `next_issue_url` that points at a scheduler-owned issue is rejected
      through the existing bounded rejection path
      (`MAX_NEXT_ISSUE_SELECTIONS`).
  - A worker engine is given no predicate: its issue is fixed and it
    selects no other. `unblock` against a namespace state dir exempts that
    namespace.
  - No phase or transition is added.
- **Reverse guard: a scheduler admission against a standalone-owned
  issue.**
  - Before admitting, the scheduler reads the standalone state at the
    configured `state_dir`, read-only and only if present.
  - It holds as `held:standalone-state` the issue that state names as
    `current_issue_url`, in any phase other than `DONE`. That includes
    `BLOCKED` and `FAILED`, so a fresh scheduler namespace can never launder
    a standalone run's loop bounds or bypass its `unblock`.
  - An unreadable standalone state holds nothing by itself, but is
    reported. Admission does not depend on parsing it.
  - Other `--state-dir` locations are unknowable, so the scheduler may admit
    an issue such a state names. The forward guard then refuses to continue
    that state. That residual is today's (two sequential runs with
    different state dirs on one issue), and the engine's PR adoption by
    marker keeps it from creating a second PR.

**D17.3 (#178).** **Namespace paths.** Standalone `run`, `step` and
`resume` refuse a state dir inside `<common>/autoforge/scheduler/`,
decided on the resolved path. `status` may read one. `unblock` may edit
one; it takes L0, so only while no orchestrator is live.

**D17.4 (#183; the claim closure is #181's).**
**`autoforge epic archive <epic> <issue>`** is the only way to retry a
settled issue or recover from a corrupt state, and it is never automatic.
It runs under L0 and L2, with no live owner, in this order:

1. closes an open claim as `archived` (a corrupt claim file is moved aside
   with the same primitive instead);
2. moves `state/state.json` aside as `state.json.archived-<timestamp>`,
   with the link-then-unlink primitive of `quarantine_state_file`
   (`state.py` :1783);
3. keeps the logs, the worktree, the binding and the claim history.

Each step is idempotent for a part that is already gone, so re-running
after a crash completes the archive (D14.5 lists both windows). A crash
between the link and the unlink leaves both names; the re-run moves
`state.json` aside again.

The next admission starts fresh (`fresh`), and the engine adopts any
existing marked PR, so archiving discards no GitHub work. It does
deliberately discard the per-issue loop-bound evidence (review rounds, fix
attempts, replan count) and the issue's validation records and
certificate, which live in the archived state. That is the point of an
explicit retry, and the reason it is never automatic. Archiving is also the
operator's only way to hand a scheduler-owned issue to a standalone run,
for example to merge it through the standalone merge gate.

**D17.5 (documentation only).** **LOCAL** is unchanged and is excluded from
scheduler runs by L0. `epic` commands are REMOTE-only.

### 2.18 Schema ownership and configuration (decision 18)

**D18.1 (each named child).** Ownership:

| File or surface | Schema | Owner | Policy |
|---|---|---|---|
| `binding.json` | `autoforge.run-binding/1` | #178 | own version; unknown version refused |
| `claim.json` | `autoforge.scheduler-claim/1` | #181 | own version; unknown version refused |
| `scheduler.json` | `autoforge.scheduler/1` | #181 | own version; unknown version refused |
| `stop-request` | `autoforge.scheduler-stop/1` | #182 | own version; an unknown version or a request that does not parse is read as a drain and reported, never refused (D16.4); published by `link(2)`, never replaced |
| `state/state.json` | `AutoForgeState`, `__protocol_version__` | existing (#23) | unchanged; no scheduling field |
| config `scheduler.max_parallel`, `scheduler.poll_interval_seconds`, `scheduler.stop_grace_seconds` | config schema | #182 | strict key validation |
| CLI `epic run\|status\|resume\|stop\|archive`, `--max-parallel`, `--dry-run`, `--force`, `--now`, `--accept-config-change` | — | #183 | — |

**D18.2 (each child, for its files).** **Rules for every scheduler file.**

- Never share or consume the per-issue protocol number.
- Never combine with #126 interaction state, ADR 0004 effect records or ADR
  0006 validation records.
- Strict loading: unknown fields are refused; strings and lists are
  bounded; ids are validated; there are no free-form dictionaries, no
  provider output and no transcripts; diagnostics are redacted.
- Atomic writes go through `safefs` (temporary file, fsync, replace),
  anchored at the coordination root (D5.3). The one exception is
  `stop-request` (D16.4).
- Corruption fails loudly. A `stop-request` is the one exception: it is
  read as a drain (D16.4).
- Run logs and worker logs are never read for recovery.
- A future per-issue field needed only for scheduling must be justified in
  its own issue and announced on #23 first.

**D18.3 (#182).** **`max_parallel`.**

- Default 2. Valid values are 1 to 4; anything outside is refused. There is
  no 0-means-unlimited and no CPU-count default.
- `--max-parallel` overrides it within the same bounds.
- It bounds live workers. Each worker runs at most one agent invocation at
  a time (ADR 0002) and runs its validation commands one after another, so
  it also bounds concurrent provider processes and validation runs.
- Raising the bound is a later configuration-schema change.

**D18.4 (#182).** **The other two keys.**

- `poll_interval_seconds`: default 60, valid 15 to 3600. It bounds how
  often a snapshot is taken; a tick with no free slot takes none.
- `stop_grace_seconds`: default 30, valid 10 to 600. The default exceeds
  the executor's agent teardown (SIGTERM, 5 s grace, SIGKILL) plus a state
  save.

## 3. What does not change

- **The per-issue engine** keeps every phase, legal edge, loop bound,
  recovery rule and verification. No phase, edge or per-issue state field
  is added, and no protocol number is consumed.
- **ADR 0004's effects** keep their kinds, records, reconciliation,
  invocation contract and transport. No generation enters an effect record
  (D13.3).
- **The merge gate** and its certificate check (ADR 0006 D13) are
  unchanged and never reached by a worker.
- **ADR 0005's sandbox decisions** are unchanged. The scheduler root is
  under the already masked `<common>/autoforge`, outside both carve-outs.
- **Standalone commands, LOCAL mode and dry runs** behave as today, except
  for the two ownership refusals (D17.2, D17.3), which only an issue a
  namespace owns can meet.

## 4. Composition with other decisions

- **ADR 0001 (LOCAL workspace identity).** Unaffected: LOCAL never runs
  under a scheduler, and its state never lives under `scheduler/`.
- **ADR 0002 (executor).** Relied on and unchanged. One contained
  invocation per process holds in each worker; the scheduler runs none. A
  worker is a controller that survives its scheduler on purpose (D9.6).
- **ADR 0003 (Pi).** Unaffected: providers are chosen inside the worker.
- **ADR 0004 (authority boundary).** Composed. Every effect a worker makes
  targets its own issue's objects (D13.2); the effects of independent issues
  are no longer serialized against each other, which no decision of ADR
  0004 relied on. Its §2.4 rejection of "an outbox table, queue or worker"
  stands: a worker here is a whole controller, not an effect executor.
- **ADR 0005 (outer sandbox).** Composed. The worktree, the sandbox
  directories and the import are per issue (D5.4, D12.1); the import and
  the worktree creation take L1. The `disabled` residual is D12.4.
- **ADR 0006 (validation evidence).** Composed. Records and certificates
  stay in each issue's state; `READY_FOR_MERGE` and a certificate satisfy
  no dependency (D15.2); exports need no namespace (D5.4). "Under the
  repository lock" in D11.3 there reads, in a worker, as under the issue's
  L2 with the inherited L0 (D11.1).
- **ADR 0007 (SDK providers, #126).** Composed. Workers are non-interactive
  (D9.3); `awaiting_input` is a settlement once #126 lands (D15.1); #126's
  decision commands take L0 (D10.1).
- **#23.** No protocol number is consumed.

## 5. Decision ownership

Each sub-decision has exactly one implementing child, or is documentation
only. Where one is split, each part names its child: the steps of D2.2,
the ownership predicate inside D5.5 (#181, D17.2),
the probe and its use in D8.3, the lock proof inside D9.2, the commands of
D14.4, the end-to-end tests of D14.5, the command of D16.4 and the claim
closure of D17.4. No decision re-implements a child of #158 (#159–#175): the effect
records, reconciliation and publication (#160–#164), the credential policy
(#165), the sandbox decisions (#166–#169) and the validation evidence and
certificates (#170–#175) are consumed, not changed.

| Child | Scope | Decisions |
|---|---|---|
| #178 | run identities and namespaces | D2.2 step 5 (the binding), D5.1–D5.5, D17.3 |
| #179 | dependency snapshots and the ready set | D2.2 steps 1 and 4, D2.5, D3.1–D3.6, D4.1, D4.2, D4.4 |
| #180 | lock domains | D2.2 step 2, D8.3 (the probe), D8.4, D9.2 step 1 (`ControllerLock.adopt`), D10.2–D10.4, D11.1, D11.3, D11.4, D12.1, D12.3, D12.5; D17.2's state and run-lock halves of the forward guard and the reverse guard's read |
| #181 | scheduler claims and restart reconciliation | D2.2 steps 3 and 5 (the claim), D6.1–D6.3, D7.1–D7.3, D8.2, D14.1–D14.5, D15.1, D17.2 (the open-claim half); the claim closure of D17.4 |
| #182 | the bounded worker supervisor | D2.2 (the orchestration) and its step 6, D2.3, D4.3, D9.1–D9.7, D13.3, D15.3, D16.1, D16.3, D16.4, D18.3, D18.4 |
| #183 | `epic` commands and end-to-end recovery tests | D8.3 (its use in `epic status`), D9.8, D14.4 (the commands), D14.5 (end to end), the `epic stop` command of D16.4, D17.4 |
| documentation only | | D1.1, D1.2, D2.1, D2.4, D7.4, D8.1, D8.5, D10.1, D10.5, D10.6, D11.2, D12.2, D12.4, D13.1, D13.2, D13.4, D15.2, D16.2, D16.5, D17.1, D17.5 |
| this record | | the pointer in `docs/agent-guides/state-and-recovery.md` to the generalized #16 invariant (D11.1) |
| each child | | D18.1 and D18.2, for its own files, keys and commands; the guide and operator documentation of its own decisions |

### 5.1 Where this record differs from #177's body

- **Line numbers** are re-read on `main` `77caf43` (§1.1). `ControllerLock`
  is at `locking.py` :132 (`acquire` :266, `release` :301); in `engine.py`,
  `locked()` is at :3781, `_agent_worktree_name` :1771,
  `_ensure_agent_worktree` :1853, `_verify_issue_selectable` :5801,
  `merge_gate_open` :7453, `_apply_update_epic` :13864 and
  `MAX_NEXT_ISSUE_SELECTIONS` :612; the checkout reads are
  `_invoke_phase_anchored` :2383; in `state.py`, `AutoForgeState` :390,
  `reset_for_new_issue` :1226, `StatePaths` :1584 and
  `quarantine_state_file` :1783; `_CONTAINMENT_LOCK` is at `executor.py`
  :917 (refused at :1025); `edit_issue_body` at `github.py` :1325.
- **The state protocol is 11, not 5** (10 when this record was accepted;
  #243 took 11). The scheduler's schemas stay outside `AutoForgeState`
  (D18.2).
- **Agents no longer push or fetch.** Every REMOTE GitHub write is the
  controller's: an effect K1–K9, or one of ADR 0004 D5.4's writes (the
  merge, disabling auto-merge, the replan close, receipt and reopen, the
  roadmap splice). D1.1, D11.1 and D13 name them, and D13.2 checks the fence argument against the kinds as
  they landed; its conclusion stands.
- **There are five controller writes to the common dir, not two** (§1.1).
  #161's verified default-branch fetch and #168's strict import have
  landed, along with the `REVIEW`, `FIX` and `REPLAN_REEXECUTE` fetches.
  The three that write git's data and the REMOTE sandbox directories'
  creation take L1 (D12.1); the lock's own files are inventoried and take
  none.
- **The controller's push updates no local ref.** #177's row "controller
  push transport (local remote-tracking ref update)" does not exist: the
  push writes nothing to the common dir, and its re-fetch is a fetch
  (D12.1).
- **#166 design A has landed** for enforced launches (ADR 0005 D6.1, #168),
  so the agent residual is only `disabled`'s (D12.4), and agents no longer
  fetch or push in any mode.
- **Validation exports** need no per-issue namespace (D5.4).
- **Evidence is per issue,** and `READY_FOR_MERGE` or a certificate never
  completes a dependency (D1.2, D15.2).
- **#219's configuration gate and #211's acceptance actions** are kept at
  worker entry (D9.4), and recovery hints never print a standalone
  `resume` for a namespace (D9.3).
- **Descriptor hygiene** names the one `pass_fds` that exists, the sandbox
  backend's own (D8.4).
- **Settled in this record, left open by the body:** `binding.json`'s
  fields (D5.2), the admission and end-reason vocabularies (D6.1), the
  `stop-request` content (D16.4), the L1 wait bound (D11.3), and the
  defaults and bounds of `poll_interval_seconds` and `stop_grace_seconds`
  (D18.4).

## 6. Tests the implementing children must add

Every test is deterministic and offline: fake providers and fake GitHub
(`ExplodingGitHub` where nothing may be written), `tmp_path` repositories,
and no paid provider or model call. Tests that need concurrency use real
processes and real `flock`, never sleeps as synchronization.

**#178 (namespaces).**

- The RunKey and the binding: a repository rename keeps the namespace; a
  different `repository_id` is global uncertainty; an issue `id` mismatch
  is `uncertain`.
- `binding.json` strict loading: unknown fields, unknown schema, out-of-bound
  strings refused.
- The namespace path is independent of `--state-dir`, `config.state_dir`
  and the working directory; every component is opened without following a
  link, and a link planted at any of them is refused.
- Standalone `run`, `step` and `resume` refuse a state dir inside
  `scheduler/`, also through a link or a relative path; `status` reads one.

**#179 (graph and ready set).**

- Recorded fixtures for `/sub_issues`, `/dependencies/blocked_by`, the
  parent and the summaries, pinning every field used.
- Completeness: a short page, a missing page, a count mismatch and an
  exceeded bound each refuse the snapshot; a child whose summary reports no
  blocker is not queried.
- Every row of D3.6, cycles of length 1, 2 and 3 with the canonical
  diagnostic, and de-duplication by `id`.
- The order and the digests are invariant under shuffled API order, and
  titles, bodies and timestamps do not change a digest.
- The admission re-read: any eligibility change between the snapshot and
  the re-read is `STALE` and writes no claim.

**#180 (locks).**

- `ControllerLock.adopt`: passes on an inherited held descriptor; fails on
  a wrong object, a descriptor of the right object that holds no lock, and
  a closed descriptor; never acquires or releases.
- Close-only release: a child sharing L0 keeps it after the parent's
  release; `LOCK_UN` is never called on a shared descriptor.
- L2's hardened open: a link, a FIFO, a directory or a second hard link at
  `run.lock` is refused; it is never unlinked.
- The filesystem refusal, by injected `fstatfs` results for each refused
  type, on the root and on a `run.lock` or `git.lock` below a mount.
- The L1 wait bound and its typed failure.
- Every named test of D12.5's table.

**#181 (claims and reconciliation).**

- `claim.json` and `scheduler.json` strict loading and bounds; history
  capped at 16.
- Every classification of D14.2, each `uncertain` trigger, and the
  `state_ref.present` rule.
- Supersede is one atomic write; a stale `(claim_id, generation)` on disk is
  `uncertain`; a claim is closed only after the worker exited and the state
  was loaded.
- `issue_settlement` for every phase, and `awaiting_input` before the phase
  once #126 lands.
- The forward guard in both places and the reverse guard, including a
  standalone state in `BLOCKED` and `FAILED`, and an unreadable one.

**#182 (workers and supervisor).**

- Startup refusals: a claim mismatch, a configuration digest mismatch and a
  failed lock proof each exit without touching state.
- The execution policy: `allow_merge` forced closed with
  `safety.allow_merge: true`, the notice printed once; a state starting in
  `MERGE` or `UPDATE_EPIC` exits without a step; `next_issue_url` is never
  read.
- #219: a resumed worker refuses a configuration difference with nothing
  written, and accepts it with `epic resume --accept-config-change`.
  #211: an acceptance action settles the issue, and `epic status` prints
  the `unblock` commands with the namespace's `--state-dir`.
- Recovery hints printed by a worker name `epic resume` and the namespace,
  never a standalone `resume`.
- Spawn failure with L2 free and with L2 held; no respawn within an
  invocation; capacity never exceeds `max_parallel`.
- Signals: Ctrl-C, SIGTERM and SIGHUP to the scheduler; a second Ctrl-C; the
  grace expiring; `EIO` on the terminal ignored; the scheduler never sends
  SIGKILL.
- `stop-request`: published by `link(2)`; `EEXIST` writes nothing; a changed
  invocation is reported; an unparsable request drains; stale requests are
  removed only by the L0 holder.
- The step-boundary L2 proof stops a worker whose proof fails.

**#183 (commands and end to end).**

- Every row of D14.5, by killing the scheduler or a worker at the window
  (a fault-injection hook, never timing).
- `epic run` refuses with an open claim and points to `epic resume`;
  `epic run --force` archives a corrupt `scheduler.json` only.
- `epic archive`: both crash windows, idempotent re-runs, a corrupt claim
  file moved aside, nothing written to GitHub.
- `epic status` takes no lock and changes nothing; `epic run --dry-run`
  creates no namespace, claim, worktree or worker.
- Manual changes on GitHub between ticks: a blocker added, a blocker
  reopened, a child closed, a child re-parented, the EPIC closed.
- Safety: no worker merges, writes the EPIC or runs `UPDATE_EPIC`; the
  scheduler makes no GitHub or git write.

## 7. Non-goals

- No production code, tests, prompts or configuration in this change.
- No daemon, service, webhook or polling process. The scheduler exits when
  nothing is admissible.
- No multi-host scheduling, distributed lease, database, queue, Redis or
  GitHub-label locking.
- No GitHub write by the scheduler, no `SchedulerEffect`, and no second
  journal.
- No change to ADR 0004's invocation contract, effect records or protocol.
- No automatic merge, `UPDATE_EPIC`, roadmap splice or parent-EPIC progress
  write in scheduler mode.
- No file-scope reservation or conflict-aware admission.
- No new workflow phase, legal edge or per-issue state field.
- No dashboard, event sourcing or generic queue framework.

## 8. Consequences

- **Several dependency-independent children of one EPIC run at once,** up
  to `max_parallel`, each through the unchanged single-issue engine, and
  each stops at `READY_FOR_MERGE` or `BLOCKED` for a human.
- **Downstream work waits for a human merge and close,** because only a
  blocker closed as `completed` satisfies a dependency (D15.2).
- **While a scheduler or its surviving workers live, no standalone command
  runs on the repository,** and the `LockError` says why (D10.4).
- **A scheduler-owned issue cannot be continued by a standalone command**
  until `epic archive` releases it (D17.2), which also discards its loop
  bounds and evidence.
- **Controller fetches, imports and worktree creations of different issues
  are serialized by L1;** a hung fetch can delay another worker's by up to
  the fetch timeout (D11.3).
- **One agent moving the operator's checkout blocks every worker whose
  invocation spans the move** (D9.6).
- **Under `sandbox.mode: disabled`, concurrent agents share the store**
  and can meet transient git lock errors (D12.4).
- **GitHub reads grow** with the number of children and live workers;
  snapshots are bounded by `poll_interval_seconds` and skip children with
  no blocker (D3.3, D18.4). A rate-limit response is a GitHub failure
  under the existing rules.
- **Documentation follows behaviour** in the implementing children:
  `docs/agent-guides/architecture.md` (scheduler and worker boundary),
  `state-and-recovery.md` (namespaces, scheduler files, lock domains),
  `workflow.md` (the scheduler execution policy), `github-safety.md` (the
  graph reads), `docs/usage.md`, `docs/configuration.md` and
  `autoforge.example.yaml`; `docs/usage.md` also recommends
  `sandbox.mode: required` for scheduler runs (D12.4). If an implementation contradicts a decision
  here, the implementing child amends this record, with a recorded
  rationale.
