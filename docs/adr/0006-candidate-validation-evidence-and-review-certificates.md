# ADR 0006. Candidate validation evidence and controller review certificates

- **Status:** accepted, and implemented by #171 (the byte-exact REMOTE
  materializer and its hash-back proof, also used by the merge gate's
  export), #172 (validation definitions, execution policy, durable
  `ValidationRecord`s, LOCAL convergence, and the one state migration),
  #173 (the producer gates, the REVIEW entry gate and the REVIEW evidence
  section), #174 (review certificates, entering `READY_FOR_MERGE` and LOCAL
  `DONE`) and #175 (drift, recovery, the operator override, and the
  certificate check in the merge gate's `READY_FOR_MERGE` and `MERGE`
  passes).
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
  definition against one candidate under one policy, or, for an attempt a
  crash interrupted, what it was about to launch (D10.1, D10.7).
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

The statement covers the outcomes the controller observed: `PASS`, `FAIL`
and `TIMEOUT`. An **`INTERRUPTED`** record (D10.7) means exactly:

> AutoForge was about to launch validator X, as defined by trusted
> configuration Y, against candidate C under execution policy P, and stopped
> before it observed a result.

It does not say whether the validator was launched, what it did or how it
ended. Y and P are the ones in force when the controller wrote the attempt
marker (D11.2), not the ones in force when the record was written.

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

Provenance is strings, an integer and booleans, bounded by D10.3, displayed
in the REVIEW evidence section (D9.1), and never compared.

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
  producer validated under the set and policy in force. It is presented to
  the reviewer (D9.1, D9.2), who cannot return a clean result over it
  (D9.4).
- **Relaunch:** a reviewer relaunched after a crash (the binding was
  written and no plan was saved) passes the entry gate again, under the
  configuration the relaunching process loaded, and after
  `_bind_review_head` has read the PR head again. A push made after the
  crash has therefore already moved the bound head, while the upcoming
  round stays the same: only an applied review advances it.
- **Reusing a persisted binding:** at every entry, a relaunch included,
  the persisted binding is reused unchanged, and nothing reruns, only when
  all five hold:
  - its review round is the upcoming round;
  - its candidate's commit is the head just bound;
  - its `validation_set_digest` (D11.2) equals the one in force;
  - its `policy_digest` (D11.2) equals the one in force;
  - no record it names is a `FAIL` or `TIMEOUT` at or below the override
    watermark `validation_override_seq` (D14.2). A `PASS` record never
    breaks this condition, since the override leaves a `PASS` applicable.
- **Any mismatch:** the binding is not evidence for this launch. The gate
  requires evidence for the bound head under the configuration in force,
  exactly as at a first entry (applicable records are reused, D10.6, and a
  head no producer validated is validated now), and replaces the binding
  in the save that charges the launch:
  - **Head drift, configuration unchanged:** a crash after the binding for
    C1 was saved, then a human or external push of C2. C1's records are
    not applicable to C2 (§1.4) and no producer validated C2, so a
    non-empty set is validated on C2 after an objects-only fetch, and an
    empty set runs nothing. The new binding names C2 and C2's records,
    none for an empty set. Reusing C1's binding would present records the
    evidence section refuses as inapplicable (D9.1), or, with an empty
    set, where no record contradicts it, carry a binding for C1 into an
    issuance for C2, which D12.2 blocks as an invariant violation.
  - **Another round:** a binding an earlier round left, for example after
    a FIX that pushed nothing, so that the same candidate is reviewed
    again. Its applicable records are reused with no launch, and the new
    binding names the upcoming round.
  - **Configuration drift:** the set or the policy differs. A non-empty
    set is validated again under the configuration in force.
  - **Operator override:** an entry presented a `FAIL` or `TIMEOUT` (the
    non-PASS case above), and the round ended in `BLOCKED` without
    completing (the reviewer reported `blocked`, for example), so
    the round, the candidate and the configuration are all unchanged. The
    operator repairs the environment and unblocks to `REVIEW`, and the
    unblock's watermark covers the failing record (D14.2). D10.6 then
    reuses the `PASS` records before the overridden definition with no
    launch, runs that definition again and continues the set, and the new
    binding names the upcoming round and those records. Reusing the old
    binding would run nothing and present the reviewer the failure the
    operator overrode.
- A reviewer is therefore never launched under a binding of another round
  or another candidate, of a set or policy the launching process does not
  hold, or naming a failure the operator overrode.
