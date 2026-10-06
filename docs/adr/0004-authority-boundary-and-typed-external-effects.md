# ADR 0004. Authority boundary and typed external effects

- **Status:** accepted. This is a design record; nothing in it is implemented
  yet. It is implemented by #160 (effect records, reconciliation, typed
  operations, the controller git transport, the `UPDATE_EPIC` progress
  comment and the one protocol bump), #161 (`ANALYZE_EXECUTE`), #162
  (`REVIEW`), #163 (`FIX`), #164 (`REPLAN_REEXECUTE`) and #165 (credential
  removal and the agent read path). The number is assigned when the record
  merges: if another ADR merges first, this record takes the next free
  number, and its index entries move with it.
- **Decides:** #159, for EPIC #158 (Wave 1).
- **Where:** placement is decided in §2.12. Existing code this record
  builds on: `src/autoforge/github.py`, `claims.py`, `result_parser.py`,
  `replan_txn.py`, `premerge.py`, `config.py`, `state.py`, `engine.py`,
  `executor.py`, `runlog.py`, `src/autoforge/prompts/*.md`.
- **Related:** ADR 0001 (LOCAL workspace boundary), ADR 0002 (nothing an
  agent starts outlives its invocation), ADR 0003 (Pi provider); #126 / #134
  (interaction records); #147 / #148 / #149 (SDK providers and their policy);
  #23 (protocol numbering); #166 (the Wave 2A outer-sandbox ADR); #170 /
  #171 (Wave 2B candidate evidence and byte-exact export).

## 1. Context

### 1.1 Who writes what today

In REMOTE mode, the agent performs most externally visible writes with the
operator's credentials. The controller verifies them afterwards, by strict
`CONTROL_RESULT` parsing and GitHub read-back. On `main` `2fff4b9`:

| Phase | Write | Actor | Evidence |
|---|---|---|---|
| `ANALYZE_EXECUTE` | push the controller-named branch | agent | `prompts/analyze_execute.md` :51–52 |
| `ANALYZE_EXECUTE` | create the PR with `Closes #n` and the `ai-implementation` marker | agent | `analyze_execute.md` :53 |
| `ANALYZE_EXECUTE` | edit an existing PR body to add the marker (adoption) | agent | `analyze_execute.md` :31 |
| `REVIEW` | post the round comment with the `ai-review-result` marker | agent | `review.md` :167, :212 |
| `REVIEW` | PATCH an existing round comment in place | agent | `review.md` :139 |
| `FIX` | push fix commits to the PR branch | agent | `fix.md` :89–90 |
| `FIX` | create a follow-up issue with the `ai-follow-up` marker | agent | `fix.md` :80 |
| `FIX` | append a follow-up marker to an existing issue body | agent | `fix.md` :64 |
| `FIX` | close a follow-up the agent created in error | agent | `fix.md` :49–50 |
| `FIX` | optional resolution-summary reply on the PR | agent | `fix.md` :91 |
| `REPLAN_REEXECUTE` | publish the replacement branch and create the replacement PR with the transaction and implementation markers | agent | `replan_reexecute.md` §15, §20 (:679, :699, :730) |
| `REPLAN_REEXECUTE` | close the source PR, post the close receipt, reopen it as compensation | controller | `github.py` :1602, :1619, :1630; `engine.py` `_supersede_source` |
| `UPDATE_EPIC` | post the progress comment with the `ai-epic-progress` marker | agent | `update_epic.md` :22 |
| `UPDATE_EPIC` | edit an existing progress comment in place | agent | `update_epic.md` :35–37 |
| `UPDATE_EPIC` | splice the managed roadmap section into the EPIC body | controller | `engine.py` `_apply_update_epic`; `github.py` `edit_issue_body` :995 |
| `MERGE` | merge bound to the reviewed HEAD; disable auto-merge | controller | `github.py` :1577, :1594 |

Two rows were not in #159's inventory and were found while this record
classified every prompt command: closing an erroneous follow-up
(`fix.md` :49–50) and editing a progress comment in place
(`update_epic.md` :35–37). Both are agent mutations, and both are classified
in §2.1.

`DEFAULT_ENV_ALLOWLIST` (`config.py` :129–169) passes GitHub and git
transport credentials (`GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`,
`GIT_SSH_COMMAND`, and `HOME` / `XDG_*`, which reach the operator's gh and
git configuration). The engine applies that list to every agent launch, to
repository-defined validation commands and to pre-merge commands.

### 1.2 Precedents this record generalises

