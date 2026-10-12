# ADR 0006. Candidate validation evidence and controller review certificates

- **Status:** accepted. This is a design record; nothing in it is
  implemented yet. It is implemented by #171 (the byte-exact REMOTE
  materializer and its hash-back proof, also used by the merge gate's
  export), #172 (validation definitions, execution policy, durable
  `ValidationRecord`s, LOCAL convergence, and the one state migration),
  #173 (the producer gates, the REVIEW entry gate and the REVIEW evidence
  section), #174 (review certificates, entering `READY_FOR_MERGE` and LOCAL
  `DONE`) and #175 (drift, recovery, the operator override, and the
  certificate check in the merge gate's `READY_FOR_MERGE` and `MERGE`
  passes). The number is assigned when the record merges: if another ADR
  merges first, this record takes the next free number, and its index
  entries move with it.
- **Decides:** #170, for EPIC #158 (Wave 2B).
- **Amends:** ADR 0001 §5.10, with a dated note recording the LOCAL
  convergence rule (D7.4). No other ADR's text changes.
- **Narrows:** ADR 0005 D16.1 and the rejected alternative of its D17.1, for
  validation records only (D8.2, D8.3). ADR 0005's text is not edited; this
  record is where the narrowing is stated.
- **Where:** placement is decided per decision and in §5. Existing code this
  record builds on: `src/autoforge/engine.py`, `premerge.py`, `state.py`,
  `config.py`, `run_contract.py`, `local_workspace.py`, `result_parser.py`,
  `replan_txn.py`, `transitions.py`, `effects.py`, `effect_ops.py`,
  `git_transport.py`, `executor.py`, `sandbox.py`, `redaction.py`,
  `safefs.py`, `src/autoforge/prompts/`.
- **Related:** ADR 0001 (LOCAL workspace identity, rule V, §5.10), ADR 0002
  (nothing an agent starts outlives its invocation), ADR 0004 (authority
  boundary, typed effects, controller-side git hardening), ADR 0005 (outer
  sandbox); #94 (the merge gate re-reads the clean review); #155
  (provider parity tests); #23 (state protocol numbering); #126 / #134
  (interaction state).

## 1. Context

### 1.1 What the code does today

#170 was planned against `main` `2fff4b9`. The references below are re-read
on `main` `1412765`, where #160–#165 and #167 have landed and #168 and #169
have not.

**REMOTE validation is an agent claim.**

- `ANALYZE_EXECUTE` and `FIX` agents "run local tests" and report prose.
- The only test-shaped field in a REMOTE `CONTROL_RESULT` is `tests_passed`,
  in `REPLAN_REEXECUTE` (`result_parser.py` `ReplanReexecuteResult` :1332,
  checked :1394–1402). It is consumed by `replan_txn.py`
  (`_attestation_from_payload` :245, `verify_attestation` :1530) and by
  `engine.py` `_replan_claim_mismatch` (:9918), which `_check_replan_result`
  (:9830) calls, so `tests_passed=false` is a rejection decided before any
  effect.
- LOCAL analyze reports `tests_attempted` (`result_parser.py` :1632), which
  is informational.
- Nothing binds a test result to a commit the controller observed.

**REMOTE controller-run verification exists only at the merge gate.**

- `merge.verification_commands` (`config.py` `MergeConfig` :439, parsed
  :1141–1143) runs in `_local_verification_problem` (`engine.py` :5105) and
  `_run_premerge_command` (:5201), inside `_verify_pr_for_merge` (:4840).
- `_verify_pr_for_merge` is shared by the `READY_FOR_MERGE` gate pass
  (`_ready_for_merge_step` :4522) and `MERGE` (`_merge_step` :5384). It runs
  only when the merge gate is open; `READY_FOR_MERGE` is otherwise a stop
  phase.
- A pass is cached as `premerge_verified_head_sha` and
  `premerge_verified_commands` (`state.py` :480–481). A resume at the same
  HEAD with the same command list does not rerun it. Both are cleared by
  `AutoForgeState.reset_for_new_issue` (:976, :1001–1002) when the run moves to
  the next issue.
- The export is `premerge.export_commit_tree` (:169): `git read-tree` into a
  private index (:189) and `git checkout-index -a -f --prefix=` (:191) into
  a `tempfile.mkdtemp(prefix="autoforge-premerge-")` directory (:183). Its
  git processes are hardened (ADR 0004 D7.3, D7.5) but still read
  attributes and run filter drivers (the module docstring says so).

**LOCAL validation is controller-run and in place.**

- `_run_validation_commands` (`engine.py` :3196) runs the run contract's
  `local.validation_commands` (`run_contract.py` :256) from the contract's
  repository root, through the executor with `contain_orphans=True`,
  `execution.command_timeout_seconds` and #165's preflight.
- A non-zero exit or a timeout raises `VerificationError` with a redacted
  2,000-character tail. The phase is then not verified and is relaunched
  within `MAX_LOCAL_PHASE_ATTEMPTS` (`engine.py` :401, value 3), then
  `BLOCKED`.
- `_verified_snapshot` (:3037) binds the *post-validation* fingerprint as
  the one the next REVIEW must see (ADR 0001 rule V and §5.10: write phases
  bind; REVIEW never binds).
- No record of what ran survives except the run logs.

**A REMOTE `VerificationError` has no bound.** `_step_once` (:2519) saves
state and re-raises a `VerificationError` from `_verify_and_apply`
(:2605–2614); one raised by a candidate check inside `_invoke_phase`
(:8147–8151) leaves the step the same way. Either way the phase is
unchanged, with no bound and no `BLOCKED`.

**The review binding and the merge gate.**

- `_bind_review_head` (:6051) binds the PR, its HEAD, base and merge base
  before every round.