- **Journal-first completion:** a REVIEW that completes from a persisted
  plan (#162) launches no reviewer and validates nothing; its binding was
  written before the reviewer whose result it completes, and the
  certificate is issued from that binding, never from the configuration in
  force (D12.2, D12.6). An override written after the plan was saved
  changes nothing there: the reuse rule above applies only where a
  reviewer is to launch, the reviewer already gave its verdict on the
  records the binding names, and a binding naming a non-PASS never yields
  a certificate (D9.4, D12.2).

**D5.6 (#172).** **LOCAL write phases** (`ANALYZE_EXECUTE`, `FIX`):
validation stays where it is (`_verified_snapshot`), now producing records,
under the convergence rule (D7.2).

**D5.7 (#173).** **LOCAL REVIEW** runs no validation and binds no
fingerprint (ADR 0001 §5.10). It requires evidence for the bound
fingerprint under the run contract's set (an empty set is complete), and
writes the `review_evidence` binding (D11.2) naming that evidence in the
save that charges the reviewer's launch, as D5.5 does in REMOTE, a
relaunch included. D5.5's reuse rule applies, with the `LocalCandidate` of
the bound fingerprint (D2.6) compared in place of the PR head; its
override condition always holds, since LOCAL has no unblock and writes no
watermark (D14.2). On a mismatch the binding is rewritten from the
evidence the write phase recorded, never by validating. The binding names
records, never a fingerprint, so §5.10's rule holds. A LOCAL state with a
non-empty set and no evidence, which only a state persisted before Wave 2B
can be, enters `BLOCKED` with "no validation evidence for this LOCAL run;
start a new local run", before any launch.

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
restored or a new round runs (D14.1). So does the certificate issuance of a
round that completes from #162's journal after the edit (D12.6).

**D8.2 (#172).** **Each record stores** `policy_digest`, the mode and the
timeout, and the **enforcement facts** copied from the in-memory
`ExecutionResult.sandbox` (`SandboxFacts`) the executor returned for that
launch: enforced or not, the backend and its version, the network position,
and the teardown fact. They are strings and booleans bounded by D10.3, and
nothing else of `SandboxFacts` is copied (not `tmpfs`, `unset_env` or
`setup_failure`). They are displayed (D9.1, `status`), and read by no
decision except D8.3's second rule. A launch with no description records
`enforced: false` and no backend.

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

**D10.1 (#172).** **Fields** (bounded by D10.3, typed, no free-form
dictionary). The last column names the shape a field belongs to:

| Field | Content | Shape |
|---|---|---|
| `schema` | integer record schema version (1) | both |
| `seq` | per-state, monotonically increasing integer | both |
| `digest` | SHA-256 over the canonical encoding of every other field | both |
| `candidate` | `RemoteCandidate` or `LocalCandidate` (D2.1, D2.6) under its `kind`, plus its provenance (D2.3) | both |
| `definition` | set index, redacted argv, `definition_digest`, scope | both |
| `policy` | `policy_digest`, mode, timeout seconds | both |
| `outcome` | `PASS`, `FAIL` or `TIMEOUT` (observed); `INTERRUPTED` (interrupted) | both: the discriminator |
| `run_log` | the relative run-log name of the step that launched the validator or wrote the marker: informational, never read for authority | both |
| LOCAL `start_fingerprint` | the fingerprint the walk before the command read (D7.2) | both |
| `enforcement` | the copied facts (D8.2) | observed |
| `exit_code` | integer as the executor reported it, or null (`TIMEOUT`) | observed |
| timing | `started_at`, `finished_at` (UTC, ISO 8601), as the executor reported them | observed |
| leftovers | ADR 0002's facts: descendants killed, group survived the kill, orphans killed, orphan survived, orphans unchecked | observed |
| output | `stdout_tail`, `stderr_tail`, `stdout_bytes`, `stderr_bytes`, and `tail_truncated` per stream | observed |
| LOCAL `end_fingerprint` | the fingerprint the walk after the command read (D7.2) | observed |
| `marked_at` | when the attempt marker was written (UTC, ISO 8601) | interrupted |
| `recovered_at` | when the resume path wrote the record (UTC, ISO 8601) | interrupted |

A record has exactly one of two shapes, chosen by its `outcome`. An
**observed** record (`PASS`, `FAIL`, `TIMEOUT`) holds the fields marked
"both" and "observed". An **interrupted** record (`INTERRUPTED`, D10.7)
holds the fields marked "both" and "interrupted". The two shapes share no
optional field, as the candidate kinds share none (D2.7). Parsing refuses a
record that lacks a field of its shape or carries a field of the other. A
LOCAL field appears exactly in a `local` record.

**D10.2 (#172).** **Output safety.** Raw bytes are processed in this order:

1. they are captured under the executor's byte caps;
2. they are decoded with replacement characters;
3. they are redacted (`redaction.redact`) on the full captured text;
4. control characters other than newline and tab are escaped;
5. the tail is cut to its bound (D10.3): the longest suffix that fits, so a
   cut never splits an escape;
6. the truncation facts are recorded.

**D10.3 (#172).** **Bounds.** This decision is the one bound definition for
every Wave 2B persisted field: the records, the attempt marker, the
`review_evidence` binding, the failure counter, the override watermark and
the certificate (D11.2, D12.1). #172 defines each bound once, as a named
constant beside the schema (as `effects.py` defines the effect record's),
and the writers and the loader use the same constants. #172 may lower a
value before its migration merges, never raise one silently. Once the
migration has merged, the bounds are part of the state protocol, so
lowering one is a protocol change (D15.5).

*Lists:*

| Value | Bound |
|---|---|
| definitions per set (`validation.commands`, `local.validation_commands`) | 16 |
| `validation_records` | 64 records (D11.4 evicts) |
| record seqs and digests in the binding or the certificate | 16 pairs, one per definition of the set |
| elements of an argv | 32 |

*Strings:*

| Value | Bound |
|---|---|
| an argv element as configured | 1,024 characters, non-blank (as today) |
| an argv as configured | 4,096 characters, summed over its elements |
| the redacted argv a record or marker holds | 3,072 characters per element and 12,288 in all: the configured bounds times redaction's `MAX_GROWTH_FACTOR` (3), as `MAX_STORED_RATIONALE_CHARS` is derived |
| `stdout_tail`, `stderr_tail` | 2,000 characters each as stored, after escaping (D10.2); today's `VerificationError` tail |
| the repository (`owner/name`) | 200 characters, under `effects.py`'s repository rule |
| a PR URL (`pr-head` provenance, `reviewed_pr_url`) | 512 characters (`MAX_URL_CHARS`); parsed by `parse_pr_url`, in the candidate's repository |
| the review comment URL | 512 characters; parsed by `parse_comment_url`, on the certificate's PR |
| `reviewed_base_ref` | 255 characters, under `effects.py`'s ref rule |
| the worktree id | 255 characters, one path component: no `/`, NUL or control character, and not `.` or `..` |
| the LOCAL run id | `RUN_ID_RE` (at most 128 characters) |
| `run_log` | 256 characters: `<run id>/<step directory>`, under `RUN_ID_RE` and `STEP_DIR_RE` |
| `backend`, `backend_version` | 64 characters each |
| `teardown` | 128 characters |

*Fixed formats and enumerations:*

- a commit, tree or base SHA: full lower-case hex of the length of the
  candidate's `object_format` (40 or 64);
- every digest and fingerprint (`digest`, `definition_digest`,
  `policy_digest`, `validation_set_digest`, `workspace_policy_digest`, the
  LOCAL fingerprints): 64 lower-case hex characters;
- every timestamp: an ISO 8601 UTC time as `utcnow_iso` writes it, at most
  32 characters;
- `kind` (`remote`, `local`), the provenance kind (`worktree`, `pr-head`),
  `object_format` (`sha1`, `sha256`), the scope (`candidate-tree`), the mode
  (`disabled`, `preferred`, `required`), `network` (`host`, `isolated`, or
  empty when there is no backend), `outcome`, and the counter's phase
  (`ANALYZE_EXECUTE`, `FIX`).

*Integers and booleans:* `schema` is 1; a set index is 0 to 15; the timeout
is 1 to `MAX_DEADLINE_SECONDS` (`execution.command_timeout_seconds`' own
range); `exit_code` is -128 to 255 (a signal is reported negated); a `seq`,
a review round and an issue number are 1 to `2**31 - 1`; the watermark is
0 to `2**31 - 1`, 0 being an override with no record to override (D14.2); a
comment id is 1 to `2**53 - 1`, and a byte count 0 to `2**53 - 1`; the
counter's count is 1 to its bound (3, D5.2). Booleans are JSON booleans,
never strings or integers.

**Writers.** A writer never produces a value the loader would refuse.

- An argv beyond its bounds is refused where it enters the controller:
  `validation.commands` and `local.validation_commands` at configuration
  load, so a new run contract never holds one. A LOCAL run contract persisted
  before #172 is checked before its first launch. An argv beyond the bounds
  there is refused before any marker: no record, no re-check (it could not
  change the answer), and `BLOCKED` naming the definition and the bound. A
  record therefore never truncates an argv. `merge.verification_commands` is copied into no record
  and is not bounded by this decision.
- The identifiers and URLs come from values the controller has already
  validated (a SHA read from the store, `current_pr_url`, the run id, the
  K4 read-back); one outside its format is refused before the marker, with
  no record, never clipped.
- The output tails are cut by D10.2, and `tail_truncated` says so.
- The enforcement text facts are display-only (D8.2). One longer than its
  bound is clipped with an ellipsis by `SandboxFacts`' existing rule, which
  already clips `backend_version` at 64 characters.
- `validation_records` is evicted to its bound (D11.4) in the write that
  appends.
- The watermark is written only by D14.2's rule: the highest retained
  record seq, or 0 when no record is retained.

Nothing else is ever clipped.

**Loading.** A persisted value outside its bound, format or enumeration, a
list longer than its bound, a field of the wrong type, or a record of the
wrong shape (D10.1) is corruption. The load raises `StateError` naming the
field and the bound, like a digest mismatch (D11.1). Nothing is clipped,
padded, defaulted or dropped to make a file fit. `run` refuses the file
unless `--force` quarantines it, as for any corrupt state file today, and
every other command that loads it reports the error.

**Total.** Counted at its bounds, one record is under 20,000 characters,
most of them the redacted argv and the two tails. The 64 records, the
marker, the binding, the counter, the watermark and the certificate
therefore stay under 1.5 million characters: under 18 MiB of
`ensure_ascii` JSON at 12 bytes a character, the worst case
`MAX_EFFECT_STATE_CHARS` is counted with. Beside the effect part's 12 MiB,
that keeps a state file within `MAX_STATE_FILE_BYTES` (64 MiB). #172 pins
the relation in a test.

**D10.4 (#172).** **Outcomes.**

- `PASS`: the launched command exited 0.
- `FAIL`: any other exit status of a launched command, 126 and 127
  included, and a termination by a signal the executor did not send.
- `TIMEOUT`: the executor's timeout ended it.
- `INTERRUPTED`: not an observed outcome. Only the resume path writes it,
  from a leftover marker, in the interrupted shape (D10.7, D11.3).

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
record (D10.4), a `FAIL` or `TIMEOUT` above the override watermark
(D14.2), is reused and ends the set; otherwise the definition runs, so a
`FAIL` or `TIMEOUT` the watermark made historical is run again.
The set stops at the first non-PASS, and the definitions after it get no
record.

**D10.7 (#172).** **The interrupted record.**

- **It is built from the attempt marker alone** (D11.2). The candidate and
  its provenance, the definition (set index, redacted argv,
  `definition_digest`, scope), the policy (`policy_digest`, mode, timeout),
  the LOCAL `start_fingerprint`, `run_log` and `marked_at` are copied from
  the marker. `seq`, `digest` and `recovered_at` are the resume path's own.
  Nothing is taken from the configuration in force at recovery, so a
  configuration changed after the crash cannot change what the record says.
  Nothing is read from `execution.json` or the run log.
- **It claims no launch.** It holds no enforcement facts, exit status,
  launch timing, leftovers, output or `end_fingerprint`, because none was
  observed. The crash may have come before the launch, while the validator
  ran, or after it exited, and the record does not say which (D1.1).
- **It is never evidence and never terminal.** D10.6 runs its definition
  again. No binding, certificate or counter names it, and it is rendered
  into no prompt (D5.4, D9.1). `status` shows it.
- **It counts toward §2.16's bound.** In the write that appends it, the
  resume path counts the retained `INTERRUPTED` records with the same
  candidate identity (not provenance), `definition_digest` and
  `policy_digest`, this one included. It enters `BLOCKED` when the count
  exceeds 2. A record at or below the override watermark (D14.2) is not
  counted. Eviction (D11.4) can only lower the count, which delays the
  `BLOCKED` and never makes evidence.

*Rationale.* A record holds only what the controller itself observed, in a
shape a loader can validate field by field. An interruption is recorded
with what the controller knew before the launch, because that is all it
knows. Redacting before the cut keeps a secret that straddles the cut from
surviving as a fragment. Escaping before the cut makes the bound the stored
length, which is what the loader checks.

### 2.11 Persistence: Option A, bounded strict state (item 11)

**D11.1 (#172).** **Everything lives in `state.json`:** the records, the
attempt marker, the REVIEW evidence binding, the certificate, the producer
failure counter and the override watermark. They are validated on load
like every persisted field. Corruption fails loudly, and referential
integrity is checked on load:

- a certificate, binding or counter that names a missing record, or an
  `INTERRUPTED` one (D10.7);
- a certificate or binding naming a record whose candidate identity (not
  provenance) or `policy_digest` differs from its own;
- a watermark above the highest retained record seq, or above 0 when no
  record is retained (D14.2);
- a digest mismatch;
- a record matching neither candidate kind, or the other mode's kind
  (D2.7);
- a value beyond its bound, or a record of the wrong shape (D10.1, D10.3).

Nothing is written outside the state file, so SafeRoot's atomic write
(`safefs.py`) is the only primitive needed. There is no blob store. Run
logs, transcripts and provider summaries keep their full output and are
never authority.

**D11.2 (#172).** **The persisted additions,** all owned by one migration
(D15.1):

- `validation_records`: a bounded list (D10.1, D10.3).
- `validation_attempt`: one optional marker holding every field an
  interrupted record copies (D10.7), and nothing observed: the candidate and
  its provenance, the definition (set index, redacted argv,
  `definition_digest`, scope), the policy (`policy_digest`, mode, timeout
  seconds), the LOCAL `start_fingerprint`, the step's `run_log`, and
  `marked_at`. It obeys the candidate-kind rule (D2.7).
  - It is written immediately before each launch: after the pre-launch
    capability gate, the materialization and its proof, and #165's
    preflight, so a refusal by any of them writes no marker.
  - Writing it is not a launch. The launch follows the write, so a crash
    between the two leaves a marker for a validator that never started;
    this is why an interrupted record claims no launch (D10.7).
  - It is cleared in the write that appends the record. When the executor
    reports a launch or setup failure instead (D10.5), it is cleared in the
    controller's next write: the re-check's own marker replaces it, or the
    `BLOCKED` write clears it. Only a crash leaves a marker for the resume
    path to find (§2.16).
- `review_evidence`: an optional binding {review round, candidate, ordered
  record seqs and digests, `validation_set_digest`, `policy_digest`},
  written before the reviewer launches (D5.5, D5.7).
  - `policy_digest` is P as the entry gate computed it for the launch the
    binding charges (D8.1). It is written for an empty set too, where no
    record carries it, and every record the binding names has it (D11.1).
  - The binding is the review's own record of the set and the policy it
    was presented under. The certificate takes both from it (D12.2), so a
    configuration changed after the reviewer launched cannot change what a
    certificate states.
- `review_certificate`: an optional `ReviewCertificate` (D12.1).
- `producer_validation_failure`: an optional counter {phase, count,
  candidate commit, last record seq} (D5.2).
- `validation_override_seq`: an optional integer, the operator's override
  watermark (D14.2). It is absent until an `unblock` writes it, and 0 when
  that unblock found no record.

Every field of every addition is bounded by D10.3.

**D11.3 (#172).** **Loading is read-only.** Load validates and never mutates.
Converting a leftover marker into an `INTERRUPTED` record (D10.7, from the
marker alone), and clearing a marker whose record exists, happen only in the
engine's resume and step path under the repository lock, in one atomic
write. `status`, `doctor` and the dry run only report a pending marker.

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
- the record seqs and digests of the presented set, `validation_set_digest`
  and `policy_digest`, all three copied from the `review_evidence` binding
  (D12.2);
- the clean result (`needs_fix_round: false`, zero actionable findings);
- `issued_at`.

No review prose is included. Every field is bounded by D10.3.

**D12.2 (#174).** **Issuance.**

- The certificate is written in the **same atomic state write** that
  completes the clean round: `REVIEW → READY_FOR_MERGE` (REMOTE) or
  `REVIEW → DONE` (LOCAL), or `REVIEW → BLOCKED` when the configuration
  drifted (D12.6).
- Its inputs are the validated clean result, the persisted
  `review_evidence` binding of this round, the review binding, and for
  REMOTE the comment identity from #162's read-back, or from the K4
  record's observation when the journal completes the round (the K4 record
  is dropped by that same save).
- Its candidate, record seqs and digests, `validation_set_digest` and
  `policy_digest` are copied from the binding, never computed from the
  configuration in force at issuance. With an empty set the binding is the
  only persisted copy of the policy the review was presented under (D11.2).
- Issuance requires the binding's review round to be the completed round
  and its candidate the reviewed one, and the records it names to be PASS,
  one per definition of its set, in set order: their `definition_digest`s
  encode to its `validation_set_digest` (D3.5), which for an empty set is
  the empty list's. D5.5 never launches a reviewer under a binding of
  another round or candidate, D9.4 refuses a clean result over a non-PASS
  record, and load checks each record's candidate and policy (D11.1), so a
  failure here is an invariant violation: no certificate, and `BLOCKED`
  naming the requirement.
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

**D12.6 (#174).** **Configuration drift at issuance.** In the issuing write,
the controller compares the binding's `validation_set_digest` and
`policy_digest` with the ones the configuration in force yields (D3.5,
D8.1).

- **Equal:** the write enters `READY_FOR_MERGE` (REMOTE) or `DONE` (LOCAL).
- **Different:** the write still issues the certificate from the binding,
  and enters `BLOCKED` instead, naming the digest that changed, as the merge
  gate refuses the same drift (D13.2, D14.1). The certificate states the
  set and policy the review was presented under, never the ones in force,
  and it is no permission (D1.2). The operator then either restores the
  configuration and unblocks, and D12.3 finds the certificate applicable
  and decides `READY_FOR_MERGE`, or unblocks under the new configuration,
  and D12.3 finds none and decides `REVIEW`: a fresh round, whose entry gate
  validates under the new configuration.
- **When it happens:** only when a REMOTE round completes from #162's
  journal in a later process than the one that launched the reviewer, and
  the configuration changed in between (§2.16). The configuration is
  loaded once per process, and a reviewer is launched only by a process
  that holds its binding's set and policy (D5.5's reuse rule). LOCAL has
  no journal: it applies a review in the process that launched the
  reviewer, so it compares equal by construction. A LOCAL difference would
  be an invariant violation and is `BLOCKED` all the same; LOCAL has no
  unblock, so the remedy is a new run.

*Rationale.* Writing the certificate with the transition leaves no state in
which the phase moved without one, or one exists for a phase that did not
move. Issuing it from the binding keeps issuance a pure function of
persisted inputs after any crash, empty set included, and routing on the
difference keeps a certificate from ever standing for a policy no review
was presented under.

*Rejected.*

- **Issuing from the configuration in force at issuance.** With an empty
  set no record would contradict it, so recovery would certify a policy no
  review was presented under, contrary to D8.1 and D14.1.
- **Issuing no certificate on drift.** A restored configuration would then
  still cost a review round, while the same drift found at the merge gate
  is cured by a restore (D14.1). The two are handled alike.
- **Relaunching the reviewer when a journal-completed round drifted.** The
  comment is already published; #162 completes a planned round without a
  reviewer, and a second verdict for the same round could contradict it.

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
| New commit, force-push, human push to the PR | inapplicable (another commit) | inapplicable | existing drift → `REVIEW`; the REVIEW entry, a reviewer relaunch included (D5.5), validates the new head and replaces a binding for the old one |
| Same tree, another commit | inapplicable (the commit is part of the key) | inapplicable | as above |
| Base advances, merge base unchanged | applicable | applicable (it binds the merge base, as today) | `BEHIND` is left to the existing readiness check |
| Base rewritten (the merge base moves) | applicable | inapplicable | existing stale → `REVIEW`; records reused, a new round required |
| Retarget (the base ref changes) | applicable | inapplicable | existing → `REVIEW` |
| Base-bound or merge-result-bound definition | n/a in Wave 2B (refused at load) | n/a | future |
| Definition set changes (configuration) | inapplicable | inapplicable | a producer gate or the REVIEW entry, a reviewer relaunch included (D5.5), validates under the new set; at certificate issuance (D12.6) or at the merge gate, `BLOCKED` naming the change, and the operator restores the configuration and unblocks (D12.3 decides `READY_FOR_MERGE`) or unblocks under the new one (D12.3 decides `REVIEW`; no automatic re-review loop) |
| Policy changes (sandbox mode, policy template, allow-list, timeout, materializer) | inapplicable | inapplicable | as for a definition change |
| Operator override (`unblock`, D14.2) | a `FAIL` or `TIMEOUT` at or below the watermark: historical, run again; a `PASS`: applicable | unchanged (it names `PASS` records only) | the next gate runs the overridden definition again (D10.6); at REVIEW entry a binding naming the overridden record is not reused and is replaced before the reviewer launches (D5.5) |
| Review comment edited, deleted or doubled | n/a | inapplicable | existing #94 re-read → `BLOCKED` |
| State hand-edited | load-time validation fails, or a digest mismatch | same | loud failure; never coerced |
| Replan replacement | no transfer: a new commit, new records | the old PR's certificate never matches the replacement PR | the replacement PR needs its own validation and review |
| LOCAL fingerprint drift | inapplicable | inapplicable | existing LOCAL drift handling |
| Host toolchain changes | not detected | not detected | out of scope: no reproducibility claim |
| Legacy state (before Wave 2B) in `READY_FOR_MERGE` / `MERGE`, OPEN PR | none | none | `BLOCKED` at the gate, with "unblock to `REVIEW`"; a PR already MERGED is reconciled as today |

**D14.2 (#175).** **Operator override.** An explicit `unblock` (REMOTE) sets
`validation_override_seq` in the unblock's own write: to the highest
retained record seq, or to 0 when `validation_records` is empty.

- An empty history is an ordinary case: the default empty set, a legacy
  state migrated with no records (D15.4), and a run blocked before any
  record was written (D10.5, an argv beyond its bounds in D10.3). Every seq
  is at least 1, so 0 makes no record historical; it records that the
  operator unblocked with nothing to override, and a later record is above
  it.
- Each later unblock sets it again by the same rule. The newest record is
  never evicted (D11.4 evicts oldest first, and pinned records stay below
  the bound), so the watermark never decreases within an issue and never
  exceeds the highest retained seq (D11.1).
- A non-PASS record at or below the watermark is historical, no longer
  terminal, so the next gate re-validates the same candidate. PASS records
  keep their applicability. An `INTERRUPTED` record at or below it no
  longer counts toward the interruption bound (D10.7).
- The unblock neither clears nor rewrites the `review_evidence` binding:
  whether it may be reused is derived at the next REVIEW entry (D14.1). A
  binding naming a `FAIL` or `TIMEOUT` at or below the watermark is not
  reused there (D5.5): the entry gate runs that definition again through
  D10.6, reusing the `PASS` records before it, and replaces the binding
  before the reviewer launches. The reviewer is presented the new records,
  never the overridden one.
- It is a human decision, shown in `status` and the run log. Automatic
  recovery never sets it.
- LOCAL has no unblock; a new run starts with no records.

*Rationale.* A terminal FAIL protects an unattended run from re-running a
known failure; a human who fixed the environment (a missing tool, a full
disk) needs a way to ask for one more run without hand-editing state.

*Rejected.* **Clearing the `review_evidence` binding in the unblock
write.** The unblock would become a second writer of the binding, and a
round that the same unblock sends to complete from #162's journal issues
its certificate from that binding (D12.2). Deriving the answer where the
binding is reused needs no staleness flag (D14.1), and the entry gate
already replaces a binding that fails a reuse condition.

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
| marker written, validator not yet launched or running | the engine's resume path (never load) records `INTERRUPTED` from the marker alone, claiming no launch (D10.7), and reruns; more than 2 interrupted attempts for the same (C, definition, P) → `BLOCKED` | #172 |
| validator exited, record not written | as above: the outcome was never observed durably, so the record claims none and the definition is rerun, never assumed | #172 |
| record written, marker not cleared | impossible by construction (one write); a hand-made stale marker is cleared by the resume path, never by load | #172 |
| producer: PASS records written, no effect intent | ADR 0004 W1: no plan and no completion context were saved, because a phase persists its rendered payload only with its intents (ADR 0004 D8.4), so the agent is relaunched under the existing attempt accounting; its commit is still in the worktree, and the same candidate reuses the records without rerunning them | #173 |
| producer: a FAIL record written | the record and the counter are one write (in `REPLAN_REEXECUTE`, the record and the rejection, D5.3); terminal for (C, definition, P); the next step relaunches until the bound, then `BLOCKED`; never PASS by recovery | #173 |
| REVIEW entry: binding written, reviewer not launched | relaunch through the entry gate, after `_bind_review_head` reads the PR head again: the same binding, with no new validation, only when its review round is the upcoming one, its candidate's commit is the head just bound, its set and policy digests equal the ones in force, and it names no `FAIL` or `TIMEOUT` at or below the override watermark; otherwise evidence for the bound head under the configuration in force and a replaced binding, in the save that charges the relaunch (D5.5). A push made after the crash (the binding names C1, the head is now C2) is validated under an unchanged configuration too, and the new binding names C2, with no record for an empty set | #173 |
| comment published (#162), state not saved | #162's journal completes the round; the certificate is issued from the journal-observed identity and from the binding's set and policy, empty set included, with no relaunch and no new comment; if the configuration in force yields another set or policy digest, the same write enters `BLOCKED` instead of `READY_FOR_MERGE` (D12.6) | #174 |
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
| #172 | D2.1–D2.3, D2.6, D2.7, D3.1, D3.2, D3.5, D3.6, D4.2, D5.6, D7.1, D7.2, D8.1–D8.3, D8.5, D8.6, D10.1–D10.7, D11.1–D11.4, D12.1, D15.1, D15.2, D15.4; §2.16's marker rows |
| #173 | D2.4, D5.1–D5.5, D5.7, D9.1–D9.5; §2.16's producer and REVIEW-entry rows |
| #174 | D1.3, D12.2, D12.3, D12.5, D12.6; §2.16's comment and certificate rows |
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
- **The `review_evidence` binding records `policy_digest`** (D11.2), which
  #170's binding lacked, and the certificate copies its set and policy from
  the binding (D12.2). With an empty set no record carries the policy, so a
  round completed from #162's journal after a configuration change could
  otherwise only certify the policy in force, which no review was presented
  under. Drift at issuance enters `BLOCKED` (D12.6), and a reviewer
  relaunch passes the entry gate again (D5.5), where #170 relaunched with
  the same binding unconditionally.
- **A persisted binding is reused only for its own round and candidate**
  (D5.5), as well as its own set and policy. #170's crash row reused it
  whatever the head: a push made while the controller was down would then
  have had one candidate's evidence presented for another's review, refused
  as inapplicable, or, with an empty set, blocked at issuance (D12.2). The
  relaunch validates the new head instead and replaces the binding.
- **A persisted binding naming a failure the operator overrode is not
  reused** (D5.5, D14.2). #170 made such a record historical but reused the
  binding regardless. A review whose entry presented a `FAIL`, and which
  ended in `BLOCKED` without completing, would then, after an unblock to
  `REVIEW` under the same round, candidate and configuration, run nothing
  and present the overridden failure again. The entry gate runs the
  overridden definition again (D10.6) and replaces the binding instead.
- **The override watermark is 0 when `unblock` finds no record** (D14.2).
  #170 set it to the highest record seq, which an empty history does not
  have, and D10.3's range now starts at 0.
- **The attempt marker** is written after every check that can refuse a
  launch, and a launch failure leaves none behind (D11.2), so that only a
  crash produces `INTERRUPTED`.
- **An `INTERRUPTED` record has a shape and a trust statement of its own**
  (D1.1, D10.1, D10.7). #170's marker held the candidate, two digests and a
  start time. A record of the full schema cannot be built truthfully from
  that: its argv and policy fields would come from the configuration in
  force at recovery, and its enforcement, exit, leftover and output fields
  were never observed. The marker now holds every field the interrupted
  record copies (D11.2). The record claims no launch, because a crash can
  come between the marker and the launch, and #170's `started_at` is now
  `marked_at`.
- **Every bound is named** (D10.3). #170 recommended three bounds and called
  the other persisted values bounded. D10.3 gives every variable-size value
  of every Wave 2B field a bound or a format, says what a writer and the
  loader do with a value beyond it, and shows that the total fits the state
  file's bound.
- **The output tail is cut after escaping** (D10.2), so its bound is the
  stored length; #170 cut before escaping.
- **The writer lists trees too** (`ls-tree -r -t`), so an empty subtree is
  exported and proved (D6.2).
- **The configuration bounds no argv today;** #172 adds the bounds a record
  needs (D10.3).
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
  refuses more than 16 definitions and an argv beyond the bounds (33
  elements, a 1,025-character element, 4,097 characters in all), in
  `validation.commands` and `local.validation_commands`; a LOCAL run
  contract persisted before #172 with an argv beyond them blocks before any
  marker; no configuration can name a scope other than `candidate-tree`
  (with a scope field, any other value is refused).
- Digests: `definition_digest` and `validation_set_digest` are stable
  across processes and change with the argv and the index;
  `policy_digest` is equal for two exports of the same candidate and
  changes with the mode, the template, the allow-list, the timeout,
  `contain_orphans` and the materialization method.
- Records: round-trip; a digest mismatch, a kind mismatch, a missing
  referenced record and the other mode's kind are refused on load; eviction
  is oldest first except pinned records.
- Bounds (D10.3): for each bounded value of a record, the marker, the
  binding, the counter, the watermark and the certificate, a file at the
  bound loads and a file one past it is refused with `StateError` naming the
  field, with nothing clipped; a malformed SHA, digest, timestamp or
  enumeration value is refused; an enforcement text fact longer than its
  bound is clipped with an ellipsis by the writer; the worst-case total
  stays under the state file's bound.
- Referential integrity (D11.1): a binding or certificate naming a record
  whose candidate identity or `policy_digest` differs from its own is
  refused on load; a binding with no `policy_digest`, for an empty set
  too, is refused; the watermark loads at 0 with no record and at the
  highest retained seq, and is refused at -1, above the highest retained
  seq, and above 0 with no record.
- Output safety: a secret straddling the tail cut is redacted on the full
  text first; control characters are escaped before the cut, the stored tail
  is at most 2,000 characters, and a cut never splits an escape; invalid
  UTF-8 is replaced.
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
  same key blocks, and interruptions at or below the override watermark are
  not counted.
- The interrupted record (D10.7): with `validation.commands`, the timeout
  and the allow-list changed between the crash and the resume, the record
  carries the marker's argv, definition and policy; a crash injected after
  the marker write and before the launch yields the same record; it holds no
  enforcement, exit, launch-timing, leftover, output or `end_fingerprint`
  field; an `INTERRUPTED` record carrying one, or an observed record missing
  one, is refused on load; it is never reused as evidence, and a binding,
  certificate or counter naming one is refused on load.
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
  presented; journal-first completion validates nothing; the binding
  carries the policy digest the gate computed, with an empty set too.
- Reviewer relaunch (a crash after the binding write, before any plan),
  with a non-empty and with an empty set: under an unchanged configuration
  and an unchanged PR head the binding is reused byte for byte and no
  validator launches; with the timeout or the sandbox mode changed, the
  binding is replaced under the new `policy_digest` in the save that
  charges the relaunch, and a non-empty set is validated again first.
- Reviewer relaunch with head drift: a crash after the binding for C1 is
  written and before any plan, then a human push of C2, with the
  configuration unchanged, with a non-empty and with an empty set. The
  entry binds C2 and does not reuse C1's binding. A non-empty set is
  validated on C2 after an objects-only fetch that creates no ref; an empty
  set launches nothing. In the save that charges the relaunch, before the
  reviewer launches, the binding is replaced by one naming the upcoming
  round, C2 and C2's records (none for an empty set). The evidence section
  presents C2's records with no applicability refusal, and the binding's
  candidate is the head the round reviews, as D12.2's issuance requires; a
  C2 that fails is presented as a non-PASS.
- A binding an earlier round left (a FIX that pushed nothing, so the next
  round reviews the same candidate) is not reused: the entry replaces it
  with one naming the upcoming round, reusing the records with no
  validator launch.
- A binding naming an overridden failure, for a `FAIL` and again for a
  `TIMEOUT`: an entry for a candidate no producer validated presents a
  `PASS` at index 0 and the non-PASS at index 1, and the reviewer reports
  `blocked`, so the round does not complete. The state then carries the
  watermark D14.2's unblock writes, with the PR head, the round, the set
  and the policy unchanged. The next entry does not reuse the binding:
  index 0's record is reused with no launch, index 1 is validated again,
  and in the save that charges the launch the binding is replaced by one
  naming the upcoming round, index 0's record and the new records. The
  evidence section presents the new records, never the overridden one; a
  new `FAIL` is presented as a non-PASS, and a reviewer relaunched after a
  crash past that save reuses the replacement binding byte for byte.
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
- Empty-set journal recovery (D12.6): with an empty set, a crash injected
  after the comment is published and before the issuing save, then a
  resume with `execution.command_timeout_seconds` changed, and again with
  the sandbox mode changed (the allow-list until #168 adds the mode). The
  journal completes the round with no relaunch, no second comment and no
  validator launch; the certificate carries the binding's `policy_digest`,
  never the one in force; the same write enters `BLOCKED` naming the policy
  change, not `READY_FOR_MERGE`. `unblock` after restoring the
  configuration decides `READY_FOR_MERGE`; `unblock` under the changed one
  decides `REVIEW`. With the configuration unchanged, the same recovery
  enters `READY_FOR_MERGE`. The same cases with a non-empty set, and with a
  changed definition set, block alike.
- The certificate's candidate, record seqs and digests,
  `validation_set_digest` and `policy_digest` equal the binding's, for an
  empty set and a non-empty one.
- Issuance is refused while the binding's round is not the completed
  round or its candidate is not the reviewed one, a named record is not
  PASS, or the named records do not encode to the binding's
  `validation_set_digest`, with no certificate.
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
- The override of an uncompleted review, end to end: a REVIEW entry
  presents a `FAIL` for a candidate no producer validated, the reviewer
  reports `blocked`, and the operator repairs the environment and runs
  `unblock`, which decides `REVIEW` with the candidate and the
  configuration unchanged. The entry runs the failing definition again,
  reusing the `PASS` records before it, and replaces the binding before the
  reviewer launches; the reviewer's prompt presents the replacement
  records, now `PASS`, and a clean result issues a certificate naming them.
  An `unblock` that sends a planned round to complete from #162's journal
  launches no validator and leaves the binding as written.
- The override with an empty history: with the default empty set, with a
  legacy state migrated with no records, and after a block before any
  record (a materialization failure), `unblock` writes 0 and the state
  loads; the next record gets a seq above 0 and a FAIL in it is terminal; a
  later `unblock` sets the highest retained seq.
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
  sandbox mode after a clean review** blocks the merge gate, or the
  certificate issuance of a round that completes from #162's journal
  (D12.6), until it is restored or a new round runs.
- **A validation command's argv is bounded** (D10.3): a configuration that
  exceeds the bounds is refused at load, where today any length is
  accepted.
- **An interrupted validation is recorded as an interruption, not as a
  run:** the record says what the controller was about to launch and
  nothing about what happened (D10.7).
- **Validation is not hermetic and not reproducible;** a record says where
  and how it ran, and the documentation says what that does not prove.
- **Documentation follows behaviour** in the implementing children:
  `docs/agent-guides/state-and-recovery.md`, `github-safety.md`,
  `workflow.md`, `control-result-protocol.md`, `docs/configuration.md`,
  `docs/local-mode.md`, `docs/usage.md` and `autoforge.example.yaml`. If
  implementation contradicts a decision here, the implementing child amends
  this record, with a recorded rationale.