- **The roadmap splice (#13).** The agent returns content only
  (`UpdateEpicResult.roadmap_section`). The controller refuses controller
  marker openers in it (`CONTROLLER_MARKER_OPEN_RE`), writes the managed
  section itself, checks that the body outside the markers is unchanged, and
  reads the result back.
- **The replan close** (`docs/agent-guides/replan-transaction.md`). The intent
  is persisted before the write (`SUPERSEDE_INTENT`). An append-only close
  watermark identifies the write after a crash, the receipt is posted only
  after the close is observed, attempts are bounded by
  `MAX_CLOSE_ATTEMPTS = 2`, and a compensating reopen exists.
- **The pre-merge evidence fetch** (`premerge.py`). `fetch_pr_head` fetches
  objects only, creating or moving no local ref, and `export_commit_tree`
  exports one exact tree.

### 1.3 Hazards that exist today

- **Blind retries of creates.** `_run_gh` (`github.py` :773–813) retries every
  transient failure on every call, creates included. A create that landed on
  GitHub but timed out locally is sent a second time.
- **Agent-writable git configuration.** The per-issue worktree shares the git
  common dir with the operator's checkout (`engine.py` :939), so the agent
  can write its `config` and hooks. `fetch_pr_head` resolves `origin`, its
  transport settings and its hooks from that config. A network `git fetch`
  can also start automatic maintenance, which runs the `pre-auto-gc` hook.
- **Agent-writable object interpretation.** The common dir also holds
  replacement refs and the graft and shallow files, and the shared object
  store holds the commit-graph. Git honours all four when it reads an
  object, so a controller check that names a commit by SHA can read a
  substitute: other parents, another message or another tree.
- **Credentials are not bounded by tool policy.** Claude Code runs with
  `bypassPermissions` by default, and Pi has no approval layer
  (`docs/pi-policy.md`). While an agent holds the operator's credentials,
  "controller-owned" is an instruction, not a boundary.
- **Authorship proves nothing.** The agent's `gh` and the controller's `gh`
  authenticate as the same GitHub login. An object's author cannot tell
  the controller's write from the agent's.

### 1.4 Terms

- **Effect:** one controller-performed write to one exact remote object (§2.2).
- **Kind:** an effect's type, from the closed set in §2.5.
- **Effect record:** the durable record of one effect in controller state.
  An object is **journaled** when an effect record accounts for it. Effect
  records are not the event journal: `events.jsonl` stays write-only and
  audit-only.
- **Entry observation:** the remote values a phase's effects will take as
  their preconditions. The controller observes them before the phase's
  first agent launch and persists them with that launch's charge (§2.4).
- **Logical isolation:** what AutoForge *hands* a child process: its
  environment and configuration (#165).
- **Outer sandbox:** what a child can *reach* when it tries (Wave 2A, #166).
- **Process containment:** what may *outlive* an invocation (ADR 0002).

The three isolation terms are kept apart throughout this record.

## 2. Decisions

Each decision is labelled `D<item>.<n>` and names exactly one implementing
child. §5 repeats the assignment as one table. "Unchanged" rows are not
decisions and name no child.

### 2.1 Operation classification (item 1)

**D1.1 (#165).** Every REMOTE operation falls into exactly one of four
classes. The first two divide the local repository where git divides it. A
linked worktree's private git directory (`<git common dir>/worktrees/<id>/`)
holds its index, its `HEAD` and that reflog, and the per-worktree refs under
`refs/worktree/`, `refs/bisect/` and `refs/rewritten/`. Every other ref, the
object store, the configuration and the hooks live in the common dir, which
the operator's checkout and every other worktree share (`git help worktree`,
"REFS").

- **Agent-local.** Anything confined to the agent's worktree and its
  private git directory: edits, builds, tests, staging, local commits on the
  worktree's `HEAD`, and git commands that only read objects and refs
  (`git rev-parse`, `git log`, `git diff`, `git branch -a`).
  - A local commit also writes to the shared object store: objects, and the
    packs, indexes and commit-graph that git's own maintenance derives from
    them. That is the one shared write an agent's work needs, and it grants
    nothing. The controller names every revision by a SHA it read from the
    worktree's `HEAD` or recorded in its own state (D6.4). It reads that SHA
    as the SHA's own bytes: it honours no replacement ref, graft, shallow
    boundary or commit-graph, and it re-hashes every commit a decision reads
    (D7.5).
  - The candidate is the worktree's `HEAD`. Verification and recovery address
    it as that plus the controller-named *remote* branch, never through a
    local ref.
- **Operator-owned local.** Every other write to the common dir:
  - creating, moving or deleting a shared ref (`refs/heads/*`, `refs/tags/*`,
    `refs/remotes/*`, `refs/replace/*`, `refs/stash`), local branch creation
    included;
  - editing the repository configuration or hooks, or the graft and shallow
    files;
  - worktree administration (`git worktree add`, `move`, `remove`, `prune`).

  None of it is externally visible, so it is not controller-only. The
  operator's checkout sees it at once, though, and the operator owns local
  branches and worktrees (AGENTS.md "Runtime model"). Agents perform none of
  it. The controller's only write there is its one detached `worktree add`;
  its fetches write objects only (D6.4).
- **Agent read-only external.** Reads of the issue, the PR, the diff,
  comments and checks, and of revisions the controller fetched. How an agent
  performs these reads after credential removal is §2.10.
- **Controller-only.** Every externally visible write:
  - push;
  - PR create, edit, close, reopen and comment;
  - issue create, edit, close and comment;
  - merge and auto-merge;
  - label, assignee and milestone changes;
  - remote branch deletion;
  - any other REST or GraphQL mutation.

  A controller-only operation is performed by the controller as a typed
  effect (§2.5) or by one of its existing mechanisms (D5.4), or by nobody.

**No phase needs a shared ref.** The controller adds the worktree detached
and fetches objects only (D6.4), and K1 pushes the candidate SHA to an
explicit remote ref. An agent therefore commits on the worktree's detached
`HEAD`. That `HEAD` keeps its commits reachable, `git gc` included, as a
branch would. #161, #163 and #164 remove the prompt steps that create a local
branch (the D1.2 rows). A phase that ever needs to name a local revision for
an agent uses `refs/worktree/*`, which git keeps in the worktree's private
git directory. No Wave 1 phase needs one.

**Enforcement, in Wave 1 and after.**

- **Wave 1 enforces the operator-owned class by instruction only.** An agent
  can still write shared refs, configuration and hooks (§2.11). What Wave 1
  guarantees is narrower. No controller verification or recovery reads a
  shared ref (D6.4), and no controller git process lets a replacement ref,
  graft, shallow boundary or commit-graph change what a SHA reads as
  (D7.5). The controller's network git reads no shared configuration or
  hook (§2.7), and its local git runs whatever that state can make it run
  with no more authority than the agent (D7.3). A shared ref an agent
  writes anyway therefore grants nothing, and a moved `HEAD` or branch of
  the operator's checkout still blocks the run.
- **A read-only common dir changes the enforcement, not the classes.** If
  Wave 2A (#166) makes the common dir read-only to agents, operator-owned
  writes become impossible, and no phase loses anything it needs. #166 must
  still give an agent what the agent-local class uses: a writable private
  git directory, which lives under the common dir, and somewhere to write
  objects, such as the private object directory and strict import that §4
  names.

**D1.2 (#165).** The classification is provider-neutral: it names
operations, not tools. #165 records it in `docs/agent-guides/github-safety.md`,
where #148 and #149 cite it. The table below covers every mutation in §1.1
and every `gh` and `git` command in the current REMOTE phase prompts. LOCAL
prompts make no GitHub call and are out of scope.

| Operation | Where today | Class | After Wave 1 | Owner |
|---|---|---|---|---|
| push the controller-named branch | `analyze_execute.md` :51–52 | controller-only | effect K1 | #161 |
| `gh pr create` (implementation PR) | `analyze_execute.md` :53 | controller-only | effect K2 | #161 |
| `gh pr edit <url> --body-file` (adoption) | `analyze_execute.md` :31 | controller-only | effect K3 | #161 |
| `gh pr comment` (round comment) | `review.md` :167, :212 | controller-only | effect K4 | #162 |
| `gh api -X PATCH …/issues/comments/<id>` | `review.md` :139 | controller-only | removed; correction in place is not a kind (D5.2) | #162 |
| push fix commits | `fix.md` :89–90 | controller-only | effect K1 | #163 |
| `gh issue create` (follow-up) | `fix.md` :80 | controller-only | effect K5 | #163 |
| `gh issue edit <url> --body-file` (marker append) | `fix.md` :64 | controller-only | effect K6 | #163 |
| close an erroneous follow-up | `fix.md` :49–50 | controller-only | removed: the agent no longer creates follow-ups, and an issue close is not a kind | #163 |
| optional resolution reply on the PR | `fix.md` :91 | controller-only | dropped (D5.3) | #163 |
| replacement branch publication and `gh pr create` | `replan_reexecute.md` §15, §20 :679, :699, :730 | controller-only | effects K1 and K7 | #164 |
| `gh issue comment` (progress) | `update_epic.md` :22 | controller-only | effect K8 | #160 |
| edit the progress comment in place | `update_epic.md` :35–37 | controller-only | removed (D5.2) | #160 |
| `gh issue edit` on the EPIC | `update_epic.md` :62 (forbidden to agents) | controller-only | unchanged: the controller's roadmap splice | — |
| source PR close, receipt, compensating reopen | `github.py` :1602, :1619, :1630 | controller-only | unchanged mechanism (D5.4) | — |
| merge; disable auto-merge | `github.py` :1577, :1594 | controller-only | unchanged mechanism (D5.4) | — |
| `gh pr merge`, auto-merge | `common.md` :37 (forbidden) | controller-only | unchanged: the controller merges behind `safety.allow_merge` | — |
| `gh pr close`, `gh pr comment`, `gh pr edit` on the source PR | `replan_reexecute.md` :503–504, :807 (forbidden) | controller-only | forbidden today; impossible for the agent once its credential is gone | #165 |
| `git push --delete` | `replan_reexecute.md` :534 (forbidden) | controller-only | forbidden to agents; no kind, so the controller never does it either | #165 |
| labels, assignees, milestones, any other REST or GraphQL mutation | no prompt | controller-only | no kind, so nobody performs them in Wave 1 | #165 |
| `git branch -D`, `git branch -d` | `replan_reexecute.md` :534 (forbidden) | operator-owned local | unchanged prohibition | — |
| `git worktree add/move/remove/prune` | `common.md` :50–51, `replan_reexecute.md` :536–537 (forbidden) | operator-owned local | unchanged prohibition; the controller's single `worktree add` is unchanged | — |
| `gh issue view … --comments`, `gh issue view <url>` | `analyze_execute.md` :38, `review.md` :29, :94 | read-only external | controller-supplied read context (§2.10) | #165 |
| `gh issue view <url> --json url,state` | `update_epic.md` :88 | read-only external | read context: states of the EPIC's referenced issues | #165 |
| `gh pr view … --comments`, `gh pr view --json headRefOid,baseRefName` | `review.md` :32, :35 | read-only external | read context; the binding is already in the prompt | #165 |
| `gh pr diff` | `review.md` :34 | read-only external | agent-local `git diff` over the controller-fetched bound HEAD and merge base | #165 |
| `gh pr checks` | `review.md` :46 | read-only external | read context: the check summary | #165 |
| `gh pr view <url> --json url,headRefOid,headRefName` (read-back) | `analyze_execute.md` :59 | read-only external | removed with the result fields it fed | #161 |
| `gh pr view <url> --json headRefOid` (read-back) | `fix.md` :93 | read-only external | removed with `new_head_sha` as a target | #163 |
| "SHAs that `gh pr view --json headRefOid` returned" | `common.md` :81 | read-only external | reworded: SHAs come from the agent's local `git rev-parse` | #165 |
| `gh pr list --state open` (prior work) | `analyze_execute.md` :42 | read-only external | the controller's own pre-launch reads | #161 |
| `git fetch` | `analyze_execute.md` :42 | network read | controller pre-launch fetch, objects only (D6.4) | #161 |
| `git fetch origin <sha>` | `review.md` :39 | network read | controller pre-launch fetch of the bound HEAD and merge base | #162 |
| `gh pr checkout`, `git pull` | `fix.md` :73–74 | network read, and a local branch (operator-owned local) | controller pre-launch fetch of the reviewed HEAD; the agent checks it out detached and creates no local branch | #163 |
| "Fetch the remote repository" | `replan_reexecute.md` §15 | network read | controller pre-launch fetch of the verified default branch | #164 |
| `git branch -a`, `git rev-parse HEAD`, local commit | `analyze_execute.md` :43, :51; `fix.md` :74, :89 | agent-local | unchanged; the commit lands on the worktree's detached `HEAD`; never load-bearing | — |
| create or check out the local branch `{{BRANCH}}` | `analyze_execute.md` :46–48 | operator-owned local | removed: the agent commits on the detached `HEAD`, from the controller-fetched default branch or prior-work head; K1 creates the remote branch | #161 |
| create the replacement branch | `replan_reexecute.md` :542, §15 :565 | operator-owned local | removed: the agent commits on the detached `HEAD`, from the controller-fetched verified default branch; K1 creates the remote replacement branch | #164 |
| "Use the `gh` CLI for GitHub reads/writes"; "git and gh credentials"; "inspect the real current state … (branch push, PR creation, comment, follow-up issue)" | `common.md` :45, :55, :64–66 | — | reworded once every write has moved | #165 |

**D1.3 (#165).** **A provider interaction approval never grants a
controller-only operation.** An approval (a #126 / #134 interaction, a
provider permission prompt, an SDK permission callback under #147–#149)
changes what a provider tool may *attempt*. It never changes which
credentials the process holds. #165 makes the rule hold by construction,
because the credential a controller-only operation needs is absent from
every agent and repository-command runtime.

*Rationale.* A classification by operation outlives any provider's tool
names, and the approval rule follows from it. Tying the boundary to
credentials means no provider permission mode, interaction answer or SDK
callback can widen it.

*Rejected.*

- **A per-provider deny list as the boundary.** Claude Code runs with
  `bypassPermissions`, Pi has no approval layer, and a tool can reach `gh`
  through a shell. #149 may still map this table into SDK deny rules as
  defence in depth.
- **Classifying by command name** (`gh pr …`, `git push`). `gh api` and
  GraphQL make every write reachable through one command.
- **Letting an approval grant a write for one invocation.** That makes the
  authority depend on interaction state, which #126 keeps separate (§4).

### 2.2 What an external effect is (item 2)

**D2.1 (#160).** An effect is one controller-performed write to one exact
remote object. It is defined by six things:

- **a kind** from the closed set in §2.5;
- **a deterministic identity** derived from controller state, never from
  agent text. For a GitHub object, the identity is the existing durable
  marker (`claims.py`, or the replan transaction marker) plus the object's
  container. For a push, it is the repository, the ref and the candidate SHA;
- **an exact target:** a repository, plus a PR or issue number or a ref;
- **a precondition:** the expected old value of the target, taken from the
  entry observation (§2.4). Examples are "no object with this identity", a
  ref that is absent or at a recorded SHA, and "this body equals a recorded
  base and carries no marker for this identity" (D5.5);
- **a payload:** the exact bytes to publish (title and body), or the exact
  candidate SHA;
- **a completion criterion:** a specific read-back, defined per kind in §2.5.

**D2.2 (#160).** An effect record persists the six fields above, plus:

- the attempt count;
- the stage;
- the observed result (URL, number or SHA, as read back);
- the binding to its owner: the run, the phase, the issue, the PR, and,
  for `REPLAN_REEXECUTE`, the transaction id;
- its position in the phase's ordered effect plan.

The record is written through the existing atomic persistence and validated
on load like every persisted field. A corrupt or incomplete record fails
loudly and is never replaced by defaults.

**D2.3 (#160).** The stage values form a closed set:

| Stage | Meaning |
|---|---|
| `intended` | validated, persisted, never issued |
| `attempted` | an issue is about to happen or has happened; the count is persisted before each issue |
| `observed` | the read-back matched |
| `conflict` | `BLOCKED`, naming the object |

Only `observed` lets the consuming phase advance.

**D2.4 (#160).** A record's lifetime is one phase entry. The atomic save that
commits the consuming state drops the phase's records in the same write that
folds their observed results into the existing state fields. That save is
the phase transition, or `VERIFIED` for `REPLAN_REEXECUTE`. State therefore
holds at most one phase's records. Each record is bounded as follows:

- **Count.** The largest plan is `FIX`: one push plus at most one follow-up
  effect per open finding (`MAX_FINDINGS_PER_REVIEW = 50`).
- **Payload.** The payload of each field is bounded by GitHub's limit for
  that field. That limit is 65,536 characters for a body or comment at the
  time of writing; #160 re-checks it against the live API.
- **Total.** #160 fixes a total payload bound that keeps the state file far
  below `MAX_STATE_FILE_BYTES`.

Only bounded, redacted values are persisted, and no credential, header or
environment value is ever stored.

*Rationale.* Each of the six fields closes one ambiguity. The identity makes
the write findable after lost local state. The precondition separates "not
written yet" from "written by someone else". The payload makes completion a
comparison rather than a judgement. Per-entry lifetime keeps state bounded
and keeps a later phase's recovery free of earlier phases' records.

*Rejected.*

- **An identity carried in the agent's result** (a URL or branch the agent
  names). That would make the target agent-chosen.
- **A random nonce embedded in each published marker.** It would change the
  marker schemas that #161–#164 keep. It is also unnecessary: the entry
  observation and ADR 0002 already tell the controller's write apart from an
  agent's (§2.4).
- **Keeping records for the whole run.** That grows state without bound, and
  no later phase needs them, because the observed results already live in
  the existing fields.

### 2.3 Request vs execution vs observed result (item 3)

**D3.1 (#160).** The four steps are distinct:

1. **Request.** An agent's `CONTROL_RESULT` may carry *requested content*:
   bounded text and structured data.
2. **Intent.** The controller turns validated content into *effect intents*.
   It persists all of a phase's intents in one atomic save (the effect plan)
   before the first write.
3. **Execution.** Each intent is executed as one write.
4. **Observation.** The *observed result* is what the read-back returns.

Only the observed result advances workflow state. A write's exit status, a
reply body and the agent's claim are never evidence of completion.

*Rationale.* This is the existing rule that `CONTROL_RESULT` is a claim and
GitHub is the source of truth, applied to the controller's own writes.
Persisting the whole plan first makes the effect order durable. For `FIX`,
the follow-ups are created before the push.

*Rejected.* **Advancing state on a successful reply.** A reply can be lost,
and a successful reply does not prove the object still matches, because a
human can edit it before the read.

### 2.4 Crash and idempotency invariants (item 4)

**D4.1 (#160).** The intent is persisted before the write. The record moves
to `attempted`, with its count incremented, in a save made before every
issue. At most one write is issued per reconciled attempt.

**D4.2 (#160).** **Reconciliation comes before any re-issue.** A step that
finds a record in `intended` or `attempted` first reads the target by its
identity. It then decides as follows:

- **The target is in the intended end state** (identity and payload
  match): `observed`.
- **The precondition still holds** and the attempt bound is not exhausted:
  issue once.
- **Anything else:** `conflict`, which is `BLOCKED` naming the object. That
  covers two objects, a different payload, a closed or merged object, or a
  moved ref.

A target already in the intended end state at `intended` is accepted as
observed. Since the agent exited, only the controller and humans can have
written it (D4.4), and the end state is exactly the one intended.

Body appends (K3, K6) refine the second case. An append that was never
issued, and whose target body has changed since its base was recorded, is
rebased, and the rebased payload is checkpointed before it is issued
(D5.5).

**D4.3 (#160).** **No blind retry of a create.** An ambiguous outcome (a
timeout, a transient error or an unparsable reply) is never re-sent at the
transport level; it is reconciled instead. The rules around it:

- Reads keep `_run_gh`'s transient retry.
- The attempt bound is 2 issues per effect, following the
  `MAX_CLOSE_ATTEMPTS` precedent.
- A definitive refusal (a non-transient 4xx such as a validation error, or
  a rejected push lease) is `conflict` and is never retried.
- A transient failure during a reconciliation *read* leaves state unchanged.
  `resume` retries it, and it is not charged as a step.

**D4.4 (#160).** **The entry observation is the launch fence.** Before a
publishing phase's first agent launch, the controller reads the values its
effects will need as preconditions:

- the remote branch head (absent, or a SHA);
- the base revision a candidate must descend from;
- the marker-bearing objects for the phase's identities.

It persists them in the same save that charges the launch (the existing
`attempt` save). Values already persisted are reused, not copied: the review
binding, and the replan `PREPARED` checkpoint. #160 persists the observation
in a kind-neutral shape, so #161–#164 add no persisted field.

When the agent returns, ADR 0002 guarantees that nothing it started still
runs. The precondition read made before the intents are persisted therefore
compares the remote against a baseline the agent could not influence. Any
difference is something the agent, or a human during the agent's run,
published, and §2.9 applies to it.

**D4.5 (#160).** Identity reads are complete:

- an open-object listing read to the end (the existing #20 rule);
- for create kinds, an all-states read that also sees closed and merged
  objects, bounded either by a natural filter (a PR's head branch) or by a
  number watermark taken before the create (the replan precedent);
- never the search API, whose index lags.

A listing that cannot be read to the end is `BLOCKED`, as today.

**The crash windows,** common to every kind. §2.5 gives each kind's
concrete reads.

| Window | Record on disk | Resolution |
|---|---|---|
| **W1** before the intent is persisted | none | Nothing was published: no write is issued before `attempted` is saved. The step re-runs. The result was never persisted, so the agent is relaunched under the existing attempt accounting, and its local commit is still in the worktree. Entry reconciliation runs first, and any phase identity that differs from the entry observation is unexplained (§2.9). |
| **W2** intent persisted, write not issued | `intended` | Reconcile (D4.2). Intended end state: `observed`. Precondition holds: save `attempted`, then issue once. A body append whose target body changed is rebased in that same save (D5.5). Otherwise `BLOCKED`, naming the object. |
| **W3** write issued, outcome unknown | `attempted` | Reconcile by identity before anything else. Intended end state: `observed`. Precondition still holds and fewer than 2 issues made: save the incremented count, then re-issue. Bound exhausted: `BLOCKED`, naming the effect and the manual step. Otherwise `BLOCKED`. |
| **W4** write landed, state not saved | `attempted` | The W3 reconciliation finds the intended end state: `observed`, with no second write. |
| **W5** read-back mismatch | `attempted` → `conflict` | The identity resolves to an object whose target, payload or state differs, or to two objects. The record becomes `conflict`, which is `BLOCKED` naming every object found. Never repaired in place, never duplicated. |

Two rules apply in every window:

- A record never makes the controller relaunch the agent. A phase with
  effect records completes from the records (journal-first resume).
- Dry-run executes no effect and runs no network git. Its plan names the
  effects it would perform and any recorded effect it would reconcile.

*Rationale.* These are the replan close's rules (intent first, a bounded
attempt count, reconcile before retry), made common to every kind. Without
the entry observation, a value the agent changed during its run would become
the precondition and be adopted as the baseline.

*Rejected.*

- **Idempotency keys on GitHub writes.** GitHub's REST API does not offer
  them for these writes.
- **Retrying creates at the transport and de-duplicating later.** A second
  create cannot be undone safely, and de-duplication has to choose which
  object to keep.
- **Reading the event journal for recovery.** It is audit-only by contract.
- **An outbox table, queue or worker.** This is a single process under a
  local lock; the effects run inside the existing step.

### 2.5 The closed Wave 1 effect-kind set (item 5)

**D5.1 (#160).** The Wave 1 set is closed at these eight kinds. #160
implements a typed operation for each one, and the phase children own the
wiring. Adding a kind later changes the persisted schema, so it is a
protocol bump; this is why the set is closed now.

#### K1. Push an exact candidate (the Git publication layer, §2.6)

- **Consumers:** #161 (the implementation branch), #163 (the fix push),
  #164 (the replacement branch).
- **Identity:** the verified repository, the ref and the candidate SHA. The
  ref is one of:
  - `refs/heads/<derived branch>`, where the implementation branch is
    derived from the issue number (`branch_name_for`);
  - the bound PR's verified head ref, when its head repository is the base
    repository;
  - the replacement branch, derived as a pure function of the issue number
    and the transaction id.
- **Target:** that ref on the verified repository, through the explicit
  URL of §2.7.
- **Precondition:** the ref's expected old value from the entry observation,
  either absent or a full SHA. In addition:
  - the candidate descends from that value and from the recorded base, and
    differs from the base, all checked locally against controller-fetched
    objects;
  - the ref is never the default branch.
- **Payload:** the candidate SHA, which the controller reads from the
  worktree. The agent's reported SHA is only a cross-check. The commit
  messages in the published range (base..candidate) pass the content policy
  for commit messages (D8.5).
- **Completion read-back:** the remote ref, read through the GitHub client,
  equals the candidate. When the ref is a PR's head, the consumer also reads
  the PR's head SHA.
- **Windows:**
  - W2: ref = candidate → `observed`; ref = expected → push once; otherwise
    `BLOCKED` naming the branch and both SHAs.
  - W3 and W4: ref = candidate → `observed`; ref = expected → re-push within
    the bound; otherwise `BLOCKED`.
  - W5: a reported success but ref ≠ candidate → `BLOCKED` as head drift.
  - Inside the replan transaction, `BLOCKED` takes the form of the
    transaction's existing rejection path.

#### K2. Create the implementation PR

- **Consumer:** #161.
- **Identity:** the issue's `ai-implementation` marker and the derived head
  branch, in the verified repository.
- **Target:** the verified repository, base = the default branch, head = the
  derived branch.
- **Precondition:**
  - no PR in any state has the derived branch as head;
  - the complete open listing has no PR with the issue's marker
    (`at_most_one` gives zero);
  - K1 is `observed`.
- **Payload:** a title and body. The body is the agent's validated text
  followed by a controller-rendered `Closes #n` line and the implementation
  marker.
- **Completion read-back:** the all-states listing filtered by head branch
  shows exactly one PR, and it is:
  - open;
  - based on the default branch;
  - at head = the candidate;
  - titled and bodied exactly as the payload.

  Among open PRs, the marker resolves `exactly_one` to it. The controller
  then persists PR, HEAD, base and branch, as `_apply_analyze` does today.
- **Windows:** the generic ones. A closed or merged PR on the branch is a
  conflict and never a reason for a second PR.

#### K3. Adopt an existing PR (append markers to its body)

- **Consumer:** #161.
- **Identity:** the PR number and the issue's implementation marker.
- **Target:** a PR recorded in the entry observation that is:
  - open;
  - in this repository, not from a fork;
  - headed at the derived branch;
  - without an implementation marker.
- **Precondition:**
  - the PR is still open;
  - its head equals the K1 candidate (K1 is `observed` first);
  - its body equals the recorded base and carries no implementation marker
    for the issue (D5.5).
- **Payload:** a body append (D5.5). The block is `Closes #n` and the
  implementation marker, and the base is the PR body.
- **Completion read-back:**
  - the body equals the payload, byte for byte;
  - the marker resolves `exactly_one`.
- **Windows:** D5.5's reconciliation.
  - A body edited before the first issue is rebased and checkpointed, then
    issued.
  - A body that differs from both the payload and the base after an issue
    is a conflict, including one that carries the block but has otherwise
    drifted.
- **Residual:** GitHub offers no conditional body update. A human edit that
  lands between the last read and the write is overwritten. The roadmap
  splice accepts the same window, and the record states it.

#### K4. Create the review round comment

- **Consumer:** #162.
- **Identity:** the `ai-review-result` marker for the bound PR, round,
  reviewed HEAD, base ref and merge base.
- **Target:** the bound PR's top-level comments.
- **Precondition:** no comment carries this round's marker at the bound
  revision. This is read at entry and again before the intent.
- **Payload:** the controller-rendered comment, built from the validated
  result:
  - the heading;
  - the binding line, from the controller's binding;
  - the findings;
  - the agent's prose sections;
  - the needs-fix line;
  - the marker, including `finding_ids`.

  The total is checked against the comment limit (D8.6).
- **Completion read-back:** exactly one top-level comment carries this
  round's marker at the bound revision, and its body equals the payload.
  Its URL is persisted as `last_review_comment_url`, the #80 handoff. Then
  `_apply_review`'s post-review re-read runs unchanged: stale detection,
  round consumption and carry-forward.
- **Windows:** the generic ones. A second matching comment is `BLOCKED`,
  naming both.

#### K5. Create a follow-up issue

- **Consumer:** #163.
- **Identity:** the `ai-follow-up` marker for the PR and finding id.
- **Target:** the verified repository's issues.
- **Precondition:**
  - no open issue carries the marker;
  - no issue created after a number watermark carries it. The watermark is
    persisted with the plan, and the all-states listing is read newest-first
    down to it.
- **Payload:** the agent's validated title and body, followed by a
  controller-rendered reference to the PR and finding, and the marker.
- **Completion read-back:** exactly one issue above the watermark carries
  the marker, and it is:
  - open;
  - in this repository;
  - not the current issue;
  - titled and bodied as the payload.

  The existing rule of one open issue per finding holds.
- **Windows:** the generic ones. A follow-up the controller created and a
  human closed before the save is a conflict, never a reason to create a
  second one.

#### K6. Append a follow-up marker to an existing issue

- **Consumer:** #163.
- **Identity:** the referenced issue number and the `ai-follow-up` markers
  it receives in this phase, one per finding. All of a phase's markers for
  one issue form one effect (D5.5).
- **Target:** an issue the controller handed over at entry
  (`FOLLOW_UP_ISSUES` or `EXISTING_FOLLOW_UP_ISSUES`) that is open, in this
  repository and not the current issue. A K5 issue is created in this phase,
  so it is never a K6 target in the same phase.
- **Precondition:** the issue is open, and its body equals the recorded base
  and carries none of the effect's markers (D5.5).
- **Payload:** a body append (D5.5). The block is the markers, one per line
  in plan order, and the base is the issue body. Markers an earlier round
  appended are part of the base and are kept.
- **Completion read-back:**
  - the body equals the payload, byte for byte;
  - each marker resolves to one open issue per finding.
- **Windows:** D5.5's reconciliation, as for K3.
- **Residual:** the same as K3.

#### K7. Create the replacement PR

- **Consumer:** #164.
- **Identity:** the replan transaction id, carried by the
  `autoforge-replan-transaction` marker, and the derived replacement branch.
- **Target:** the verified repository, base = the verified default branch.
- **Precondition:**
  - no PR above the PR-number watermark carries the transaction marker;
  - no PR in any state is headed at the replacement branch;
  - K1 to that branch is `observed`.
- **Payload:** a title and body. The body is the agent's validated text,
  followed by controller-rendered `Closes #n`, the implementation marker and
  the transaction marker. The marker's counts come from the validated
  result.
- **Completion read-back:** the existing `_bind_replacement` predicates:
  - above the watermark;
  - exactly one valid attestation;
  - the sole implementation claimant;
  - linked to the issue;
  - based on the verified default branch.

  Two checks are added: the bound HEAD equals the recorded candidate, and
  the title and body equal the payload. `verify_attestation` stays as a
  check of the controller's own rendering.
- **Windows:**
  - W3 and W4: the existing marker binding finds the PR → `VERIFIED`.
  - A human closed the new PR before binding: the existing durable rejection
    (`find_non_open_claimant`).
  - W5: a rejection through the transaction's existing path, naming both
    SHAs. It never becomes a new checkpoint.

#### K8. Create the EPIC progress comment

- **Consumer:** #160 (its first consumer).
- **Identity:** the `ai-epic-progress` marker for the implemented issue and
  PR, on the EPIC issue.
- **Target:** the EPIC issue's comments.
- **Precondition:** no comment on the EPIC carries this marker.
- **Payload:** the agent's validated progress text followed by the marker.
- **Completion read-back:** exactly one comment carries the marker (the
  existing `exactly_one`), and its body equals the payload. The roadmap
  splice, the `next_issue_url` verification and EPIC batching are unchanged.
- **Windows:** the generic ones. Two comments block before launch, as today.

**D5.2 (#160).** **Correcting a controller-authored object in place is not a
kind.** Every payload is rendered from persisted bytes (D8.4), so the
controller has nothing to correct. A remote object that differs from its
payload is a conflict (W5), and an operator resolves it. The agent's PATCH
instruction (`review.md` :139) and its in-place edit instruction
(`update_epic.md` :35–37) go away with their phases (#162 and #160
respectively).

**D5.3 (#163).** **The `FIX` resolution reply (`fix.md` step 6) is dropped.**
It is not made a controller-rendered effect. Nothing reads it as authority.
The resolutions are already validated and persisted, redacted, as
`last_fix_resolutions`. #163 supplies the previous `FIX` round's validated
resolutions to the next `REVIEW` prompt as data, since today the reviewer
can only learn them from PR comments. Human readers keep:

- the commits;
- the follow-up issues, each linked to its finding;
- the next round's review comment.

**D5.4 (unchanged; no child).** **The existing controller writes keep their
own mechanisms:**

- merge;
- disable auto-merge;
- the replan close, receipt and compensating reopen;
- the roadmap splice.

Each is already controller-owned, durable, tested and bound to
phase-specific evidence:

- merge is bound to the reviewed HEAD, with idempotent merge counting;
- the close has its watermark, `MAX_CLOSE_ATTEMPTS` and compensation;
- the splice checks the body before and after its write.

Migrating them into effect records would add risk with no new safety.
#160 only pins with a test that the close receipt is presence-checked
(`has_close_receipt`), so a duplicate receipt is harmless.

**D5.5 (#160).** **A body append is a frozen block over a recorded base.**
K3 and K6 are the two kinds whose payload contains bytes the controller did
not render: the target's existing body. Both follow one rule.

- **The block** is the controller-rendered text the kind appends. It is
  rendered once, persisted, and never re-rendered (D8.4).
- **One effect per target.** All of a phase's appends to one object form
  one effect, whose block carries every marker for that object in plan
  order. Two findings deferred to one existing issue are one K6 effect with
  two markers. They are never two effects composed over the same body,
  where the second write would drop the first marker.
- **The base** is the target's body as read in the precondition read
  before the plan is persisted (D4.4). Markers already in it, from earlier
  rounds, stay in it.
- **The payload** is the base, a fixed separator that #160 defines, and
  the block. The record holds the full payload, the block, and a SHA-256
  digest of the base. A payload over the target's body limit (D2.4) is
  refused before the plan is persisted, as `BLOCKED` naming the object.
- **Precondition:** the target's body equals the base, by digest, and
  carries none of the block's markers. The kind adds its own conditions
  (K3, K6).
- **Completion:** the body equals the payload, byte for byte.

Reconciliation reads the body and decides as follows. It refines W2–W5 for
these two kinds:

| Record | Body read now | Decision |
|---|---|---|
| `intended` or `attempted` | equals the payload | `observed` |
| `intended` or `attempted` | equals the base, attempt bound not exhausted | save `attempted` with the count incremented, then issue the persisted payload |
| `intended` | equals neither; the target still meets the kind's other conditions and carries none of the block's markers | **rebase**, then issue (below) |
| any | anything else | `conflict`, naming the object |

- **A rebase** is one atomic save. It replaces the payload with the body
  just read followed by the persisted separator and block, replaces the
  base digest with that body's, and moves the record to `attempted` with its
  count incremented. The new payload is issued only after that save.
- **A rebase is allowed only from `intended`.** No write has been issued
  then (D4.1), so the record still describes everything sent, which is
  nothing. After the save, the journal holds the exact bytes of the one
  write that follows, and a crash at any point reconciles against them.
- **From `attempted`, a body that equals neither the payload nor the base
  is a conflict.** The controller cannot tell a write that landed and was
  then edited from one that never landed, and it does not guess.
- The rebase consumes an attempt, so the bound of D4.3 also bounds rebases.

*Rationale.* The eight kinds are exactly the agent writes that must move;
the inventory has no others. A closed set keeps the effect layer from
becoming a general write channel. Dropping step 6 and in-place correction
removes two places where agent text could be republished under the
operator's identity.

For body appends (D5.5):

- **The journal must hold every byte sent.** Recovery compares the target
  against the bytes actually issued, so a composition over a new base is
  checkpointed before it is issued, and never reconstructed afterwards.
- **A rebase only from `intended`** is the one case where the controller
  knows that nothing was sent. It turns a benign human edit made while the
  run was stopped into a normal write instead of a block.
- **One effect per target** removes ordering between effects. Each write
  replaces the whole body, so two effects on one body would either drop a
  marker or make one effect's base depend on another's completion.

*Rejected.*

- **A generic "GitHub write" kind** carrying a method, a path and a body.
  That is a command language.
- **Making the step-6 reply a ninth kind.** It widens the published surface
  for text nobody consumes.
- **Making in-place correction a kind.** It would need its own precondition
  over the controller's earlier write and its own conflict rules, for a
  case that rendering from persisted bytes removes.
- **Migrating the existing writes now.** That is churn with no new safety,
  and #160's non-goals exclude it.
- **Re-reading the body and appending at write time without a checkpoint.**
  The published bytes would then differ from the journal, and a crash after
  the write could not be reconciled.
- **Freezing the payload and blocking on any body change.** It blocks a run
  on a benign edit made while no write was in flight, which is the case a
  rebase handles safely.
- **One effect per finding, chained on one issue.** Each effect's base
  would be its predecessor's payload. That adds ordering and a dependency
  between records for no gain over one effect per target.

### 2.6 The Git publication layer (item 6)

**D6.1 (#160).** A push is a kind (K1) in a narrower **Git publication
layer**. It is not a GitHub API effect. It shares the effect records,
stages, bound and reconciliation, but it has its own transport (§2.7) and
its own rules:

- an exact SHA to an exact controller-derived ref (K1);
- compare-and-swap against the expected old value, either "absent" or the
  last observed head, through the push lease;
- never the default branch;
- never a non-fast-forward in Wave 1;
- ancestry checked against revisions the controller fetched.

**D6.2 (#160).** **The lease is not the fast-forward check.** A lease that
holds still permits a non-fast-forward update. The controller checks locally,
before the push, that the candidate descends from the expected old value.
It refuses the push otherwise, and the refusal names both SHAs.

**D6.3 (#160).** **There is no force, no delete and no history rewrite.** No
kind deletes or rewinds a ref.

**D6.4 (#160).** **Fetched revisions live as objects only.** The controller
fetches into the shared object store and creates or moves no local ref:
the `premerge.py` precedent, now through the hardened transport. The
authority for a fetched revision is the SHA the controller recorded in its
own state, read as that SHA's own bytes (D7.5).

- An object that is missing when the controller needs it (pruned by
  `git gc`, for example) is fetched again immediately before the check.
- A check that still cannot find it fails closed. It is never read as "not
  an ancestor, so allowed".

Each phase child fetches, before launch, the revisions its prompt names:

- #161: the default branch, and the remote branch head if one exists;
- #162: the bound HEAD and merge base;
- #163: the reviewed HEAD;
- #164: the verified default branch.

*Rationale.* A push differs from an API write. Its identity is a ref value,
its precondition is a lease, and its safety depends on ancestry, which the
API layer cannot check. Objects-only fetches keep AutoForge from owning any
ref lifecycle that the operator would have to clean up (AGENTS.md "Runtime
model"). They also compose with Wave 2A's strict object import (#168),
because nothing ever trusts a ref.

*Rejected.*

- **One layer for API writes and pushes.** Pushes need ancestry and lease
  semantics that API writes do not have.
- **A controller-owned ref namespace** (`refs/autoforge/…`). In Wave 1 the
  shared refs are agent-writable, so such refs could never be trusted, and
  they add a lifecycle the operator would see.
- **Allowing non-fast-forward pushes with a lease.** That is a history
  rewrite, which no Wave 1 phase needs.

### 2.7 Controller-side git hardening (item 7)

**D7.1 (#160).** Every controller network git operation, fetch and push
alike, runs in a **controller-owned git context**. No repository state the
agent can write steers or runs anything. The context has these parts:

- **Its own git directory.** A minimal git directory that the controller
  creates for the one operation and removes after it. It has no hooks and a
  configuration the controller wrote, which disables automatic gc and
  maintenance. `GIT_OBJECT_DIRECTORY` points at the shared object store, so
  fetched objects land where the worktrees read them and candidates are read
  from there.
- **No other configuration.** Repository, worktree, global and system
  configuration are not read: `GIT_CONFIG_NOSYSTEM`, an empty global
  configuration, and no `GIT_CONFIG_*` taken from the operator's
  environment. Hooks are additionally disabled at command-line precedence.
- **An explicit environment.** The child gets only the controller's
  standard proxy and CA variables, the locale and `PATH`, never the
  operator's whole environment.
- **An explicit HTTPS URL,** built from the verified repository identity and
  the host the controller's `gh` is authenticated to. `origin`, `pushurl`,
  `insteadOf` and SSH settings are never consulted.
- **The credential from the controller's own authenticated `gh`.** It is
  supplied through a mechanism #160 verifies against the installed `gh` and
  `git`, for example `gh`'s git credential helper named at command-line
  precedence. The credential never appears in argv, a URL, the environment
  of any other process, a log or state.

**D7.2 (#160).** **The rule applies to the existing pre-merge fetch.**
`fetch_pr_head` moves onto the same context. It currently takes `origin` and
its transport settings from the shared configuration, so a planted `pushurl`,
`insteadOf` rewrite or `core.sshCommand` could steer it. A network fetch can
also trigger automatic maintenance and run a planted `pre-auto-gc` hook with
the controller's environment.

**D7.3 (#160).** **Every other controller git process** runs through the
executor (ADR 0002 unchanged) with:

- hooks and the file-system monitor disabled at command-line precedence;
- an environment that carries no GitHub or git credential.

This covers `rev-parse`, `merge-base`, the one-time `worktree add`, and the
`read-tree` / `checkout-index` export. A program that repository
configuration can still make such a process run, such as a filter driver
during the export, therefore runs with no more authority than the agent
itself. The export's byte fidelity under filters is #171's (Wave 2B) and is
not decided here. Every one of these processes that reads an object by SHA
also follows D7.5.

**D7.4 (#160).** The controller's `gh` invocations name the repository
explicitly (`--repo`, or an API path) and never resolve it from a git
remote. `gh` is invoked with no working directory inside a worktree.

**D7.5 (#160).** **A SHA reads as its own bytes.** Every controller git
process that reads an object by SHA reads the object stored under that
SHA, never a substitute that agent-writable state supplies. The rule
covers:

- object existence checks (today's `commit_is_local`);
- K1's ancestry and fast-forward checks (D6.2) and its published range and
  commit messages (D8.5);
- every tree the controller derives from a commit, the pre-merge export
  (D7.2) and the one-time `worktree add` checkout;
- the network fetch and push of D7.1.

Reads of a ref, such as the worktree's `HEAD` for the candidate or the
operator checkout's `HEAD` and branch for the anchor check, read no object
and are unchanged.

**No substitution.** Git honours four kinds of substitution state. In Wave 1
an agent can write all four, and each changes what a SHA reads as without
changing the SHA:

| State | Where | What it substitutes | Excluded by |
|---|---|---|---|
| replacement refs (`refs/replace/*`) | common dir | a whole object: parents, message or tree | `--no-replace-objects` and `GIT_NO_REPLACE_OBJECTS=1` |
| the graft file (`info/grafts`) | common dir | a commit's parents | a controller-owned git directory, which has none |
| the shallow file (`shallow`) | common dir | a commit's parents, cut off | a controller-owned git directory, which has none |
| the commit-graph (`info/commit-graph`, `info/commit-graphs/`) | shared object store | parents and root tree, read without the commit | `core.commitGraph=false` at command-line precedence |

No one switch is enough. Measured with git 2.55.0 in an isolated
repository: `--no-replace-objects` still honours a graft file and a forged
commit-graph, and a private git directory still reads a forged
commit-graph from the shared object store. So the rule needs all three
mechanisms:

- **Every such process** runs with replacement objects disabled and the
  commit-graph off, both at command-line precedence.
- **Every process that reads parents** runs in a per-operation
  controller-owned git directory, as D7.1 defines it, with
  `GIT_OBJECT_DIRECTORY` pointing at the shared store. That directory has
  no refs and no graft or shallow file. These processes are the ancestry
  checks and the range enumeration, plus the network fetch and push, which
  run there already (D7.1). The network operations hold the credential and
  the local reads do not.
- **Tree reads** (the pre-merge export and `worktree add`) keep the
  repository they use today. Graft and shallow state change parents only,
  never a tree. The configuration-driven conversions of the export are
  #171's (D7.3).

**Verified bytes for every decision.** Git does not re-hash every object it
reads, and in Wave 1 an agent can rewrite the object store's files
directly. A decision therefore reads only commits whose ids the controller
recomputes from the bytes git returned, in the object format of the
recorded SHA (40 or 64 hexadecimal digits), never in a format read from
agent-writable configuration.

- **Ancestry.** "X descends from Y" holds only when a chain of parent links
  leads from X to Y through commits that were re-hashed against their ids.
  The chain is sought among the commits of the range Y..X that git
  enumerates under the rule above. #160 fixes a bound on that range. A
  range over the bound, an id mismatch, or no chain fails closed and is
  never read as "allowed".
- **Messages.** D8.5 reads each commit message from the same re-hashed
  bytes.
- **Trees.** A tree id the controller derives from a commit comes from that
  commit's re-hashed bytes. The bytes of the trees and blobs the pre-merge
  export writes are proven by #171's hash-back (Wave 2B), which already uses
  §2.7's hardening; until it lands, Wave 1 guarantees the export no
  substitution, not byte integrity.

Beyond these reads, the integrity of the shared store is not a Wave 1
guarantee (§2.11). GitHub re-hashes every object a push sends, and a whole
store the agent cannot write is Wave 2A's private object directory and
strict import (#166, #168).

*Rationale.* Neutralising the shared configuration key by key is a deny
list: every key that is missed is a hole, and new git versions add keys
(`include.path`, `includeIf`, `url.*.insteadOf`, `http.*`, `core.sshCommand`,
credential helpers). A private git directory with no global or system
configuration is an allow list by construction. Creating it for each
operation leaves no persistent private state to drift or to plant into.

D7.5 has two layers, because hardening configuration does not cover object
interpretation:

- **Re-hashing makes each decision a proof.** An ancestry chain and a
  commit message are read from bytes that hash to the ids the controller
  holds. A substituted or rewritten commit fails the comparison, whichever
  mechanism supplied it.
- **No substitution keeps the rest honest.** Which commits the range holds,
  and every tree read, are git's answers, and no re-hash checks them. The
  substitution switches are what keep those answers about the named SHAs.

The re-hash is cheap: the commits it reads are the published range, which
the push sends anyway.

*Rejected.*

- **`-c` overrides on the shared configuration.** That is a deny list
  (above).
- **A persistent private git directory with a drift check.** It has a
  window between the check and the use, and it is state to maintain.
- **Reusing `origin` and SSH transport.** Both honour `~/.ssh/config`, the
  SSH agent and repository configuration.
- **Exempting the pre-merge fetch** because it predates the effect layer.
  It has the same exposure.
- **`--no-replace-objects` alone.** A graft file and a forged commit-graph
  still change parents (D7.5).
- **Blocking when substitution state exists.** The operator may keep
  replacement refs or grafts of their own, and git's maintenance writes a
  commit-graph by itself. The controller must ignore that state, not
  refuse it, and a check followed by a use leaves a window.
- **Trusting git to verify object ids.** Git re-hashes only some of the
  objects it reads, and which ones depends on the command and its version.

### 2.8 `CONTROL_RESULT` evolution (item 8)

**D8.1 (#160).** **Requested content is bounded data.** Titles, bodies,
prose sections, structured findings, follow-up text and progress text are
rendered into controller templates. Fields that name targets the controller
can read for itself are either removed or kept as cross-checks only:

- PR, comment and replacement URLs;
- branch names;
- the replacement and new-head SHAs.

The cross-check form is the issue URL, the candidate SHA and `round`. A
cross-check that disagrees is a correction, never a retarget. Each phase
child changes its own phase's schema and bumps the prompt version in its
own PR.

**D8.2 (#160).** **Controller marker openers are refused** in every agent
text field: both `<!-- ai-…` (`CONTROLLER_MARKER_OPEN_RE`, extended where
needed) and `<!-- autoforge-…` (the replan markers). Today's opener pattern
covers only the `ai-` prefix.

**D8.3 (#160).** **The published-content policy, applied at validation:**

- **Credential-shaped strings: refuse, never redact.** A field is refused
  when the run-log redactor (`redaction.py`) would change it. The agent is
  asked through the existing correction path, bounded by
  `max_correction_attempts`. A refusal message names the field and the
  pattern class, never the matched text. Because accepted text is exactly
  text the redactor leaves unchanged, the persisted payload, the published
  body and the logs are byte-identical, and redaction stays defence in
  depth.
- **Closing keywords: refuse.** A closing keyword (`close`, `fix` or
  `resolve`, in any form and case) followed by an issue reference is
  refused in agent text. The reference may be `#n`, `owner/repo#n` or an
  issue URL. The controller renders `Closes #n` for the run's own issue
  itself, in the kinds that need it (K2, K3, K7).
- **`@`-mentions: refuse.** An `@` that starts a GitHub mention (preceded
  by a line start, whitespace or punctuation, and followed by a name
  character) outside a code span or fenced block is refused. The correction
  asks the agent to put such tokens in code spans.

**D8.4 (#160).** **A phase persists the rendered payload.** It does not
re-render on recovery. The exact bytes to publish are stored in the effect
record (D2.2). A write that landed while the state save did not then
completes without relaunching the agent, and the read-back compares against
the stored bytes. A body append (K3, K6) persists its rendered block once.
If the body it was composed over changes before any write is issued, the
controller composes the persisted block onto the new body and checkpoints
that payload before issuing it (D5.5). Nothing is re-rendered from a
template, and every byte issued is in the record first.

**D8.5 (#160).** **Commit messages are published content too.** K1 refuses a
candidate when a commit message in the published range does either of
these:

- names any issue other than the run's own with a closing keyword. On
  merge, GitHub closes issues named that way in commits;
- contains a credential-shaped string.

The agent rewrites its local commits through the correction path. The
committed tree is the work product: it is not filtered, and `REVIEW` and the
merge gate own it.

**D8.6 (#162).** **The review comment's total size is checked on the rendered
body.** Today's per-finding bounds times `MAX_FINDINGS_PER_REVIEW` (about
127,000 characters) already exceed the 65,536-character comment limit. A
result whose rendered comment would not fit is refused through the
correction path, and the per-finding bounds are unchanged. Each phase child
bounds its own text fields so that its rendered body fits.

*Rationale.*

- **Refusing beats rewriting.** A refusal never silently changes what the
  operator's identity publishes, and an imprecise refusal fails closed (a
  correction), while an imprecise rewrite publishes.
- **Why mentions are refused.** Mentions and closing keywords are external
  effects in their own right: notifications, and issue state changes on
  merge. A prompt-injected agent must not trigger them.
- **Why persist rather than re-render.** Persisted bytes survive an upgrade
  that changes a template between the intent and the write. Re-rendering
  byte-identically would tie recovery to template and prompt versions.

*Rejected.*

- **Redacting credential-shaped strings.** That publishes altered text
  under the operator's name, and it makes the persisted payload differ from
  the agent's text.
- **Neutralising mentions** with zero-width characters or code spans. That
  is silent alteration, and a Markdown approximation that misses a case
  publishes the mention.
- **Allowing closing keywords for the run's own issue in agent text.** The
  controller renders that link itself, and one rule for all references is
  simpler to check.
- **Re-rendering on recovery.** It breaks across template changes (above).

### 2.9 Unjournaled marker-bearing objects (item 9)

**D9.1 (#160).** Whenever a phase reads one of its identities, at entry or
in the precondition read before the intents, every object found falls into
one of three classes:

- **Journaled:** an effect record of this phase accounts for it. It is
  reconciled under §2.4.
- **Pre-existing:** recorded in the entry observation, that is, observed
  before this phase's first launch. It is handled by the per-kind rule below.
- **Unexplained:** neither. This covers an object an agent published with a
  credential it still held, a human's object, and a run persisted by an
  older version (§2.13 handles that case first).

**D9.2 (#160).** **An unexplained object is `BLOCKED`.** The reason names
its URL, its identity and this rule. It is never adopted and never
duplicated. The operator has two ways out:

- **Remove the object, or its marker, and run `resume`.**
- **Run `unblock`.** The controller re-inspects GitHub, the decision is
  recorded in `unblock_history`, and the next entry runs with `attempt == 0`.
  The object is then pre-existing, and the per-kind rule decides. For
  kinds whose pre-existing rule refuses, only removal helps.

**D9.3 (#160).** **Authorship is not proof.** The agent and the controller
share the operator's GitHub login, so no object is adopted, or refused, on
its author. Only the effect records and the entry observation prove origin.

**The per-kind pre-existing rules,** one owner each:

- **D9.4 (#161) `ANALYZE_EXECUTE`.**
  - An open marker-bearing PR is recovered at entry by the existing
    `_try_recover_pr` rule, without a launch. The rule requires an open PR
    in this repository with exactly one marker, and the step reports it.
    `REVIEW` then binds and reviews every byte, so adopting it grants
    nothing.
  - An unmarked open PR in this repository on the derived branch is adopted
    through K1 and K3 after the agent returns.
  - A fork-head PR is never adopted.
  - A closed or merged PR on the branch is `BLOCKED`.
  - A remote branch with no PR is prior work: its head becomes K1's expected
    old value, and it is fetched so that the agent continues from it.
- **D9.5 (#163) `FIX`.** Marker-bearing follow-ups found at entry are handed
  over and are referenceable through K6. They are never republished, as with
  #90's `EXISTING_FOLLOW_UP_ISSUES` today.
- **D9.6 (#162) `REVIEW`.** A round comment for this round at the bound
  revision is refused, even when pre-existing. It cannot be completed without
  the validated result it would need, and adopting it would let any author
  define the round's findings. The operator removes it, or its marker.
- **D9.7 (#160) `UPDATE_EPIC`.** A progress comment for this issue and PR is
  refused unless it is journaled, or §2.13 applies.
- **D9.8 (#164) `REPLAN_REEXECUTE`.** A PR carrying this transaction's marker,
  or a branch with the derived replacement name, that the records do not
  account for is refused through the existing provenance rejections. The
  transaction id is random and new at `PREPARED`, so only §2.13 can produce
  a legitimate one.

*Rationale.* Agent-side publication must never be adopted silently. A
detected leak, before #165 or after a credential slips through, then stops
the run instead of being rewarded. A PR is the one object that may still be
recovered on sight: its authority comes from the later review, not from its
existence. Review comments carry authority, because the merge gate re-reads
the last one (#94), so they are never adopted.

*Rejected.*

- **Adopting a marker-bearing object when its author is the operator's
  login.** Agents use that login too.
- **Adopting on byte equality with what the controller would have
  rendered.** The template is public, so an agent could match it.
- **Deleting unexplained objects.** That is a destructive write with no kind,
  on an object the controller does not own.

### 2.10 The agent read path (item 10)

**D10.1 (#165).** After credential removal, agents read through
**controller-supplied read context** only. No separately configured
read-only credential is used.

**D10.2 (#165).** **What #165 implements:**

- **Collection.** The controller reads, through its typed GitHub client,
  everything each phase prompt currently tells the agent to read with `gh`:
  - the issue title, body and comments;
  - the PR title, body and comments;
  - the check summary for the bound HEAD (name, status, conclusion);
  - earlier review comments the prompt points at;
  - the follow-up issues handed over;
  - for `UPDATE_EPIC`, the EPIC body and the state and title of each issue
    it references;
  - for `REPLAN_REEXECUTE`, the historical evidence its prompt's §6 and §7
    name: the source PR, its review comments, the issue and the EPIC.
- **Delivery.** Identity values stay inline in the prompt, as today. Bulky
  items go into a per-launch directory the controller writes before the
  launch. It sits under the controller's state directory, outside the issue
  worktree and outside the state file, and is written through the
  controller's filesystem boundary. The prompt names it by absolute path.
  The controller never reads it back as authority.
- **Bounds and redaction.** Every item and the total are bounded.
  Truncation is explicit, with an omission marker that states the counts,
  and never silent. Everything is redacted with the run-log redactor.
- **The diff** is agent-local: `git diff` over the bound HEAD and merge base
  the controller fetched (D6.4).
- **Trust.** The supplied context is project data. The trust-boundary
  rules of `prompts/common.md` apply to it as quoted evidence.
- **Prompts.** They stop instructing `gh` reads (the read rows of D1.2).
  Until #165 lands, agents keep their `gh` reads.

Git revisions are not part of this decision. #161–#164 fetch them before
launch (D6.4).

*Rationale.* Supplied context needs no credential at all, so no
misconfiguration can restore write authority through it. It is also bounded
and redacted, and it is the same for every provider. Files rather than argv
avoid prompt-length limits. A per-launch directory outside the worktree
keeps the reviewed tree clean, and Wave 2A can project it read-only.

*Rejected.*

- **A read-only credential.** AutoForge cannot establish that a token is
  read-only. Fine-grained token permissions are not reliably introspectable,
  and a misconfigured token silently restores write. A read token also
  widens what a prompt-injected agent can read beyond the phase's needs.
- **Supplying context inside the worktree.** It would enter the reviewed
  tree, and could be committed.
- **Replacing every read with tool calls brokered by the controller.** That
  is an interaction protocol, owned by #126 / #148, and not needed for a
  fixed per-phase read set.

### 2.11 Supported threat model (item 11)

**D11.1 (#165).** #165 records the threat model in
`docs/agent-guides/github-safety.md`, beside the classification.

**Actors.**

- **Trusted:** the operator and the controller.
- **Untrusted:**
  - the agent: its output, and anything a prompt injection in project data
    makes it do;
  - repository-defined validation and pre-merge commands, which are the
    PR's code;
  - humans acting concurrently on GitHub.
- **GitHub** is the source of truth.

**What Wave 1 guarantees** (once #165 lands):

1. **No delegated authority.** Agents and repository-defined commands
   launched by AutoForge receive no GitHub write authority through anything
   AutoForge controls:
   - the environment;
   - `gh` or git configuration (global, system, repository or worktree);
   - credential helpers and askpass programs;
   - the SSH agent;
   - provider integrations that AutoForge configures.
2. **No steering of the controller.** The controller's own git network
   operations and `gh` invocations cannot be steered, and cannot be made to
   run code, by repository state the agent can write. That state covers
   repository and worktree configuration, hooks, `origin`, and include
   directives. The controller's local git processes hold no credential
   (D7.3). Nor can that state change what a SHA the controller reads means:
   replacement refs, grafts, shallow boundaries and the commit-graph are
   ignored, and every commit a decision reads is re-hashed (D7.5).
3. **Every write is accounted for.** Every Wave 1 externally visible write
   is a typed controller effect with a durable record. Anything else that
   appears with a Wave 1 identity is detected and blocks; it is never
   adopted.
4. **Agent text is bounded data.** Agent text is published only inside
   controller framing. It cannot:
   - forge a controller marker;
   - close or link an issue;
   - mention a user or team;
   - carry a credential-shaped string.

**What Wave 1 does not guarantee** (the outer-sandbox wave, Wave 2A, owns
the first four):

- **Absolute-path reads.** A same-UID process that deliberately reads
  credential files, keychains or sockets by absolute path. Examples are
  `~/.config/gh/hosts.yml`, `~/.git-credentials`, `~/.ssh/`, and a keyring
  over the session bus.
- **Absolute-path writes.** A same-UID process that deliberately modifies
  the operator's own `gh` or git configuration by absolute path. The
  controller's own `gh` reads that configuration. After #165, an agent's
  ordinary `gh` and `git config --global` writes land in controller-supplied
  locations, not the operator's.
- **Shared local repository state.** An agent can still create, move or
  delete shared refs and edit the configuration and hooks of the common dir,
  which the operator's checkout shares. D1.1 forbids it by instruction only;
  §2.7, D6.4, D7.3 and D7.5 bound what that state can do to the controller.
- **Network egress.** An agent can send whatever it can read.
- **Exposure outside the boundary.** Credentials the operator exposes
  outside AutoForge's controlled execution boundary, for example a token in
  a worktree file or in a service the agent can reach.
- **The committed tree.** Its content is not filtered; `REVIEW` and the
  merge gate own it.
- **Integrity of the shared object store.** An agent can rewrite the
  store's files. D7.5 re-hashes the commits behind every K1 decision, and
  GitHub re-hashes every object a push sends. Which commits git enumerates
  in a range, and the bytes of the trees and blobs the pre-merge export
  writes, still come from the store; #171's hash-back covers the export in
  Wave 2B, and a store the agent cannot write is Wave 2A's (#166, #168).

**Tool permission is not the boundary.** Provider permission modes and
approvals are defence in depth at most (D1.3).

*Rationale.* Wave 1 can enforce only what AutoForge hands a child (logical
isolation). What a child can *reach* by trying is a filesystem and process
boundary (outer sandbox), and promising it here would be a claim no code
backs.

*Rejected.* **Claiming protection from same-UID reads by hiding paths in
the child environment.** An absolute path defeats that.

### 2.12 Module ownership (item 12)

Placement follows `docs/agent-guides/architecture.md`:

- no provider semantics in the effect layer;
- no GitHub effect semantics in providers;
- no CLI flags outside `providers.py`;
- no workflow semantics in `executor.py`;
- no raw `gh --json` outside `github.py`.

This record names modules, not classes or functions.

- **D12.1 (#160) Effect records and pure verifiers** live in a new module in
  the engine layer. It holds:
  - the record type and its validation on load;
  - the stage rules;
  - reconciliation decisions as pure functions of (record, observed remote
    values).

  It does no I/O and knows no provider.
- **D12.2 (#160) Typed GitHub writes** live in `github.py`:
  - one typed method per GitHub kind;
  - create methods outside the transient-retry loop (D4.3);
  - typed results;
  - no generic passthrough of `gh` arguments.

  The module docstring's "the controller only *verifies*" changes with it.
- **D12.3 (#160) The controller git transport** lives in a new module beside
  `premerge.py`. It owns:
  - the per-operation git context (D7.1);
  - the credential plumbing;
  - lease and ancestry checks;
  - typed results.

  Every process runs through `executor.py`. `premerge.py`'s fetch moves onto
  it.
- **D12.4 (#160) Rendering.**
  - Markers stay in `claims.py`, with unchanged schemas.
  - Published bodies come from template files rendered by the strict prompt
    renderer, where a missing variable fails. The templates live in a
    subdirectory of `src/autoforge/prompts/`, kept apart from agent prompts.
  - #160 creates the directory and the progress-comment template. Each phase
    child adds its own template as part of its wiring.
- **D12.5 (#160) The content policy** (D8.2–D8.3) lives in `result_parser.py`
  and reuses `redaction.py`'s patterns. #160 implements it for its first
  consumer, and each phase child applies it to its own fields.
- **Phase wiring** belongs to `engine.py`, with `replan_txn.py` for the
  replan side. The wiring is:
  - pre-launch fetches;
  - the entry observation;
  - the effect plan;
  - the read-back;
  - dry-run plan text.

  This is not one decision with several owners. Each phase's wiring is
  owned by that phase's child alone, as the "K wiring" entries of §5 list
  it: #161 (`ANALYZE_EXECUTE`), #162 (`REVIEW`), #163 (`FIX`),
  #164 (`REPLAN_REEXECUTE`) and #160 (`UPDATE_EPIC`).
- **D12.6 (#165) Read-context assembly** lives in `engine.py`. It reads
  through `github.py`'s typed reads and writes through the controller's
  filesystem boundary.

**D12.7 (#164)** Replan effects are ordinary effect records that carry the
transaction id. The replan journal gains no stage, and only the replan stage
machine advances that phase. The replacement branch name is a pure function
of persisted values, so it needs no new field.

*Rationale.* Each boundary already exists in `architecture.md`, and this
placement only extends it. Reconciliation decisions are pure, so every crash
window can be tested without a process or a network. The GitHub client
stays the one place that speaks `gh`, and the executor stays the one place
that runs a process. Published bodies follow the prompt convention, so a
missing value fails instead of publishing an empty field. No effect module
imports a provider, so an SDK provider inherits the layer unchanged.

*Rejected.*

- **Effect logic inside `github.py`.** That mixes workflow semantics into
  the client.
- **Git transport inside `executor.py`.** That puts workflow semantics in
  the executor.
- **Bodies as Python string literals.** The repository convention forbids
  large prompt text in code.

### 2.13 Upgrade across the boundary (item 13)

**D13.1 (#160).** **One protocol bump, made alone by #160.** It is numbered
at merge time and never takes the 5 → 6 that #23 reserves for #126 (with
#134 riding it). Whichever bump lands second rebases its migration rule onto
the other, and the number is settled on #23 first. A field that a phase
child finds it needs follows the EPIC's rule:

- while #160 is open, it joins #160's bump;
- afterwards, it becomes its own bump, announced on #158 and #23.

**D13.2 (#160).** **The migration rule.** A file of the previous protocol
loads with three settings:

- no effect records;
- no entry observation;
- if `attempt >= 1`, its in-flight launch labelled as launched under the
  *agent-publishing* contract.

That is exactly what the previous version meant, so nothing is inferred
from GitHub and nothing is rebound. LOCAL state that carries effect records
or an entry observation is refused as corrupt.

**D13.3 (#160).** **The launch label and the one-shot legacy re-entry.**

- **The label.** Every launch in a publishing phase records, in its
  pre-launch save, which contract it runs under: the agent publishes, or
  the controller does.
- **The re-entry.** A step that resumes a phase with `attempt >= 1`, whose
  label is agent-publishing while the running version publishes that phase
  itself, performs one **legacy re-entry**. The objects the old contract
  authorised that phase's agent to publish are classified pre-existing
  instead of unexplained, and the per-phase outcome below applies. The next
  pre-launch save records the new label, so the re-entry happens once.
- **Without a launch.** With `attempt == 0`, the entry is normal.

The label is what keeps the rule correct across the intermediate versions
between #160 and the last of #161–#164. A run persisted while a phase was
still agent-published resumes correctly under a version where it no longer
is.

**The per-phase legacy outcomes:**

- **D13.4 (#161) `ANALYZE_EXECUTE`.**
  - An open marker-bearing PR is recovered by `_try_recover_pr`.
  - An unmarked open PR on the derived branch is adopted (K1 and K3).
  - A remote branch with no PR is prior work.
- **D13.5 (#162) `REVIEW`.** A legacy round comment at the bound revision is
  `BLOCKED`, naming it: the validated result it would need does not exist.
  The operator deletes the comment, or its marker, and resumes, and the
  round re-runs. Without such a comment the launch is normal.
- **D13.6 (#163) `FIX`.**
  - A PR head past the reviewed HEAD takes the existing drift route
    (`FIX -> REVIEW`), so the old agent's push is reviewed.
  - Marker-bearing follow-ups are pre-existing, handed over and
    referenceable.
- **D13.7 (#160) `UPDATE_EPIC`.**
  - Exactly one legacy progress comment is adopted as the phase's observed
    progress comment, named in the step message and the run log.
  - The agent is relaunched under the new prompt for the roadmap section and
    the next issue only. Its progress text is not published.
  - Two comments: the existing refusal.
- **D13.8 (#164) `REPLAN_REEXECUTE` at `PREPARED`.**
  - A replacement found by the transaction marker above the watermark is
    bound under the existing predicates without the candidate-HEAD equality,
    exactly as today (`require_checkpoint_head=False`). No recorded
    candidate exists, and the replacement is reviewed from scratch after
    activation anyway.
  - With no replacement found, the new path runs.
  - A transaction past `VERIFIED` is unaffected.

*Rationale.*

- **Why the per-phase outcomes.** Under the old contract, the agent's write
  was legitimate. Calling it "unexplained" after an upgrade would block runs
  for having done what the previous version asked. The outcomes still give
  authority only where it is re-established: `REVIEW` re-reviews an adopted
  PR, a drifted head is reviewed, and a legacy review comment is refused
  because nothing can re-establish its findings.
- **Why a recorded label.** The label keeps the rule tied to a recorded
  fact, never to the shape of the file (`state-and-recovery.md`).

*Rejected.*

- **Refusing every legacy file mid-phase.** That needlessly strands runs at
  `ANALYZE_EXECUTE`, `FIX` and `UPDATE_EPIC`, where re-entry is safe.
- **Inferring the contract from the absence of records.** That reads the
  shape of the file, and it fails for the intermediate versions.
- **Relaunching the reviewer to adopt a legacy comment.** The comment's
  findings and the new result could disagree, and the marker's cardinality
  forbids a second comment.

## 3. What does not change

- **`CONTROL_RESULT` stays untrusted, and read-back stays authoritative.** The
  controller's own writes are read back too.
- **`MERGE` stays controller-owned,** behind `safety.allow_merge`, with its
  merge gate, merge counting and disable-auto-merge mechanism.
- **The PR, HEAD, base and merge-base binding** of `REVIEW`, stale-round
  handling, carry-forward and the merge gate's re-read of the last review
  comment (#94).
- **LOCAL makes zero `gh` calls,** has no effect machinery, and keeps its
  prompts and run contract.
- **Dry-run makes zero writes:** no effect and no network git.
- **ADR 0002:** every controller git process, network operations included,
  runs through the executor, and nothing it starts outlives it.
- **The event journal (`events.jsonl`) stays audit-only.** An effect
  transition may append a line, but no recovery path reads it.
- **State and logs stay bounded and redacted.** Effect records are bounded
  (D2.4). The published payload is redaction-invariant (D8.3), and record
  errors are redacted within `MAX_GROWTH_FACTOR`.
- **Marker schemas and cardinality** (`at_most_one` at entry, `exactly_one`
  on read-back), review-round routing, loop bounds, stagnation and the replan
  policy.
- **The replan transaction's lifecycle,** close, receipt, compensation and
  activation.
- **The controller's own credentials:** its `gh` and git network calls keep
  the operator's authority (§2.7).

## 4. Composition with other decisions

- **ADR 0001 (LOCAL workspace boundary).** Unchanged. LOCAL has no effects,
  no entry observation and no read context, and its state is refused if it
  carries them. LOCAL agents receive #165's environment policy, which the
  LOCAL run contract does not record, so the contract is unchanged.
- **ADR 0002 (executor).** Unchanged, and relied on. Controller git runs
  through the executor. The entry observation (D4.4) is a sound launch fence
  only because nothing an agent starts outlives its invocation: after the
  agent returns, no process of its own can publish.
- **ADR 0003 (Pi).** Unchanged. The rules are provider-neutral. A Pi
  `provider_failure` is a provider failure like any other, and no effect is
  planned from it. Pi's tool policy is not a credential boundary. #165 edits
  only the credentials paragraph of `docs/pi-policy.md` §8.
- **#126 / #134 (interaction records).** **Effect records are not interaction
  records.**
  - They are separate durable records with separate lifecycles.
  - A pending interaction happens inside the agent run, before any effect is
    planned.
  - Effect recovery never reads the interaction journal.
  - An interaction answer reaches a payload only through a validated
    `CONTROL_RESULT`.
  - An approval never creates an intent or widens a kind (D1.3).
- **#147–#149 (SDK).** SDK adapters inherit the boundary with no engine
  branch. #149 owns SDK tool and permission policy, and may map D1.2 into
  deny rules as defence in depth.
- **Wave 2A (#166–#169).** #166 decides the outer sandbox's git surface. A
  read-only common dir, a private object directory and a strict import
  compose with this record. Nothing here depends on an agent-written shared
  ref, and the only shared state an agent's work writes is the object store
  (D1.1), which a private object directory and a strict import take over.
  Until then, D7.5 bounds what that store can do to the controller. D1.1
  states what #166 must keep writable for agents: the worktree's private git
  directory, which lives under the common dir. The controller's own runtime
  directories, the per-operation git contexts and the read-context
  directories are never projected writable to agents.

## 5. Decision ownership

Each decision has exactly one implementing child. D5.4 records that existing
mechanisms are unchanged and has no child.

| Child | Decisions |
|---|---|
| #160 | D2.1–D2.4, D3.1, D4.1–D4.5, D5.1, D5.2, D5.5, D6.1–D6.4, D7.1–D7.5, D8.1–D8.5, D9.1–D9.3, D9.7, D12.1–D12.5, D13.1–D13.3, D13.7; the K8 wiring |
| #161 | D9.4, D13.4; the K1, K2 and K3 wiring; the D1.2 rows it owns |
| #162 | D8.6, D9.6, D13.5; the K4 wiring; the D1.2 rows it owns |
| #163 | D5.3, D9.5, D13.6; the K1, K5 and K6 wiring; the D1.2 rows it owns |
| #164 | D9.8, D12.7, D13.8; the K1 and K7 wiring; the D1.2 rows it owns |
| #165 | D1.1, D1.2 (the guide entry and the rows it owns), D1.3, D10.1, D10.2, D11.1, D12.6 |

## 6. Tests the implementing children must add

### 6.1 Per kind

These tests run at the operation level against `FakeGitHub` and a local bare
repository (#160), and at the phase level by the consumer. Each test
interrupts the step after each persisted stage and resumes.

| Kind | Crash-window tests (W1–W5) | Conflict tests | Owner(s) |
|---|---|---|---|
| K1 push | intent saved, no push; push landed, save lost; timeout landed and not landed; read-back drift | default branch refused; non-fast-forward refused; lease mismatch (a human push); an ancestor missing after the fetch fails closed; a planted `pushurl`, `insteadOf`, `core.sshCommand`, `core.hooksPath`, `pre-push` or `pre-auto-gc` hook neither redirects nor runs; a commit message with another issue's closing keyword refused; planted substitution state or a rewritten commit object passes neither the ancestry check nor the message check (D7.5, below) | #160; #161, #163, #164 |
| K2 implementation PR | create landed, save lost; timeout landed and not landed; duplicate invocation after a restart | a PR on the branch already closed; two marker-bearing PRs; a marker-bearing PR that appeared during the agent run (unexplained) | #160; #161 |
| K3 adopt PR | append landed, save lost; a body edited before the first issue is rebased, checkpointed, then issued; a crash after the rebase save and before the write, and after the write and before the next save, reconciles against the rebased payload with no second write; a body edited after an issue, payload not present, is `BLOCKED` | a fork-head PR refused; a closed PR on the branch; a body that carries the block but has otherwise drifted; a payload over the body limit refused before the plan is persisted | #160; #161 |
| K4 review comment | create landed, save lost (round completes with no relaunch); timeout | a second matching comment; a pre-existing round comment refused; an oversized rendered body refused before any effect; a stale round still gets its comment | #160; #162 |
| K5 follow-up issue | create landed, save lost; timeout | created then closed by a human; two issues for one finding; a human-created marker-bearing issue | #160; #163 |
| K6 marker append | append landed, save lost; a human body edit before the first issue is rebased, checkpointed, then issued; a crash after the rebase save and before the write reconciles against the rebased payload; a body edited after an issue, payload not present, is `BLOCKED`; two findings deferred to one issue are one effect, and both markers survive, across a crash after the write as well; a later round's append keeps every earlier round's marker | a body already equal to the payload (`observed`); a block marker in a body that is not the payload; an issue the controller did not hand over refused; a payload over the body limit refused before the plan is persisted | #160; #163 |
| K7 replacement PR | push landed with no PR; create landed, save lost; no second agent run | a branch with the derived name at another SHA; a human push before binding; a human close before binding; a marker-bearing PR created during the agent run | #160; #164 |
| K8 progress comment | create landed, save lost; timeout | an unjournaled comment refused; two comments block before launch | #160 |

In addition:

- **Object interpretation (#160, D7.5).** Each case plants one of a
  replacement ref, a graft entry, a shallow entry or a forged commit-graph,
  for the candidate, a commit of its range or the base:
  - an unrelated candidate fails the ancestry check, and a non-fast-forward
    candidate fails the fast-forward check;
  - a commit message with another issue's closing keyword is still read
    and refused;
  - the existence check reports a missing commit as missing, even when a
    replacement for it exists;
  - a tree derived from a commit, the pre-merge export and the
    `worktree add` checkout all see the named commit's own tree;
  - a rewritten loose commit object in the range fails closed on its id,
    for both the ancestry and the message check.
- **Transport and dry-run (#160):**
  - a create is never re-sent after a transient failure, while a read is
    retried;
  - the close receipt's presence check is pinned;
  - attempt-bound exhaustion is `BLOCKED`;
  - dry-run under `ExplodingGitHub` performs no effect and no network git.
- **Content policy:** a marker opener (both prefixes), an `@`-mention, a
  closing keyword or a credential-shaped string in any agent text field is
  refused before any effect, and the correction carries no secret.
  - #160 tests the policy itself.
  - Each phase child tests its own fields.
- **State (#160):**
  - records round-trip;
  - corrupt records fail loudly;
  - LOCAL state with records or an entry observation is refused;
  - a previous-protocol file loads under D13.2;
  - each legacy re-entry outcome is tested, by its owner (D13.4–D13.8).

### 6.2 The EPIC #158 test-matrix ownership

The table below is EPIC #158's ownership table, copied verbatim; this
record does not change it. The per-kind tests of §6.1 refine these rows and
assign no owner the table does not.

| Scenario | Owner(s) |
|---|---|
| effect write succeeds, state save crashes | #160; #161 #162 #163 #164 |
| effect intent saved, write never happens | #160; #161 #162 #163 #164 |
| timeout during the write | #160 |
| duplicate invocation after restart | #160; #161 #162 #163 #164 |
| human mutation between intent and effect | #160; #161 #163 #164 |
| remote resource already exists | #160; #161 #162 #163 |
| existing resource has a conflicting identity | #160; #161 #162 #163 |
| duplicate PR / comment / follow-up markers | #161 #162 #163 |
| branch / HEAD drift; external push by a human | #161 #163 #164 |
| provider failure before / after a local commit | #161 #163 #164 |
| malformed `CONTROL_RESULT`; correction path | #161 #162 #163 #164 |
| stale `REVIEW` | #162 |
| `FIX` finding-resolution mapping | #163 |
| `REPLAN` old/new PR transaction | #164 |
| LOCAL zero-GitHub | #160 (no effect machinery), #165 (environment) |
| dry-run performs no external effect | #160; each phase child for its own plan output |
| logs and state bounded and redacted | #160 (records), #165 (environment, doctor) |
| ADR 0002 cleanup unchanged | #160 (controller git through the executor), #165 |
| Claude Code / OpenCode / Pi do not regress | #161 #162 #163 #164 #165 |
| SDK providers consume the same authority policy with no engine branch | #165 |
| an agent-side write attempt fails closed, and a leaked one is detected rather than adopted | #165 (with #161–#164's unjournaled-object rule) |

## 7. Non-goals

- No change to ADR 0001, 0002 or 0003 (§4 states how this record composes
  with each).
- No redesign of `MERGE`, the merge gate, or the replan close and
  compensation.
- No class or function names imposed on the implementation. Only modules
  are placed (§2.12).
- No OS sandbox design beyond naming its boundary (§2.11). That is Wave 2A.
- No SDK-specific policy (#149), and no interaction design (#126, #134,
  #148).
- No production code, tests, prompts or configuration in this change.

## 8. Consequences

- **The source of authority moves.** After Wave 1, an agent's work product
  is a local commit plus a validated `CONTROL_RESULT`. Every externally
  visible write is the controller's, and it is durable, reconciled and
  bounded.
- **Recovery after lost state gets stricter.** Recovery of a review round,
  or a progress comment, whose local state was lost (for example after
  `run --force`) no longer hands the old object to a new agent invocation
  to adopt. It blocks, naming the object, and an operator removes it. This
  is the price of never adopting authority-bearing objects on sight.
- **The `FIX` resolution reply disappears from PRs.**
- **Documentation follows behaviour.** Each child updates the guides its
  change affects. #165 also updates AGENTS.md's "Project" and "Runtime
  model" wording, which still says agents own GitHub content creation.
  If implementation contradicts a decision here, the implementing child
  amends this record, with a recorded rationale.