- The controller renders and publishes the review comment (#162):
  `_apply_review` (:8728) plans the K4 record and `_complete_review`
  (:8842) drives `ReviewCommentOp` (`effect_ops.py` :518), whose read-back
  compares the marker and the body. The commit save keeps only the comment
  URL, as `last_review_comment_url` (`state.py` :379). The marker key is
  rebuilt from `review_round`, `reviewed_head_sha`, `reviewed_base_ref` and
  `reviewed_merge_base_sha`.
- The merge gate re-reads that comment live (#94, `_clean_review_problem`
  :5029).
- None of these say anything about tests.

**The candidate the producers publish.** `_check_analyze_result` (:3819),
`_check_fix_result` (:3911) and `_check_replan_result` (:9830) read it with
`git -C <worktree>` (`_worktree_git` :3799) inside `_invoke_phase`, so a
refusal is corrected before anything is planned. `_apply_analyze` (:8322)
then persists the effect plan and its completion context in one save
(:8432–8438); `_apply_fix` (:9109) and `_apply_replan` (:9942) do the same
for their phases. ADR 0005 D6.3 and D6.5 (#168) move the read to
`worktrees/<id>/HEAD` through the common dir and add a strict import; on
`main` neither exists yet.

**The outer sandbox exists in the executor layer only.**
`ExecutionRequest.sandbox` (`executor.py`) and `ExecutionResult.sandbox`, a
`SandboxFacts` (`sandbox.py` :277: `enforced`, `backend`, `backend_version`,
`teardown`, `network`), are on `main`; no launch site passes a description,
and there is no `sandbox.mode` key (#168).

**Versions.** State protocol `7` and prompt version `v8`
(`src/autoforge/__init__.py`).

### 1.2 Measured behaviour

All measurements used scratch repositories and fake filter programs only.

- **Planning (#170):** git 2.55.0.
- **While writing this record (marked "re-measured"):** Fedora 44, git
  2.56.0.

**The current export depends on in-tree attributes.** With a candidate
`.gitattributes` of `* text eol=crlf` and `ident`, `read-tree` plus
`checkout-index` wrote an LF blob as CRLF and expanded `$Id$` (re-measured).

**`--attr-source` does not remove host dependence.** `git
--attr-source=<empty tree> checkout-index` ignored the in-tree attributes.
It still applied the operator's `$GIT_DIR/info/attributes` (CRLF conversion
again), and it ran an operator-configured `filter.<name>.smudge` program on
candidate content; `-c core.attributesFile=/dev/null` changed neither
result (re-measured: the CRLF conversion, and the filter program created its
sentinel file).

**Blob reads are exact.** `git cat-file blob <id>` returned the committed
bytes in every case (re-measured: `git hash-object` of the returned bytes
equals the blob id).

**The hash-back proof is computable without git** (re-measured). A writer
that listed a commit's tree with `git ls-tree -r -z --full-tree` and wrote
each blob from `git cat-file`, followed by a walk that recomputed blob and
tree ids from the bytes and modes on disk, reproduced the root tree id in a
sha1 and in a sha256 repository. The trees held a nested directory, an
executable, a symlink, an empty file, a gitlink (an empty directory, the
recorded commit substituted), a `.gitattributes` of `* text eol=crlf` that
the writer did not apply, and the names `a.b` and `a/`, whose order depends
on git's rule that a tree entry sorts as its name followed by `/`.

**A legacy entry mode cannot be reproduced** (re-measured). A tree written
literally with the mode `100664` is accepted by `index-pack --strict`
(`git fsck` warns `badFilemode`), and `git ls-tree` prints the entry as
`100644`. A tree recomputed from the listing or from the filesystem
therefore has a different id from the stored one.

Conclusion: a `checkout-index` export's bytes depend on the operator's git
configuration, and filter drivers execute host programs on candidate
content. Exact bytes are obtained only by writing blobs directly, and a
proof over the written directory can establish that they were.

### 1.3 Upstream decisions this record builds on

- **#159 (ADR 0004):** the authority boundary. Agent output is a claim;
  every externally visible write is a typed controller effect, intent first
  (ADR 0004 D4.1); controller git reads an object by SHA as its own bytes
  (ADR 0004 D7.5); a phase persists the rendered payload in its effect
  record and recovers from it (ADR 0004 D8.4, D4.6), and a crash before the
  plan is saved relaunches the agent (ADR 0004 W1).
- **#160:** durable effect execution (landed): intended, attempted, observed
  or conflict.
- **#161, #163, #164:** the controller verifies the candidate it reads from
  the worktree, then pushes and publishes it (landed).
- **#162:** the controller renders, publishes and reads back the review
  comment, and journal recovery completes a round without relaunching the
  reviewer (landed).
- **#165:** no GitHub mutation credential in any child (landed).
- **#166 (ADR 0005):** design A. The common dir is read-only, new objects go
  to a private object directory, and the controller reads the candidate
  through the common dir (ADR 0005 D6.3) and imports exactly its objects
  with `--strict` (ADR 0005 D6.5) before publication. The export is a launch
  class of its own (ADR 0005 §2.5).
- **#167:** execution facts are additive (landed); ADR 0005 D16.1.
- **#168:** every agent and every repository-defined command runs under the
  outer sandbox when the mode is `required`; candidate addressing and the
  strict import (not landed).
- **#169:** `doctor`, `preferred`, and REMOTE treating `preferred` as
  `required` (not landed).

### 1.4 Terms

- **Candidate (C):** the exact bytes the controller would publish, review
  or merge. REMOTE: one commit and its tree. LOCAL: one workspace
  fingerprint.
- **Definition:** one validation command as trusted configuration names it
  (argv, working-directory role, scope, index in its set).
- **Validation set:** the ordered list of definitions in force.
- **Execution policy (P):** how a definition runs (D8.1).
- **Validator:** the process a definition launches. Its argv is trusted;
  what it executes is candidate code and untrusted.
- **`ValidationRecord`:** what the controller observed for one launch of one
  definition against one candidate under one policy (D10.1).
- **Applicable:** a record whose candidate, definition and policy equal the
  ones in force at the point of use (D8.3). Derived at use time, never
  stored.
- **Evidence for C:** one applicable PASS record for every definition of the
  set in force, in set order. An empty set is complete evidence.
- **`ReviewCertificate`:** the controller's own record that a clean review
  met complete evidence for one reviewed candidate (D12.1).

## 2. Decisions

Each decision is labelled `D<item>.<n>` after #170's decision numbers, and
names exactly one implementing child, or says that it is documentation only.
§5 repeats the assignment as one table and lists where this record differs
from #170's body. Where a child's body and this record disagree, this record
wins.

### 2.1 Trust statements (item 1)

**D1.1 (documentation only).** A **`ValidationRecord`** means exactly:

> AutoForge launched validator X, as defined by trusted configuration Y,
> against candidate C under execution policy P, and observed result R.

It does **not** mean any of these:

- the implementation is correct;
- the review is clean;
- GitHub CI passed;
- the candidate is still valid after the base moves;
- the result is reproducible on another host or toolchain;
- the validator is trusted code.

The argv is trusted configuration. What it executes (test files, build
scripts, `Makefile` targets) is candidate code and untrusted. The controller
trusts only its own observation of the launch, the exit and the captured
bytes.

**D1.2 (documentation only).** A **`ReviewCertificate`** means exactly:

> For candidate C, in review round N, bound to PR / HEAD / base / merge
> base (REMOTE) or to workspace fingerprint F (LOCAL), the controller
> accepted a clean review result. It presented exactly validation set V to
> that review, and every record in V is PASS for C under the definitions
> and policy recorded. For REMOTE, it verified the published review comment
> identity K.

It is **not** any of these:

- a GitHub approval or a CI result;
- permission to merge;
- proof that the code is correct;
- an authorization token;
- a substitute for the live GitHub re-read, the required checks, the
  protected-path rule, `merge.verification_commands` or any other merge-gate
  fact.

**D1.3 (#174).** A certificate is never shown to an agent, never published
and never accepted from one. No prompt variable, `CONTROL_RESULT` field or
effect payload carries it.

*Rationale.* Both statements are narrow on purpose: each says what the
controller saw, never what it concludes about the code. Anything wider would
make a local run on the operator's toolchain sound like a property of the
change.

### 2.2 Candidate identity (item 2)

**D2.1 (#172).** **`RemoteCandidate`**, the REMOTE validation identity:

- the repository (owner/name, as the controller resolved it);
- the object format (`sha1` or `sha256`);
- the full commit SHA;
- the full tree SHA.

Each SHA is full lower-case hex of the format's length.

**D2.2 (#172).** **The tree SHA is derived, never reported.** It is read from
the commit object in the operator's object store, after the controller made
the object local, as ADR 0004 D7.5 reads a commit: re-hashed against its id,
with replacement objects and the commit-graph off
(`GitTransport.read_commit`). No agent reports it. Commit and tree are
content addresses, so the store itself proves that they correspond.

**D2.3 (#172).** **Provenance is recorded beside the identity, never part of
its key.** It is one of:

- `worktree`: the issue number, the worktree id, whether #168's strict
  import ran for this candidate (always false before #168 lands), and the
  base revision the phase's entry observation required (ADR 0004 D4.4);
- `pr-head`: the canonical PR URL, with the objects fetched without
  creating a ref.

Provenance is bounded strings and booleans, displayed in the REVIEW
evidence section (D9.1), and never compared.

**D2.4 (#173).** **No mutable local branch is ever an input.**

- A producer candidate is exactly the commit the phase's candidate check
  accepted (`_check_analyze_result`, `_check_fix_result`,
  `_check_replan_result`): the issue worktree's detached `HEAD`, read today
  with `git -C <worktree>` and, after #168, through the common dir (ADR 0005
  D6.3). The gate never reads `HEAD` a second time; it validates the SHA
  the check accepted, which is the SHA K1 pushes.
- A published candidate is the PR head SHA GitHub returned to
  `_bind_review_head`.
- Each gate fills the candidate's provenance (D2.3) from what it read.

**D2.5 (documentation only).** **Base ref, base revision, merge base and PR
identity are review facts,** bound by `_bind_review_head` and carried into
the certificate (D12.1), not into validation evidence, because every Wave 2B
definition is candidate-tree scoped (D4.2). Before publication
(`ANALYZE_EXECUTE`) a candidate has no PR.

**D2.6 (#172).** **`LocalCandidate`:**

- the workspace fingerprint (ADR 0001's snapshot under its `SNAPSHOT_TAG`,
  `local_workspace.py` :133);
- `workspace_policy_digest`: the run contract's `WorkspacePolicy.digest()`
  (`run_contract.py` :172);
- the run id.

There is no commit model. The feature specification's SHA-256 and the git
anchor stay the contract's REVALIDATED facts and are checked where they are
today. The run contract is immutable within a run, so the run id plus
`workspace_policy_digest` identify the classification rules; the definition
digest (D3.5) covers `local.validation_commands`.

**D2.7 (#172).** **Two distinct types, no union of optionals.** They share no
optional field. A persisted record carries a `kind` discriminator (`remote`
or `local`), and parsing refuses a record whose fields do not match exactly
one kind. A REMOTE state holding a `local` record, or a LOCAL state holding
a `remote` one, is refused on load like any other illegal combination
(ADR 0001 rule S).

*Rationale.* One type with optional commit and fingerprint fields would let
a record be half of each, and every reader would re-derive which half
counts. Two types make the illegal combination unconstructible.

*Rejected.*

- **The branch name as identity.** It moves; the commit does not.
- **An agent-reported tree SHA.** The store already holds the answer.
- **Base and merge base in the validation key.** No Wave 2B definition
  reads the base, so a base move would discard evidence that is still true.

### 2.3 Validation definitions (item 3)

**D3.1 (#172).** **REMOTE definitions come from a new trusted configuration
key** holding argv lists, empty by default. The recommended key is a
top-level `validation.commands`, parsed by `_as_argv_list` like
`merge.verification_commands`, in the built-in YAML subset and under strict
key validation; #172 may refine the name. It is read from the controller's
loaded configuration, never from the candidate tree.

**D3.2 (#172).** **LOCAL definitions are the run contract's
`local.validation_commands`,** unchanged: immutable for the run, and drift
refuses (ADR 0001 §5.7).

**D3.3 (documentation only).** **`merge.verification_commands` keeps its
meaning** and keeps running at every merge gate pass, with its
`premerge_verified_*` cache rule unchanged. An operator may configure the
same commands in both places. Wave 2B never skips merge-time verification
because a candidate-time record passed.

**D3.4 (documentation only).** **Never a source:**

- agent output or `CONTROL_RESULT`;
- the candidate's own configuration, workflows, `Makefile`, `AGENTS.md` or
  `CLAUDE.md`;
- issue or PR text;
- prose.

**D3.5 (#172).** **Binding.**

- Every record binds a `definition_digest`: SHA-256 over the canonical
  encoding of what runs. That is the argv as configured (not the redacted
  copy), the working-directory role (`candidate-root`: the export in REMOTE,
  the repository root in LOCAL), the scope (D4.1) and the definition's index
  in its set. The encoding is canonical JSON (sorted keys, no whitespace,
  every argv element its own string) with a version label, as
  `WorkspacePolicy` is encoded (ADR 0001 §5.7).
- How a definition runs (timeout, environment, sandbox) is policy P (D8.1),
  not part of the definition.
- A certificate binds the `validation_set_digest`: SHA-256 over the same
  kind of encoding of the ordered definition digests.

A configuration change therefore never lets old evidence satisfy a new set.

**D3.6 (#172).** **An empty set is a valid, explicitly empty set.** Its
`validation_set_digest` is the digest of the empty list. The certificate
then states that no controller validation was configured, and so does the
REVIEW evidence section (D9.2). Wave 2B does not make validation mandatory
configuration.

*Rationale.* The definition is what the operator decided; the policy is how
the controller ran it. Keeping them apart lets a timeout change withdraw
evidence (D8.3) without pretending that the command changed.

*Rejected.* **Reading validation commands from the candidate** (a
`Makefile` target, a workflow file, `AGENTS.md`). The candidate would choose
the check that certifies it.

### 2.4 Scope of a definition (item 4)

**D4.1 (documentation only).** Three scopes are defined, for the
invalidation matrix (D14.1):

- **candidate-tree:** depends only on C's bytes;
- **base-bound:** depends on C and the base revision;
- **merge-result-bound:** depends on the merge of C into the base.

**D4.2 (#172).** **Wave 2B implements candidate-tree only.** The
configuration cannot name another scope; if #172 gives a definition an
explicit scope field, any value other than `candidate-tree` is refused at
load. Base-bound and merge-result-bound validation (a controller test
merge) is future work and is not created.

*Rationale.* A candidate-tree record stays true while the base moves, which
is what lets the REVIEW entry reuse a producer's records and lets the merge
gate keep checking the base separately.

### 2.5 Placement: controller gates around existing transitions (item 5)

**D5.1 (#173).** **REMOTE producer gate** (`ANALYZE_EXECUTE` #161, `FIX`
#163, `REPLAN_REEXECUTE` #164).

- **Order:** after `_invoke_phase` returns a result whose candidate check
  passed (and after #168's strict import, once it lands), and before
  `_apply_analyze`, `_apply_fix` or `_apply_replan` persists the effect plan
  (ADR 0004 D4.1), the controller validates the exact candidate (D2.4).
- **All PASS:** publication proceeds unchanged.
- **Any non-PASS:** no effect intent is recorded. Nothing is pushed or
  published, and no review starts.

**D5.2 (#173).** **A non-PASS in `ANALYZE_EXECUTE` or `FIX` is a conclusive
result, not a `VerificationError`.** Wave 2B does not reuse the unbounded
path of §1.1.

- In one atomic write, the controller appends the record and increments
  the persisted counter `producer_validation_failure` {phase, count,
  candidate commit, last record seq}, a field #172 defines (D11.2).
- The step then ends without a transition, and the next step relaunches
  the agent, as a relaunch of the same entry under the existing attempt
  accounting (ADR 0004 D4.4): the persisted entry observation is reused.
- When the count reaches a fixed bound (3, the value of LOCAL's
  `MAX_LOCAL_PHASE_ATTEMPTS`, kept as a constant of its own), the phase goes
  to `BLOCKED`, naming the candidate, the definition index and the record
  seq.
- A relaunch that hands back the same candidate reuses its terminal record
  without rerunning it (D10.6), and counts again.
- The counter is cleared in every write that changes the phase, and when
  the run moves to the next issue.

**D5.3 (#173).** **`REPLAN_REEXECUTE` takes the rejection path #164 uses for
`tests_passed=false`:** a rejection persisted before anything is sent, with
the record appended in the same write. It has no counter. A replan whose
rejection `_check_replan_result` already decided is not validated.

**D5.4 (#173).** **Relaunch input.** The relaunched agent's prompt
(`analyze_execute.md`, `fix.md`) carries a controller-rendered failure
section built from the record the counter names: provenance as in D9.1, and
the redacted, bounded tails inside `fenced_untrusted_block`. Nothing else
about the failure reaches the agent.

**D5.5 (#173).** **REVIEW entry (REMOTE).**

- **Order:** after `_bind_review_head` and the round reconciliation, and
  before the reviewer launches, the controller requires evidence for the
  bound PR head, and writes the `review_evidence` binding (D11.2) in the
  save that charges the reviewer's launch.
- **Normal path:** the head is the candidate a producer just validated and
  published, so the records are reused and nothing reruns.
- **No applicable record:** a human or external push, a resume across the
  upgrade, or a candidate no producer validated. The controller validates
  the PR head now, fetching objects only (`fetch_pr_head`,
  `commit_is_local`; no ref is created).
- **Non-PASS at REVIEW entry:** this happens only for a candidate no
  producer validated. It is presented to the reviewer (D9.1, D9.2), who
  cannot return a clean result over it (D9.4).
- **Journal-first completion:** a REVIEW that completes from a persisted
  plan (#162) launches no reviewer and validates nothing; its binding was
  written before the reviewer whose result it completes.

**D5.6 (#172).** **LOCAL write phases** (`ANALYZE_EXECUTE`, `FIX`):
validation stays where it is (`_verified_snapshot`), now producing records,
under the convergence rule (D7.2).

**D5.7 (#173).** **LOCAL REVIEW** runs no validation and binds no
fingerprint (ADR 0001 §5.10). It requires evidence for the bound
fingerprint under the run contract's set (an empty set is complete), and
writes the `review_evidence` binding (D11.2) naming that evidence in the
save that charges the reviewer's launch, as D5.5 does in REMOTE. The
binding names records, never a fingerprint, so §5.10's rule holds. A LOCAL
state with a non-empty set and no evidence, which only a state persisted
before Wave 2B can be, enters `BLOCKED` with "no validation evidence for
this LOCAL run; start a new local run", before any launch.

**D5.8 (documentation only).** **No `VALIDATE` phase.** Every window is
modelled by state plus records: a durable attempt marker (D11.2), records
keyed by candidate, definition and policy, and the existing phase. A
`VALIDATE` phase would add legal edges, unblock targets and recovery paths
for no information the records lack.

*Rationale.* The gate sits exactly where ADR 0004 already draws the line
between an agent's claim and the controller's act: after the candidate is
verified, before the first intent. A failing candidate therefore never
reaches GitHub, and a crash on either side of the gate is covered by an
existing window (§2.16).

*Rejected.*

- **A `VALIDATE` phase.** D5.8.
- **Reusing the REMOTE `VerificationError` path for a failing validator.**
  It has no bound and no `BLOCKED` (§1.1).
- **Validating inside the correction loop.** A failing validator is not a
  malformed result; a correction would ask the agent to repair its
  `CONTROL_RESULT`, not its code.

### 2.6 REMOTE materialization: a byte-exact export with a hash-back proof (item 6)

**D6.1 (#171).** **Source objects.** The candidate's objects must be in the
operator's shared object store: after #168's strict import (producers; until
#168 lands, agents write the shared store directly), or after an
objects-only fetch plus `commit_is_local` (REVIEW entry). A candidate still
only in a private object directory is not materializable. That is a
refusal, not a retry.

**D6.2 (#171).** **The writer.**

- It enumerates `git ls-tree -r -t -z --full-tree <tree>` and streams the
  blobs with `git cat-file --batch`, every git process built by
  `local_git_request` (ADR 0004 D7.3, D7.5: no replacement objects, no
  commit-graph, no hooks, an allow-listed environment).
- It writes into a fresh private directory (mode `0700`) at the export
  location #168 chooses (ADR 0005 D8.4); until #168 lands, one created by
  `tempfile.mkdtemp` in the system temporary directory, where
  `export_commit_tree` creates its export today. Candidate validation uses
  the prefix `autoforge-validate-`; the merge gate keeps
  `autoforge-premerge-`. Both are the same kind of workspace for #168: one
  shot, the export writable, nothing else of the repository visible
  (ADR 0005 §2.5).
- **One export per definition launch.** Each launch of a definition gets a
  fresh export and its own proof (D6.3), as ADR 0005 §2.5 gives the export
  command a workspace per launch. A definition therefore never runs on what
  an earlier definition wrote, and its record is a function of (C,
  definition, P) whether the earlier definitions ran or were reused
  (D10.6). The merge gate's export keeps today's one export per gate pass
  until #168 routes it per launch; #171 changes only how it is written.
- Names are handled as bytes and never decoded. Blob content is streamed
  to disk as bytes: never decoded (the executor captures text and replaces
  invalid UTF-8), never cut at a capture bound, and never held whole in
  memory. How the byte stream reaches the controller without breaking
  ADR 0002's process ownership is #171's.
- Every entry is created exclusively, without following a link, below the
  directory created for its parent tree.

Its rules:

- a regular file gets mode `0644`, or `0755` when the entry is executable;
- a symlink is created holding its link text and is never followed;
- a gitlink is created as an empty directory and counted (submodules are
  not populated); its recorded commit is never read from the store;
- a tree is created as a directory, so an empty subtree appears too;
- there is no `.git`;
- an entry name that is `.`, `..` or `.git` (case-insensitively), or that
  contains `/` or NUL, is refused, as defence in depth;
- no attribute, eol conversion, `ident`, smudge filter, `core.autocrlf`,
  `info/attributes` or `core.attributesFile` is consulted.

**D6.3 (#171).** **The proof.** Before any validator launches, the
controller re-walks the directory with `lstat`, never following a link:

- it recomputes each blob id from the bytes on disk (a symlink's from its
  link text), in the repository's object format;
- it takes each entry's mode from what is on disk: `100755` for a regular
  file with the owner's execute bit, `100644` for any other regular file,
  `120000` for a symlink, `40000` for a directory; any other kind of entry
  is a mismatch;
- it recomputes the tree ids bottom-up, in git's entry order;
- at a gitlink path it requires an empty directory and substitutes the
  tree entry (mode `160000`, the commit SHA `ls-tree` listed); it never
  hashes or reads the submodule commit;
- it requires the root to equal C's tree SHA, with no extra entry.

The walk is the controller's own code; no git process and no git
configuration takes part. A mismatch (filesystem case folding, a full disk,
a writer bug, a legacy entry mode, §1.2) is a materialization failure:
inconclusive, never a record (D10.5).

**D6.4 (#171).** **Isolation, and why validation cannot modify C.**

- Validators run in a disposable export, with the export writable and
  nothing else of the repository visible (#168 routes the launch through its
  projection for repository-defined commands). The issue worktree is never
  their working directory. The export is deleted afterwards, and a second
  run materializes afresh.
- **In every mode,** validation cannot modify C: C is a commit id and a tree
  id, and every later read of it is by id and re-hashed (ADR 0004 D7.5) or
  re-proved (D6.3). Altered bytes are a refused read or another candidate,
  never C.
- **Under an enforced sandbox,** the object store and the issue worktree are
  not visible to the launch at all, so they are untouched.
- **Under `disabled`,** the validator runs as the operator's UID (D8.4) and
  can reach the worktree and the store by absolute path, as any
  unsandboxed launch can. What it changes there is outside the boundary;
  it cannot be attributed to C.

**D6.5 (documentation only).** **Honesty.**
Validators see the committed bytes:

- checkout-time eol conversion, `ident` expansion and filter drivers are
  not applied;
- LFS pointers stay pointers;
- git metadata and submodules are absent.

`docs/agent-guides/github-safety.md` and `autoforge.example.yaml` already
say the last item of the `merge.verification_commands` export; #171 adds the
first two (D6.6).

**D6.6 (#171).** **Reuse.** The same materializer, proof included, replaces
`read-tree` plus `checkout-index` in `premerge.export_commit_tree`. This is a
fidelity fix: the merge-gate export stops depending on the operator's git
configuration and stops running host filter programs.
`merge.verification_commands` still runs at every gate pass, with its cache
rule unchanged. #171 updates the text that names `checkout-index`
(`premerge.py`, `github-safety.md`, `autoforge.example.yaml`) and adds D6.5's
eol, `ident`, filter and LFS facts to it.

*Rationale.* §1.2 leaves one exact source, the blob bytes, and one exact
check, the tree id. Writing from the first and proving with the second
makes "the validator saw C" a fact the controller computed, not a property
of the operator's git setup.

*Rejected.*

- **A disposable detached worktree.** It shares the common dir, applies
  attributes and filters, and needs worktree lifecycle the controller does
  not own.
- **A satellite clone.** It needs a fetch path and a lifecycle for no gain
  in exactness.
- **`checkout-index --attr-source`.** Measured as host-dependent (§1.2).
- **`git archive`.** It applies `export-ignore` and `export-subst`, which
  are in-tree attributes.
- **`ls-tree -r` without `-t`** (#170's text). An empty subtree, which git's
  index cannot produce but a written tree can hold, would be missing from
  the export, and the proof would fail on a candidate it could have
  exported.

### 2.7 LOCAL materialization: in place, with a convergence rule (item 7)

**D7.1 (#172).** **In place.** LOCAL keeps ADR 0001 rule V: validation
commands run in the repository root (writable; the git dir read-only under
#168). Copying the workspace is rejected: LOCAL deliberately leaves the
environments it needs (virtual environments, `node_modules`, build caches)
outside the fingerprint, so a copy is either enormous or broken.

**D7.2 (#172).** **Rule: evidence for fingerprint F requires a validation run
that starts at F and ends at F.**

- Each LOCAL record carries `start_fingerprint` and `end_fingerprint`. It
  is evidence only for its start fingerprint, and only when the two are
  equal. The controller walks the tree before the first command and after
  each command; consecutive records share a walk, and the walk keeps
  ADR 0001 §5.5's bounds. A walk that is refused is the existing refusal,
  and that command leaves no record.
- If a PASS run changed covered bytes (F0 → F1), the controller performs
  **one** confirming run of the whole set, starting at F1. If it ends at F1
  with every command passing and every record starting and ending at F1,
  F1 is the validated candidate, which is the post-validation binding
  ADR 0001 already makes.
- Anything else is "validation does not converge". The phase is not
  verified; it follows the existing bounded relaunch, then `BLOCKED`, and
  the message names the operator's remedy (`local.exclude`, or a validator
  that does not write covered files).
- A FAIL or TIMEOUT run is a record for its start fingerprint, never
  evidence.

**D7.3 (documentation only).** **The consequence, stated honestly.** In
LOCAL a validator can write the workspace; that is ADR 0001's decision. The
controller never attributes evidence to a fingerprint the
evidence-producing run changed, so the validated candidate is exactly the
candidate REVIEW is bound to.

**D7.4 (this record).** **ADR 0001 §5.10 gains a dated amendment note**
recording D7.2. Rules S, C, V and G are otherwise unchanged.

*Rationale.* Today a validator that rewrites a covered file is accepted,
and its rewrite is what REVIEW is bound to, although nothing validated the
rewritten tree. The confirming run checks exactly that tree, once.

*Rejected.*

- **Copying the workspace.** D7.1.
- **Binding the pre-validation fingerprint.** It makes every writing
  validator a self-inflicted drift (ADR 0001 §5.10).
- **Repeating until a fixed point.** One confirming run either converges or
  shows that the validator does not; more runs add cost, not information.

### 2.8 Execution policy P and sandbox facts (item 8)

**D8.1 (#172).** **P is computed from controller configuration only, and is
path-independent.** Its inputs:

- the sandbox mode (`sandbox.mode`, #168 and #169). Until #168 lands, every
  launch is unsandboxed and the mode is `disabled`;
- the digest of a policy *template*: the projection roles and their access
  modes, the network position and the namespaces, with every concrete path
  replaced by its role name (`candidate-root`, `tmp`, ...). It is computed
  from the role-level inputs #168 passes to #167's compiler (ADR 0005
  D13.2), before concrete paths are bound. If the compiler exposes only
  the per-launch description, the template is that description with each
  concrete path replaced by its role name. Under `disabled` it is a fixed
  "no outer sandbox" template;
- the digest of the environment allow-list's names in force
  (`execution.environment_names()`: `env_allowlist` plus
  `env_allowlist_extra`);
- the timeout (`execution.command_timeout_seconds`);
- `contain_orphans`;
- the materialization method (REMOTE `byte-exact-v1`; LOCAL `in-place`).

A per-launch description is never hashed into P: it names the fresh export
directory, so two exports of the same candidate would never agree.
`policy_digest` is SHA-256 over the canonical encoding of these inputs, with
a version label. Editing the timeout or the allow-list after a clean review
therefore changes P, and the merge gate refuses until the configuration is
restored or a new round runs (D14.1).

**D8.2 (#172).** **Each record stores** `policy_digest`, the mode and the
timeout, and the **enforcement facts** copied from the in-memory
`ExecutionResult.sandbox` (`SandboxFacts`) the executor returned for that
launch: enforced or not, the backend and its version, the network position,
and the teardown fact. They are bounded strings and booleans, displayed
(D9.1, `status`), and read by no decision except D8.3's second rule. A
launch with no description records `enforced: false` and no backend.

**D8.3 (#172).** **Applicability.**

- A record applies only under an equal `policy_digest`.
- When the current mode is `required`, or `preferred` in REMOTE, which
  refuses as `required` does (ADR 0005 D12.2), the record must also show
  `enforced: true`.
- A policy change can withdraw evidence, never grant it.

**This narrows ADR 0005.** Its D16.1 says that no transition, verification,
recovery or merge decision reads sandbox facts, and its D17.1 rejects
persisting the enforcement state of a launch because the controller would
then read it. For validation records only, this record persists the copied
facts (D8.2) and lets exactly one rule, the second above, read one of them.
That rule reads the persisted record, never `execution.json`, and can only
withdraw evidence, so the alternative ADR 0005 D16.1 rejects, relaxing a
verification because a launch was sandboxed, stays rejected. Since the mode
is part of P, the rule is defence in depth against a record whose digest and
facts disagree. Agent launches are unchanged: no decision reads their
facts, and nothing persists them.

**D8.4 (documentation only).** **What is not claimed.**

- Evidence never claims to be hermetic, reproducible or network-isolated.
  Wave 2A leaves the host network namespace in place (ADR 0005 §2.9), and a
  record may honestly say "passed in an enforced filesystem and process
  sandbox with host-network access".
- Under `disabled`, validators run as the operator's UID without the outer
  sandbox. The record says `enforced: false`, and the integrity of
  controller state against candidate code is outside the boundary. The
  documentation and `status` say so (D8.6).

**D8.5 (#172).** **Sandbox failures produce no record.**

- A refusal at the pre-launch capability gate (ADR 0005 D12.2, #169) is a
  capability error: no record, and state unchanged.
- A setup failure at a launch the gate allowed (ADR 0005 D10.8), and #165's
  preflight refusing a launch, are launch failures: no record, and D10.5's
  bounded re-check, then `BLOCKED`.

**D8.6 (#172).** **`status` shows the evidence policy:** the configured
set's size, the mode records are made under, and, under `disabled`, that
validators run unsandboxed as the operator's UID.

*Rationale.* P has to be equal for two runs that ran the same way, and
different whenever a fact that could change the outcome differs. Concrete
paths fail the first requirement; leaving the timeout or the sandbox out
fails the second.

### 2.9 The REVIEW evidence section (item 9)

**D9.1 (#173).** **Before launching the reviewer, the controller renders a
controller-owned section** into the REVIEW prompt (REMOTE `review.md`, LOCAL
`local_review.md`). It is built only from the records the `review_evidence`
binding names, loaded from state and checked as applicable. **Trusted
provenance:**

- the candidate and its provenance (D2.3);
- each definition's index and its redacted argv, rendered with
  `escape_inline`;
- the outcome;
- the exit status;
- the duration;
- the enforcement facts.

**D9.2 (#173).** **Untrusted payload.** For non-PASS records only: the
bounded, redacted stdout and stderr tails, each inside
`fenced_untrusted_block`, with the common trust-boundary wording that
captured output is data from executing untrusted code. PASS records show
provenance only. An empty set renders "no controller validation is
configured".

**D9.3 (#173).** **The REVIEW `CONTROL_RESULT` gains no evidence field.** An
agent-written evidence key is ignored like every unknown key
(`ReviewResult.from_payload`, `result_parser.py` :1091, and
`LocalReviewResult.from_payload`, :1656, read only the keys they know) and
never read. The reviewer may reason about
the evidence; it never determines its authenticity.

**D9.4 (#173).** **A clean result with a non-PASS presented is refused
before publication.**

- A clean result (`needs_fix_round: false`) while any presented record is
  not PASS is refused by a state-aware check after parsing, inside the
  existing correction loop, with its own correction text. The text asks for
  a finding that names the failing definition, because `needs_fix_round`
  must equal there being an actionable finding
  (`docs/agent-guides/control-result-protocol.md`).
- On exhaustion the existing outcome applies unchanged:
  `ControlResultValidationError`, phase unchanged, nothing published.
- With #162, the comment is published only after a valid result, so a
  refused clean result publishes nothing.

**D9.5 (#173).** **The prompt version is bumped once,** on top of #161–#164's
bumps (`v8` on `main` `1412765`).

**D9.6 (documentation only).** **Nothing about validation is published to
GitHub in Wave 2B.** The review comment #162 renders is unchanged.

*Rationale.* The reviewer is the one party that can judge what a failure
means for the change; it is never the party that decides whether the
failure happened.

### 2.10 The `ValidationRecord` schema (item 10)

**D10.1 (#172).** **Fields** (bounded, typed, no free-form dictionary):

| Field | Content |
|---|---|
| `schema` | integer record schema version (1) |
| `seq` | per-state, monotonically increasing integer |
| `digest` | SHA-256 over the canonical encoding of every other field |
| `candidate` | `RemoteCandidate` or `LocalCandidate` (D2.1, D2.6) under its `kind`, plus bounded provenance (D2.3) |
| `definition` | set index, redacted argv, `definition_digest`, scope |
| `policy` | `policy_digest`, mode, timeout seconds |
| `enforcement` | the copied facts (D8.2) |
| `outcome` | `PASS`, `FAIL`, `TIMEOUT` or `INTERRUPTED` |
| `exit_code` | integer as the executor reported it, or null (`TIMEOUT`, `INTERRUPTED`) |
| timing | `started_at`, `finished_at` (UTC, ISO 8601) |
| leftovers | ADR 0002's facts: descendants killed, group survived the kill, orphans killed, orphan survived, orphans unchecked |
| output | `stdout_tail`, `stderr_tail`, `stdout_bytes`, `stderr_bytes`, and `tail_truncated` per stream |
| LOCAL only | `start_fingerprint`, `end_fingerprint` |
| `run_log` | the relative run-log name: informational, never read for authority |

The configuration does not bound an argv today (`_as_argv_list`). #172 bounds
the element count and each element's length for every command list a record
copies, and refuses a configuration beyond them at load, so a record never
truncates an argv.

**D10.2 (#172).** **Output safety.** Raw bytes are processed in this order:

1. they are captured under the executor's byte caps;
2. they are decoded with replacement characters;
3. they are redacted (`redaction.redact`) on the full captured text;
4. the tail is cut to the bound;
5. control characters other than newline and tab are escaped;
6. the truncation facts are recorded.

**D10.3 (#172).** **Bounds** (recommended; #172 may lower them, never raise
them silently):

- at most 16 definitions per set, refused at load beyond;
- at most 2,000 characters per stream tail (today's `VerificationError`
  tail);
- at most 64 records per state (D11.4 decides eviction).

**D10.4 (#172).** **Outcomes.**

- `PASS`: the launched command exited 0.
- `FAIL`: any other exit status of a launched command, 126 and 127
  included, and a termination by a signal the executor did not send.
- `TIMEOUT`: the executor's timeout ended it.
- `INTERRUPTED`: historical only, written by the resume path from a
  leftover marker (D11.3).

`FAIL` and `TIMEOUT` are terminal for (C, definition, P) under automatic
recovery; only the operator's override makes them historical (D14.2).

**D10.5 (#172).** **Not a record.**

- A launch or setup failure the executor or the backend reports (D8.5) is
  inconclusive: no record, a bounded in-process re-check, then `BLOCKED`
  naming the command.
- A materialization failure (D6.3) is inconclusive: no record, one fresh
  materialization in the same process, then `BLOCKED` naming the candidate
  and the failing entry.
- A producer candidate whose objects are not in the shared store (D6.1) is
  refused under the phase's existing candidate rules (ADR 0005 D6.5), never
  retried. At REVIEW entry, a PR head that cannot be fetched takes today's
  inconclusive path (`fetch_pr_head`), and `resume` reads it again.

**D10.6 (#172).** **Execution.** Definitions run sequentially in set order.
For each one, an applicable `PASS` record is reused; an applicable terminal
record (D10.4) is reused and ends the set; otherwise the definition runs.
The set stops at the first non-PASS, and the definitions after it get no
record.

*Rationale.* A record holds only what the controller itself observed, in a
shape a loader can validate field by field. Redacting before the cut keeps
a secret that straddles the cut from surviving as a fragment.

### 2.11 Persistence: Option A, bounded strict state (item 11)

**D11.1 (#172).** **Everything lives in `state.json`:** the records, the
attempt marker, the REVIEW evidence binding, the certificate, the producer
failure counter and the override watermark. They are validated on load
like every persisted field. Corruption fails loudly, and referential
integrity is checked on load:

- a certificate, binding or counter that names a missing record;
- a digest mismatch;
- a record matching neither candidate kind, or the other mode's kind
  (D2.7).

Nothing is written outside the state file, so SafeRoot's atomic write
(`safefs.py`) is the only primitive needed. There is no blob store. Run
logs, transcripts and provider summaries keep their full output and are
never authority.

**D11.2 (#172).** **The persisted additions,** all owned by one migration
(D15.1):

- `validation_records`: a bounded list (D10.1, D10.3).
- `validation_attempt`: one optional marker {candidate, definition digest,
  policy digest, `started_at`}. It is written immediately before each
  launch: after the pre-launch capability gate, the materialization and its
  proof, and #165's preflight, so a refusal by any of them writes no marker.
  It is cleared in the write that appends the record. When the executor
  reports a launch or setup failure instead (D10.5), it is cleared in the
  controller's next write: the re-check's own marker replaces it, or the
  `BLOCKED` write clears it. Only a crash leaves a marker for the resume
  path to find (§2.16).
- `review_evidence`: an optional binding {review round, candidate, ordered
  record seqs and digests, `validation_set_digest`}, written before the
  reviewer launches (D5.5, D5.7).
- `review_certificate`: an optional `ReviewCertificate` (D12.1).
- `producer_validation_failure`: an optional counter {phase, count,
  candidate commit, last record seq} (D5.2).
- `validation_override_seq`: an optional integer, the operator's override
  watermark (D14.2).

**D11.3 (#172).** **Loading is read-only.** Load validates and never mutates.
Converting a leftover marker into an `INTERRUPTED` record, and clearing a
marker whose record exists, happen only in the engine's resume and step path
under the repository lock, in one atomic write. `status`, `doctor` and the
dry run only report a pending marker.

**D11.4 (#172).** **Retention.**

- Records named by `review_evidence`, `review_certificate` or
  `producer_validation_failure` are never evicted. They are at most
  2 × 16 + 1, below the bound.
- Other records are evicted oldest first beyond the bound.
- All six fields are cleared when the run moves to the next issue, in
  `AutoForgeState.reset_for_new_issue`, where `premerge_verified_*` is cleared
  today.
- A new LOCAL run starts empty.

*Rationale.* The data is small and bounded, and it must be exactly as
durable as the phase it justifies. One file with one atomic write has no
second object to reconcile after a crash.

*Rejected.* **Option B: an immutable artifact under SafeRoot, bound from
state.** It adds a second file whose existence and digest must be
reconciled on every load, an orphan class and a retention policy, and a new
SafeRoot naming scheme, for data that fits the bounds above.

### 2.12 The `ReviewCertificate` (item 12)

**D12.1 (#172).** **Fields** (#172 defines the schema; #174 issues the
certificate, D12.2):

- `schema`;
- `digest`: SHA-256 over every other field;
- `kind`: `remote` or `local`;
- the candidate;
- `review_round`;
- REMOTE: `reviewed_pr_url` (the PR identity), `reviewed_head_sha`,
  `reviewed_base_ref`, `reviewed_merge_base_sha`, and the review comment
  identity observed on #162's read-back: the comment URL, its id, and the
  marker key (`review_round`, `reviewed_head_sha`, `reviewed_base_ref`,
  `reviewed_merge_base_sha`);
- LOCAL: `reviewed_workspace_fingerprint`;
- the record seqs and digests of the presented set;
- `validation_set_digest`;
- `policy_digest`;
- the clean result (`needs_fix_round: false`, zero actionable findings);
- `issued_at`.

No review prose is included.

**D12.2 (#174).** **Issuance.**

- The certificate is written in the **same atomic state write** as
  `REVIEW → READY_FOR_MERGE` (REMOTE) or `REVIEW → DONE` (LOCAL).
- Its inputs are the validated clean result, the persisted
  `review_evidence` binding of this round, the review binding, and for
  REMOTE the comment identity from #162's read-back, or from the K4
  record's observation when the journal completes the round (the K4 record
  is dropped by that same save).
- Issuance requires every presented record to be PASS and applicable to
  the reviewed candidate.
- It is a pure function of persisted, verified inputs, and re-derivable
  after a crash.

**D12.3 (#174).** **`READY_FOR_MERGE` is never entered to merge an open PR
without an applicable certificate.** This covers `REVIEW → READY_FOR_MERGE`
and the `BLOCKED → READY_FOR_MERGE` unblock of an OPEN PR at the
clean-reviewed revision (`_unblock_with_pr`). When that unblock finds no
applicable certificate, its decision is `REVIEW` (a fresh round under the
existing round cap), never `READY_FOR_MERGE`.

**D12.4 (#175).** **A PR GitHub already reports MERGED keeps today's rule:**
the unblock branch for a merged, uncounted PR and `MERGE` crash recovery
count only a merge made at the clean-reviewed revision and base. No merge
is performed there; the certificate is a prerequisite of a merge decision,
not of counting one.

**D12.5 (#174).** **LOCAL `DONE` is never entered without a certificate.**
GitHub is never consulted for a LOCAL certificate.

*Rationale.* Writing the certificate with the transition leaves no state in
which the phase moved without one, or one exists for a phase that did not
move.

### 2.13 MERGE: a fresh live gate, never "certificate ⇒ merge" (item 13)

**D13.1 (#175).** **Placement.** In `_verify_pr_for_merge` (both the
`READY_FOR_MERGE` gate pass and `MERGE`), the certificate check runs:

- after the PR identity and revision checks;
- beside the #94 clean-review re-read (`_clean_review_problem`);
- before the readiness reads and `merge.verification_commands`, so a PR
  with no applicable certificate never runs its code locally.

A PR that GitHub already reports MERGED is reconciled before these checks,
exactly as today (D12.4).

**D13.2 (#175).** **The check requires all of these:**

- the certificate exists and its digest verifies;
- it names `current_pr_url` and the PR GitHub returns;
- its HEAD, base ref and merge base equal the live ones;
- its comment identity equals `last_review_comment_url`;
- its records exist and are PASS;
- the current `validation_set_digest` and `policy_digest` equal the
  certificate's.

A failure the earlier revision checks did not already route (D14.1) is
`BLOCKED`, naming the requirement.

**D13.3 (documentation only).** **Every existing gate is kept:**
`safety.allow_merge`, the status rollup and check definitions, base, HEAD
and merge-base verification, protected paths, the auto-merge and
merge-queue refusals, the live #94 re-read, and `merge.verification_commands`.
The certificate adds a prerequisite and removes none.

### 2.14 Invalidation matrix (item 14)

**D14.1 (#175).** **"Inapplicable" is derived at use time; no staleness flag
is ever written.** The one persisted operator input is the override
watermark (D14.2). Old records and certificates stay as history until they
are evicted or cleared. They are never rewritten for a new candidate.

| Change | `ValidationRecord` (candidate-tree) | `ReviewCertificate` | Routing |
|---|---|---|---|
| New commit, force-push, human push to the PR | inapplicable (another commit) | inapplicable | existing drift → `REVIEW`; the REVIEW entry validates the new head |
| Same tree, another commit | inapplicable (the commit is part of the key) | inapplicable | as above |
| Base advances, merge base unchanged | applicable | applicable (it binds the merge base, as today) | `BEHIND` is left to the existing readiness check |
| Base rewritten (the merge base moves) | applicable | inapplicable | existing stale → `REVIEW`; records reused, a new round required |
| Retarget (the base ref changes) | applicable | inapplicable | existing → `REVIEW` |
| Base-bound or merge-result-bound definition | n/a in Wave 2B (refused at load) | n/a | future |
| Definition set changes (configuration) | inapplicable | inapplicable | a producer gate or the REVIEW entry validates under the new set; at the merge gate, `BLOCKED` naming the change, and the operator restores the configuration or unblocks to `REVIEW` (no automatic re-review loop) |
| Policy changes (sandbox mode, policy template, allow-list, timeout, materializer) | inapplicable | inapplicable | as for a definition change |
| Review comment edited, deleted or doubled | n/a | inapplicable | existing #94 re-read → `BLOCKED` |
| State hand-edited | load-time validation fails, or a digest mismatch | same | loud failure; never coerced |
| Replan replacement | no transfer: a new commit, new records | the old PR's certificate never matches the replacement PR | the replacement PR needs its own validation and review |
| LOCAL fingerprint drift | inapplicable | inapplicable | existing LOCAL drift handling |
| Host toolchain changes | not detected | not detected | out of scope: no reproducibility claim |
| Legacy state (before Wave 2B) in `READY_FOR_MERGE` / `MERGE`, OPEN PR | none | none | `BLOCKED` at the gate, with "unblock to `REVIEW`"; a PR already MERGED is reconciled as today |

**D14.2 (#175).** **Operator override.** An explicit `unblock` (REMOTE) sets
`validation_override_seq` to the highest record seq, in the unblock's own
write.

- A non-PASS record at or below the watermark is historical, no longer
  terminal, so the next gate re-validates the same candidate. PASS records
  keep their applicability.
- It is a human decision, shown in `status` and the run log. Automatic
  recovery never sets it.
- LOCAL has no unblock; a new run starts with no records.

*Rationale.* A terminal FAIL protects an unattended run from re-running a
known failure; a human who fixed the environment (a missing tool, a full
disk) needs a way to ask for one more run without hand-editing state.

### 2.15 State protocol rule (item 15)

**D15.1 (#172).** **One migration.** Wave 2B makes exactly **one** state
protocol bump, owned by #172. That migration lands the complete persisted
schema of §2.10–§2.12 and the two small fields of D5.2 and D14.2 (D11.2).
#173, #174 and #175 populate fields #172 defined and add none.

**D15.2 (#172).** **Numbering and independence.** The number is assigned
when #172 is ready to merge, never reserved. On `main` `1412765` the
protocol is `7`: #160's bump took 5 → 7, and #23 reserves 6 for #126. The
Wave 2B bump is separate from #126 / #134 and from #160's. Whichever lands
later rebases its migration rule onto the earlier one, and they are never
combined.

**D15.3 (documentation only).** **Announcement.** The bump is announced on
#23 and #158 before #172 merges. #170's planning posted the coordination
note on #23.

**D15.4 (#172).** **Legacy files** load in every phase with empty Wave 2B
fields. The gate rules (D12.3, D13.2, D14.1) decide what an empty field
means. Loading never fabricates a record or a certificate.

**D15.5 (documentation only).** **A later field.** A child that finds
another persisted field unavoidable must justify it, announce it, and either
fold it into #172's migration before that merges or make its own bump.

### 2.16 Crash windows (item 16)

| Window | Rule | Owner |
|---|---|---|
| candidate selected, nothing launched | resume re-reads the candidate and validates | #173 |
| marker written, validator running | the engine's resume path (never load) records `INTERRUPTED` (historical) and reruns; more than 2 interrupted attempts for the same (C, definition, P) → `BLOCKED` | #172 |
| validator exited, record not written | as above: the outcome was never observed durably, so it is rerun, never assumed | #172 |
| record written, marker not cleared | impossible by construction (one write); a hand-made stale marker is cleared by the resume path, never by load | #172 |
| producer: PASS records written, no effect intent | ADR 0004 W1: no plan and no completion context were saved, because a phase persists its rendered payload only with its intents (ADR 0004 D8.4), so the agent is relaunched under the existing attempt accounting; its commit is still in the worktree, and the same candidate reuses the records without rerunning them | #173 |
| producer: a FAIL record written | the record and the counter are one write (in `REPLAN_REEXECUTE`, the record and the rejection, D5.3); terminal for (C, definition, P); the next step relaunches until the bound, then `BLOCKED`; never PASS by recovery | #173 |
| REVIEW entry: binding written, reviewer not launched | relaunch with the same binding, with no new validation | #173 |
| comment published (#162), state not saved | #162's journal completes the round; the certificate is issued from the journal-observed identity, with no relaunch and no new comment | #174 |
| certificate written | it was written atomically with the transition, so there is nothing to reconcile | #174 |
| candidate moved during validation | the records stay true for C; the pre-launch HEAD check routes to `REVIEW` for the new head | #175 |
| export directory left behind by a kill | it is not evidence and is never read again; each materialization uses a fresh directory, and a leftover is ordinary temporary-directory debris, as `autoforge-premerge-*` is today | #171 |

### 2.17 Provider neutrality and CI (item 17)

**D17.1 (documentation only).** **No `if provider ==` anywhere in evidence
semantics.** The provider may appear in metadata only.

**D17.2 (each child, for its own decisions).** **Tests reuse #155's parity
method** across the Claude Code, OpenCode and Pi fakes, and the SDK fakes
when they exist.

**D17.3 (documentation only).** **GitHub CI stays a live, separate source of
truth.** No check run is copied into a record, and no local evidence is
published as a GitHub status.

### 2.18 `tests_passed` and `tests_attempted` (item 18)

**D18.1 (documentation only).**

- They remain agent claims.
- #164 keeps using `tests_passed=false` to reject a replacement before
  publication.
- Neither is ever evidence.
- Removing them is not part of Wave 2B and is not created.

## 3. What does not change

- **`CONTROL_RESULT` stays untrusted,** and no phase's schema gains an
  evidence field (D9.3, D18.1).
- **Controller-owned effects** (ADR 0004, #160–#164) keep their kinds,
  records, reconciliation and transport. No evidence is stored in an effect
  record; the effect journal is not turned into a workflow journal.
- **Legal edges are unchanged.** No `VALIDATE` phase and no new edge (D5.8).
- **`merge.verification_commands`** runs at every gate pass with its cache
  rule (D3.3); only its export changes (D6.6).
- **LOCAL makes zero GitHub calls,** and the run contract is unchanged
  (D3.2).
- **The dry run** writes nothing and launches no validator; its plan lists
  the definitions and reports a pending marker (D11.3).
- **Redaction, output bounds and the sandbox decisions** of ADR 0005 are
  unchanged, except that D8.2 and D8.3 narrow ADR 0005 D16.1 and D17.1 for
  validation records.

## 4. Composition with other decisions

- **ADR 0001 (LOCAL workspace identity).** Amended by D7.4: §5.10 gains a
  dated note. Rules W, P, R, S, C, V and G are unchanged; the
  post-validation binding becomes the converged fingerprint (D7.2), and
  REVIEW still never binds.
- **ADR 0002 (executor).** Unchanged, and relied on. Validators run through
  the executor with `contain_orphans=True`; its leftover facts are copied
  into the record (D10.1).
- **ADR 0003 (Pi).** Unchanged. The rules are provider-neutral (D17.1).
- **ADR 0004 (authority boundary).** Composed. The producer gate sits
  between the candidate check and the first intent (ADR 0004 D4.1). The
  tree SHA is read under ADR 0004 D7.5. The crash window before the plan is
  ADR 0004 W1 (§2.16 here). The K4 read-back, or its record's observation,
  supplies the certificate's comment identity (D12.2 here).
- **ADR 0005 (outer sandbox).** Composed. The candidate export is the export
  launch class (ADR 0005 §2.5, D13.2); the producer's candidate is read and
  imported under ADR 0005 D6.3 and D6.5; P's template comes from ADR 0005
  D13.2's role-level inputs; the mode semantics are ADR 0005 D12.1 and
  D12.2. Narrowed for validation records only: D8.2 persists the copied
  sandbox facts and D8.3 reads one of them, only to withdraw evidence
  (ADR 0005 D16.1, D17.1).
- **#23.** The one Wave 2B bump (D15.2).
- **#126 / #134.** Unrelated, except for protocol numbering.

## 5. Decision ownership

Each decision has exactly one implementing child, or is documentation only.
No decision re-implements #160–#169: the effect records and reconciliation
(#160), the publication of each phase (#161–#164), the comment rendering
(#162), the credential policy (#165), the sandbox decisions (#166–#169) and
the parity work (#155) are consumed, not changed.

| Child | Decisions |
|---|---|
| #171 | D6.1–D6.4, D6.6; §2.16's export row |
| #172 | D2.1–D2.3, D2.6, D2.7, D3.1, D3.2, D3.5, D3.6, D4.2, D5.6, D7.1, D7.2, D8.1–D8.3, D8.5, D8.6, D10.1–D10.6, D11.1–D11.4, D12.1, D15.1, D15.2, D15.4; §2.16's marker rows |
| #173 | D2.4, D5.1–D5.5, D5.7, D9.1–D9.5; §2.16's producer and REVIEW-entry rows |
| #174 | D1.3, D12.2, D12.3, D12.5; §2.16's comment and certificate rows |
| #175 | D12.4, D13.1, D13.2, D14.1, D14.2; §2.16's moved-candidate row |
| documentation only | D1.1, D1.2, D2.5, D3.3, D3.4, D4.1, D5.8, D6.5, D7.3, D8.4, D9.6, D13.3, D15.3, D15.5, D17.1, D17.3, D18.1 |
| this record | D7.4 (the ADR 0001 note) |
| each child | D17.2, for its own decisions |

### 5.1 Where this record differs from #170's body

- **Line numbers** are re-read on `main` `1412765` (§1.1).
  `_verify_review_comment` no longer exists: #162 replaced the agent-posted
  comment with the K4 record (`_apply_review`, `_complete_review`).
- **The REVIEW result ignores unknown keys** (`result_parser.py` :1091,
  :1656), so an agent-written evidence field is ignored, never refused
  (D9.3).
- **The comment identity** the certificate binds is the URL, the id and the
  marker key; state keeps only the URL after the commit save, so #174 takes
  the identity from the read-back or the K4 record's observation in the
  issuing write (D12.2).
- **The producer crash window** that #170 left conditional on #159 item 8 is
  resolved by ADR 0004 D8.4 and W1: the agent is relaunched, and the same
  candidate reuses its records (§2.16).
- **#160's bump has landed** as protocol 7, with 6 reserved for #126 (D15.2).
- **`workspace_policy_digest`** is the run contract's
  `WorkspacePolicy.digest()`; the contract has no field of that name (D2.6).
- **There is no `sandbox.mode` on `main`;** until #168 lands, P's mode is
  `disabled` with the fixed "no outer sandbox" template (D8.1).
- **#170 describes the producer candidate as read through the common dir and
  strictly imported, which is #168's.** Wave 2B does not wait for #168:
  until it lands, the gate validates the SHA the phase's check read with
  `git -C <worktree>` (and re-hashed, ADR 0004 D7.5), agents write the
  shared store directly, and provenance records that no import ran (D2.3,
  D2.4, D6.1). The gate itself never reads `HEAD`, so #168 changes the read
  and adds the import without changing the gate.
- **#170 says the issue worktree is untouched by validation in REMOTE.**
  That holds under an enforced sandbox. Under `disabled` a validator can
  reach it by absolute path; what holds in every mode is that validation
  cannot modify C (D6.4).
- **#170 calls the copied enforcement facts "displayed only",** while also
  deciding that a `required` mode needs a record showing enforcement. D8.3
  keeps both by narrowing ADR 0005 D16.1 and D17.1 for validation records
  only, and says so.
- **One export per definition launch** (D6.2), which #170 left open; it
  follows ADR 0005 §2.5. The export location is the one #168 chooses (ADR
  0005 D8.4), `mkdtemp` until then.
- **LOCAL REVIEW writes the `review_evidence` binding** (D5.7), which #170
  assigned to REMOTE only, so that a LOCAL certificate has its input.
- **The attempt marker** is written after every check that can refuse a
  launch, and a launch failure leaves none behind (D11.2), so that only a
  crash produces `INTERRUPTED`.
- **The writer lists trees too** (`ls-tree -r -t`), so an empty subtree is
  exported and proved (D6.2).
- **The configuration bounds no argv today;** #172 adds the bounds a record
  needs (D10.1).
- **A legacy entry mode** fails the proof, measured (§1.2, D6.3).
- **#170 calls a materialization failure inconclusive** without saying
  where the run goes. D10.5 gives it one fresh attempt, then `BLOCKED`, and
  sends a candidate missing from the shared store down the phase's existing
  candidate refusal.
- **`FAIL` covers a termination by a signal** the executor did not send
  (D10.4).
- **#170 says both that a sandbox setup failure leaves state unchanged
  (decision 8) and that a launch or setup failure ends in `BLOCKED`
  (decision 10).** D8.5 separates the two: a refusal at #169's pre-launch
  gate leaves state unchanged; a setup failure at a launch the gate
  allowed, or a #165 preflight refusal, is a launch failure under D10.5.
- **The LOCAL walk count** is stated: one walk before the first command and
  one after each (D7.2).

## 6. Tests the implementing children must add

Every test is deterministic and offline: fake providers and fake GitHub
(`ExplodingGitHub` where nothing may be written), `tmp_path` repositories,
fake filter programs, and no paid provider or model call.

**#171 (materializer and proof):**

- Byte-exactness under hostile configuration: a candidate `.gitattributes`
  with `* text eol=crlf`, `ident` and a filter, the repository's
  `info/attributes`, `core.attributesFile`, `core.autocrlf=true` and a
  configured smudge program that would create a sentinel. Every exported
  file equals `git cat-file blob` byte for byte, and the sentinel is never
  created.
- Modes `0644` and `0755`; a symlink holding absolute, relative,
  `..`-escaping and dangling link text, never followed; a gitlink exported
  as an empty, counted directory whose commit is never read (absent from
  the store); an empty subtree; no `.git`; binary and invalid-UTF-8 blob
  content; a name that is not valid UTF-8.
- Refused names (`.`, `..`, `.git`, `.GIT`, a `/` or NUL), constructed by
  writing tree objects directly, refused before anything is written below
  them.
- The proof, in sha1 and sha256 repositories: it passes on a faithful
  export, and fails as a materialization failure, never a record, on an
  extra file, a changed byte, a flipped executable bit, a symlink replaced
  by a file, a non-empty gitlink directory, a simulated case-folding
  collision (writer fault injection) and a legacy `100664` entry.
- A producer candidate absent from the shared store is refused, not
  retried; at REVIEW entry a failed fetch is inconclusive and read again on
  `resume`.
- Replacement refs and a forged commit-graph cannot change what is
  exported.
- The merge gate's export uses the same materializer with the
  `autoforge-premerge-` prefix; the existing pre-merge tests still pass; the
  export is deleted after a pass, a failure and an exception.

**#172 (definitions, policy, records, persistence):**

- Configuration: the key is empty by default, accepts argv lists only, and
  refuses more than 16 definitions and an argv beyond the bounds; no
  configuration can name a scope other than `candidate-tree` (with a scope
  field, any other value is refused).
- Digests: `definition_digest` and `validation_set_digest` are stable
  across processes and change with the argv and the index;
  `policy_digest` is equal for two exports of the same candidate and
  changes with the mode, the template, the allow-list, the timeout,
  `contain_orphans` and the materialization method.
- Records: round-trip; a digest mismatch, a kind mismatch, a missing
  referenced record and the other mode's kind are refused on load; bounds
  (the tail, the record count, eviction oldest first except pinned records).
- Output safety: a secret straddling the tail cut is redacted on the full
  text first; control characters are escaped; invalid UTF-8 is replaced.
- Outcomes: exit 0 is PASS; exits 1, 126 and 127 and a self-inflicted
  signal are FAIL; a timeout is TIMEOUT; a launch or setup failure and a
  preflight refusal leave no record and block after the bounded re-check;
  the set stops at the first non-PASS; applicable PASS and terminal records
  are reused without a launch; each definition launch gets a fresh, proved
  export, and a file the first definition writes is absent for the second.
- The marker: written after the gate, the proof and the preflight, and
  cleared in the appending write; a refusal by any of the three writes no
  marker, and a launch or setup failure leaves none behind; load leaves the
  file byte-identical and `status` reports a marker a crash left; resume
  converts it to `INTERRUPTED` and reruns; a third interruption of the
  same key blocks.
- LOCAL convergence: a validator that writes a covered file
  deterministically (F0 → F1, then the same bytes again from F1) converges
  after one confirming run, and F1 is bound; one that writes a timestamp
  does not converge and follows the bounded relaunch to `BLOCKED` with the
  remedy named; a FAIL record is never evidence.
- Migration: a legacy file of every phase loads with empty Wave 2B fields;
  a file of the new protocol missing one is refused.

**#173 (gates and the evidence section):**

- The producer gate in `ANALYZE_EXECUTE`, `FIX` and `REPLAN_REEXECUTE`: a
  non-PASS records no intent, pushes nothing and opens nothing; the record
  and the counter are one write; the relaunch prompt carries the failure
  section with fenced tails; the same candidate reuses its terminal record
  and counts; the bound blocks, naming the candidate, the definition and the
  record; the counter is cleared on a phase change and at the next issue;
  the replan takes the `tests_passed=false` rejection path.
- A crash injected after the records and before the plan save leaves no
  effect record, and the relaunch reuses the records with no validator
  launch.
- REVIEW entry: the normal path launches no validator; a human push is
  validated after an objects-only fetch that creates no ref; a non-PASS is
  presented; journal-first completion validates nothing.
- The evidence section: `escape_inline` for argv, `fenced_untrusted_block`
  for tails, provenance only for PASS, the empty-set wording, and a
  hostile tail (marker openers, a fake `CONTROL_RESULT`, fence breakers)
  stays inside its fence.
- A REVIEW result carrying an evidence key: the key is ignored. A clean
  result with a non-PASS presented is corrected; on exhaustion it is
  `ControlResultValidationError` with nothing published.
- LOCAL REVIEW with a non-empty set and no evidence blocks before any
  launch; with an empty set it proceeds.
- Parity across the Claude Code, OpenCode and Pi fakes (D17.2).

**#174 (certificates):**

- The certificate and the transition are one write: a crash injected around
  it leaves both or neither, for `REVIEW → READY_FOR_MERGE` and LOCAL
  `REVIEW → DONE`.
- A round completed by #162's journal issues the certificate from the
  observed identity, with no relaunch and no second comment.
- Issuance is refused while a presented record is not PASS or not
  applicable.
- The unblock of an OPEN PR at the clean-reviewed revision with no
  applicable certificate decides `REVIEW`; with one, `READY_FOR_MERGE`.
- LOCAL `DONE` requires a certificate.
- No prompt and no published payload contains a certificate field.

**#175 (drift, recovery and MERGE):**

- The certificate check runs after the identity and revision checks and
  before the readiness reads: a failing certificate launches no
  verification command.
- Each requirement of D13.2 failing alone blocks, naming it; HEAD, base and
  merge-base drift still route to `REVIEW`.
- Each row of D14.1's matrix, including a definition or policy change at
  the gate (`BLOCKED`, then unblock to `REVIEW`), legacy state in
  `READY_FOR_MERGE` / `MERGE` with an OPEN PR, and a replan replacement
  that inherits nothing.
- A MERGED PR is reconciled and counted as today, with or without a
  certificate.
- The override: set only by `unblock`, shown in `status`; a non-PASS at or
  below the watermark is re-run, a PASS keeps its applicability; automatic
  recovery never sets it.
- `merge.verification_commands` still runs at every gate pass when a
  certificate exists.

## 7. Non-goals

- No production code, tests, prompts or configuration in this change.
- No `VALIDATE` phase or new legal edge.
- No evidence stored in #160's effect records.
- No re-creation of #161–#164's publication, #162's comment rendering,
  #165's credential policy, #166–#169's sandbox decisions or #155's parity
  work.
- No unification of `merge.verification_commands` with candidate
  validation.
- No base-bound or merge-result-bound validation.
- No publication of evidence to GitHub.
- No artifact store or blob store.
- No parallel issue scheduler, EPIC dependency executor, scheduler claims,
  file-scope locks, multi-repo daemon, dashboard, distributed workers, audit
  timeline or history UI, event sourcing, remote validation farm, build
  cache service or network-egress proxy.

## 8. Consequences

- **A REMOTE candidate that fails a configured validator never reaches
  GitHub,** and a run whose agent cannot make it pass stops at `BLOCKED`
  after a fixed number of relaunches instead of looping.
- **A clean review can be merged only with the controller's own evidence
  behind it,** bound to the exact PR, revision, comment, definitions and
  policy; any of them changing withdraws it.
- **Validators see committed bytes,** in candidate validation and now also in
  the merge gate's export: no eol conversion, `ident`, filters, LFS content,
  git metadata or submodules. A command that needs any of them fails there;
  the documentation warns of the last two today, and #171 adds the rest.
- **The operator's filter drivers no longer run on candidate content** at the
  merge gate (D6.6).
- **A tree with a legacy entry mode cannot be validated** (§1.2): the proof
  fails and the run blocks, naming the entry.
- **LOCAL walks the tree once more per validation command,** and a validator
  that writes covered files nondeterministically now blocks with a remedy
  instead of being bound.
- **Changing `execution.command_timeout_seconds`, the allow-list or the
  sandbox mode after a clean review** blocks the merge gate until it is
  restored or a new round runs.
- **Validation is not hermetic and not reproducible;** a record says where
  and how it ran, and the documentation says what that does not prove.
- **Documentation follows behaviour** in the implementing children:
  `docs/agent-guides/state-and-recovery.md`, `github-safety.md`,
  `workflow.md`, `control-result-protocol.md`, `docs/configuration.md`,
  `docs/local-mode.md`, `docs/usage.md` and `autoforge.example.yaml`. If
  implementation contradicts a decision here, the implementing child amends
  this record, with a recorded rationale.
