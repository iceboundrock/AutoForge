# ADR 0001 — LOCAL mode: workspace identity and the filesystem boundary

- **Status:** accepted, implemented in PR #44 (`feature/local-mode`)
- **Reviewed PR HEAD:** `3fb17c1a7ca0552cc25b17664569f90cff4c5904`
- **Base:** `main` (merge-base `316ba0365985b9c99b2d9858a8613462be960f2a`)
- **Supersedes:** the `git status` fingerprint and the `safeio.py` pathname
  checks of review rounds 1–4
- **Amended:** round 6 — §5.4 (state-root inode binding, R6-F1), §5.7 (the
  reader policy is frozen with the run, R6-F2), §5.8 (delimiter-safe
  specification quoting)
- **Closes by design:** #46, #47, #48, #49, #50; #45 (see *Compatibility*)

## 1. Problem

LOCAL mode reviews a working tree instead of a pull request. Four review
rounds each produced new blocking findings, and every one of them was in the
same two places: *which bytes did the reviewer review*, and *where can the
controller's own writes land*. Each round was closed with another branch in an
existing helper, and each fix made the next gap smaller but not less likely.

The question this ADR answers is not "how do the round-5 findings get fixed".
It is: **what design makes this class of finding stop existing?**

### 1.1 Historical finding ledger, by root cause

Grouped by the mechanism that produced them, not by the round that found
them. "Local" means the fix was genuinely about that one entry; "systemic"
means the finding was a sample of an open-ended set.

#### Class A — workspace identity (the reviewed byte set)

| Finding | Round | Symptom | Invariant broken | Previous fix | Kind |
|---|---|---|---|---|---|
| R1-F4 | 1 | `state_dir` at the repository root excluded everything | Review binding | Reject that one placement | systemic |
| R1-F1 | 2,3 | an in-repository `state_dir` hides implementation changes | Review binding | Name+kind allowlist for state entries | systemic |
| R1-F5 | 2,3 | unreadable files and dirty submodules got a stable "could not hash" marker | Fingerprint totality | Fail closed on those two shapes | systemic |
| R1-F9 | 1 | large files were stat-hashed, not content-hashed | Fingerprint totality | Content-hash everything | local |
| F3 | 4 | mode changes were not in the fingerprint | Fingerprint totality | Add the mode to the record | local |
| F4 (→ #48) | 4 | runtime artifacts were recognised *by filename* | Review binding | Tighten the name patterns | systemic |
| F4 (r4) | 4 | `core.fileMode=false` hides the executable bit | Fingerprint totality | Read the mode from `lstat` | local |
| R5-F1 | 5 | ignored / `assume-unchanged` / `skip-worktree` paths are unbound | Fingerprint totality | — | systemic |
| R5-F2 (→ #49) | 5 | a *clean* tracked symlink was never inspected | Fingerprint totality | — | systemic |
| #45 | — | `--allow-dirty` records paths but does not pin their contents | Review binding | — | systemic |
| #46 | — | hashing has no cost bound | Availability | — | local |
| #47 | — | submodules/nested repos unsupported | Product guarantee | — | systemic |
| R6-F2 | 6 | a resume with a new `local.exclude` re-bound the review to a narrower tree | Review binding | Freeze `local.exclude` and the cost bounds | systemic |
| R7-F2 | 7 | the link-target check had its own copy of "excluded", diverging from the walk's | Fingerprint totality | — | systemic |
| R7-F4 | 7 | the walk materialised a directory before checking the budget; the spec read was unbounded | Availability | — | local |

Every systemic row has the same cause, and it is one sentence:

> **`git status` was used as the enumerator of the filesystem universe.**

`git status --porcelain --untracked-files=all` answers *"what would I commit?"*
That is a genuinely different question from *"which bytes could the reviewer
have read?"*, and the gap between them is not a bug list — it is a feature
list, and git keeps adding to it. Ignored files, `assume-unchanged`,
`skip-worktree`, `core.fileMode=false`, clean tracked symlinks, submodule
gitlinks, empty directories: each is a deliberate git feature for *not*
reporting something, and each one is code a reviewer can read and an agent can
edit. A design that enumerates them will always be one git feature behind.

The state-directory rows (R1-F4, R1-F1, F4/#48) are the same error inverted:
having made git the enumerator, the controller then had to *subtract* its own
files from git's answer, and it did so by matching **names**. A name is not
evidence of authorship. That produced an allowlist of "shapes AutoForge could
have written", which an agent could simply write into.

#### Class B — runtime filesystem safety (where controller writes land)

| Finding | Round | Symptom | Cell of the matrix | Kind |
|---|---|---|---|---|
| R1-F2 (r2/r3) | 2,3 | `local init` followed a symlink out of the repository | link × final × create | systemic |
| F1 | 4 | logging could escape the state directory, or block on a FIFO | link/FIFO × final × open | systemic |
| F2 | 4 | `run_id` permitted log path traversal | traversal × prefix × all | local |
| R1-F5 (r4) | 4 | symbolic links at artifact names | link × final × write | systemic |
| R5-F3 | 5 | a **parent** component replaced by a symlink redirected every write | link × *parent* × all | systemic |
| R5-F4 | 5 | `local init --force` truncated an external **hard link** target | hardlink × final × truncate | systemic |
| #50 | — | all of the above are pathname checks, so check ≠ use | — | systemic |
| R6-F1 | 6 | the state **root** replaced by an ordinary directory redirected state and logs | directory × *root* × all | systemic |
| R7-F1 | 7 | the bootstrap of a new run resolved the state directory by pathname before the capability existed | link × root × create | systemic |
| R7-F5 | 7 | `create_exclusive` wrote the final name directly, so a crash left a partial artifact | — × final × create | local |

Again one cause:

> **Safety was expressed as pathname predicates evaluated before the
> operation, so every fix closed one cell of an
> (entry kind × path position × operation) matrix.**

The matrix has at least 6 × 3 × 12 cells. Rounds 1–4 closed the cells somebody
had thought of; round 5 found `parent × symlink` and `final × hardlink`. There
were more. Worse, a pathname predicate is not even sound for the cell it
closes: between `lstat(p)` and `open(p)` the name can be re-pointed (#50), and
four modules had each grown their own private copy of the checks.

#### Classes C, D, E — state machine, secrets, locking

These were raised and closed *without* recurring, which is the diagnostic
signal that they were not of the same kind:

- **C (state/recovery):** R1-F1 (`unresolved` cleared), R1-F2 (resume
  re-invoking a writer after its side effect), R1-F3 (no-commit invariant),
  R1-F4 (a failed validation charging a fix round), R1-F6 (a failed anchor
  read read as an unborn HEAD), R1-F7 / F6 (corrupt state accepted). Each
  already had the right shape: *which independently verified fact authorises
  this transition?* They needed the fact identified, not a new architecture.
- **D (secrets):** R1-F5 (r1), F3 (r4) — the journal was serialised from a
  record that had not been redacted. Fixed once, at the record, not per
  call site. Has not recurred.
- **E (locking):** R1-F3 (r2/r3) — `local init` wrote without the repository
  lock. One contract, one entry point added to it. Has not recurred.

#### Class F — run definition (what a resumed process believes the run is)

R6-F2 and R7-F1 look like an A row and a B row, and each was first fixed as
one. Read together they are a third class: both are a **new process
re-deriving a run-defining input from the current environment** — the
exclusion rules from today's configuration, the state directory from
today's pathname — and treating the result as the run's definition. The
fingerprint check, the inode check and every other REVALIDATE step compare
the world against the definition; if the definition itself is rebuilt from
the world, they compare the world against itself. §5.7 closes the class
with a persisted contract of every run-defining input and one gate that
revalidates it, and §5.9 states what that contract can and cannot prove
about identity across processes.

**The diagnosis, in one line:** classes A, B and F defined safety by
*enumerating the bad cases*; classes C, D and E defined it by *closing the
definition of the good case*. Only A, B and F kept producing new findings.

## 2. Threat model

### 2.1 Actors

| Actor | Trusted? | Can write the tree | Can write `.autoforge` | Can change git config/index | Can create links/FIFOs | Can rename directories | Concurrent | Can write outside the repo |
|---|---|---|---|---|---|---|---|---|
| Operator | yes (they own the machine) | yes | yes | yes | yes | yes | yes | yes |
| Controller | yes (it is us) | no¹ | yes | no | no² | no | no (holds the lock) | no |
| Coding agent (ANALYZE_EXECUTE / FIX) | **no** | yes, by design | same UID ⇒ yes | yes | yes | yes | yes (it is a child process) | same UID ⇒ yes |
| Review agent (REVIEW) | **no** | must not, and is checked | same UID ⇒ yes | yes | yes | yes | yes | same UID ⇒ yes |
| `git` | yes as a *program*, **no** as an oracle | — | — | — | — | — | — | — |
| Filesystem | yes as a mechanism, **no** as a namespace | — | — | — | — | — | — | — |
| Persisted state loaded after a crash | **no** | — | — | — | — | — | — | — |

¹ The controller never writes the reviewed tree except through `local init`,
which writes one feature specification before any run exists.
² The controller creates no links; it creates regular files and directories.

### 2.2 The core question, answered plainly

> When the LOCAL agent runs under the same OS user as the controller with no
> sandbox, is it a fallible collaborator or an adversarial filesystem actor?

**Neither assumption is available, so the honest answer is: it is an
adversarial actor that we cannot fully contain, and the design says so.**

A same-UID unsandboxed process can do everything the controller can. It can
`kill` the controller, rewrite `state.json`, `ptrace` the process, replace
`git` on `PATH`, or edit the operator's shell profile. **No amount of
`resolve()`, `lstat()` or `startswith()` in Python changes that.** Claiming
"AutoForge confines the agent" would be false, and every pathname check added
in that belief was a patch on an unachievable promise.

What *is* achievable is narrower and worth stating exactly:

**Achievable (and enforced).**
1. **Fidelity of the controller's own writes.** No filesystem entry an agent
   plants can cause a *controller* write to land on an inode the controller
   did not create. This is enforceable because it depends only on the syscalls
   the controller itself issues.
2. **Fidelity of the review binding.** If the bytes a reviewer could read
   change between the review and its acceptance, the controller notices.
   Enforceable because it is a total function of the tree, computed by the
   controller.
3. **Fail-closed refusal.** Anything that cannot be bound stops the run,
   naming the entry and the exclusion that would accept it.

**Not achievable, and therefore not promised.**
1. **Containment of the agent.** A same-UID agent can write anywhere the
   operator can. AutoForge does not prevent that and does not claim to.
2. **Integrity of controller state against a hostile agent.** `state.json` is
   readable and writable by the same UID. The `load` boundary rejects
   *malformed* state, not *plausible forged* state.
3. **Atomicity across the whole tree.** Snapshot-then-review is not atomic
   with respect to a concurrent writer; what is guaranteed is *detection*, not
   exclusion.

The operator-facing consequence: **run LOCAL mode on a checkout you would let
an agent edit anyway**, ideally a dedicated `git worktree`. Real isolation is
a sandbox (container, user namespace, separate UID), which is out of scope for
v1 and is the honest answer to anyone wanting guarantee (1) above.

### 2.3 The one guarantee we deliberately do not try to give with pathnames

`SafeRoot.open(root)` resolves the *root pathname* normally — symbolic links
included. A process that can redirect that pathname can redirect the checkout
itself, which is not a problem Python can solve. Everything **below** the root
is closed by construction. This limit is stated in the code and pinned by a
test (`test_the_root_pathname_is_resolved_normally_and_that_is_the_stated_limit`)
so that no future round mistakes it for an oversight.

It is a statement about the *first* resolution only, and round 6 drew the line
more sharply (§5.4): the state root's pathname is resolved normally **once**,
and thereafter the controller writes to the inode that resolution reached, not
to the name. What cannot be prevented is a redirection that was already in
place before the controller started; what is now prevented is one introduced
while it runs. §5.9 states the cross-process half of that boundary and what
the persisted contract can and cannot prove about it.

## 3. Required invariants

Stated so that each is mechanically checkable, before any code.

- **W (workspace identity).** The fingerprint is a total function of the
  working tree: for every entry the controller can reach below the root,
  either the entry's bytes-or-metadata are in the fingerprint, or the entry is
  covered by a rule that is itself in the fingerprint, or the snapshot does
  not exist (refusal). *There is no fourth outcome, and in particular no
  "not reported, so not seen".*
- **P (reader policy).** A fingerprint is only meaningful relative to the
  rules that produced it, so the classification policy in force when a run was
  created is recorded in that run's state, and a run whose configuration no
  longer matches it does not proceed. *Comparing two fingerprints can never
  establish this, because both sides of the comparison are recomputed under
  the new policy.*
- **R (runtime writes).** Every write the controller performs resolves below a
  directory it holds an open descriptor for, follows no symbolic link at any
  component below that descriptor, and lands on an inode the controller
  created — or fails. The directory the descriptor is opened *on* is itself
  bound by identity, so the root of that chain is not a pathname either.
- **S (state).** A state object that exists satisfies every invariant of its
  mode. Illegal combinations are impossible to construct from a file, not
  merely unlikely.
- **C (recovery).** A crash may happen on either side of an agent invocation.
  The controller never re-invokes a write-capable agent in a way that treats
  its dead predecessor's side effects as absent, and never advances past work
  it did not verify *after* the crash.
- **V (review).** A review is accepted only while `reviewed_fingerprint ==
  current_fingerprint`, both computed by the controller, never reported.
- **G (git anchor).** HEAD, branch and repository identity are read by the
  controller before and after every phase and are bound separately from the
  tree contents.

## 4. Options considered

### Option A — harden the live working tree with better pathname checks
Keep `git status` + `safeio.py`; add the round-5 cases (ignored paths, index
flags, clean symlinks, parent components, hard links).

*Rejected.* It closes the reported cells, not the classes. `git status` would
still be the enumerator, so the next git feature is the next finding; and
pathname checks would still be unsound against the check/use window (#50) no
matter how many are added. The diff would have looked exactly like "several
more special cases", which is the failure mode this reset exists to end.

### Option B — snapshot / staging architecture
Copy the tree into a controller-owned staging area, let agents work there, and
review the copy.

*Rejected.* It buys atomicity we do not need and costs the thing LOCAL mode is
*for*: the operator's own working tree, with their editor open on it and their
build running against it. It also does not solve the problem — a same-UID
agent can write the staging area too — while adding a copy of every repository
on every step, a reconciliation step, and a second source of truth. It trades
a real guarantee for an apparent one.

### Option C — a constrained LOCAL v1: close the definition of the good case
Make the controller, not git, enumerate the tree; make one capability object,
not pathnames, define where writes land; and **refuse** repository shapes the
controller cannot bind, rather than supporting them approximately.

**Chosen.**

## 5. Decision

### 5.1 Workspace identity is a controller walk, with a closed classification

`LocalWorkspace.snapshot()` walks the tree from an open descriptor of the
root, and classifies every entry into exactly one of:

| State | Applies to | Bound by |
|---|---|---|
| `file` | regular files | SHA-256 of the bytes **and** the permission bits |
| `dir` | directories | the mode (so an empty directory is a real entry) |
| `link` | symbolic links | the link's target **text** (never the target's content) |
| `excluded` | the git directory; `local.exclude` matches | the *rule*, hashed into the fingerprint and disclosed to the reviewer |
| *refused* | everything else | nothing — the run stops |

Git's role shrinks to the three things it is actually authoritative about:
where the repository is, what HEAD and the branch are, and the start-up dirty
policy. **The fingerprint contains no git output at all.**

Consequences, each of which was previously a separate finding:

- Ignored, `assume-unchanged` and `skip-worktree` paths are ordinary files to
  a walk, so R5-F1 does not arise — the walk never asked git.
- A clean tracked symlink is an entry like any other, so R5-F2 does not arise;
  its target *text* is hashed, and a target outside the tree (or inside an
  excluded region) is refused, because its bytes could change with the
  fingerprint unmoved. #49 is closed by the same rule.
- `core.fileMode` is a git setting; the mode comes from `lstat`.
- Empty directories, which git cannot represent at all, are entries.
- The git directory is excluded by **inode** (`(st_dev, st_ino)` compared
  against `git rev-parse --git-dir --git-common-dir`), not by the name
  `.git` — so a decoy directory named `.git` is not excluded, and a real git
  directory reached under another name still is.
- `--allow-dirty` pins the *contents* of the dirty files, because the snapshot
  hashes them like everything else. #45 is dissolved; the recorded path list
  is disclosure, not an exemption.

### 5.2 The state directory leaves the reviewed tree

A LOCAL run's state lives at `<git common dir>/autoforge/state`. The git
directory is already outside the reviewed tree for reasons that have nothing
to do with AutoForge, and it is the deepest directory in the chain the
controller did not create.

This deletes an entire mechanism rather than fixing it: `is_runtime_artifact`,
the crash-shaped-name regexes, `state_dir_relpath`, `_excluded_prefixes` and
the "only entries AutoForge could have written" allowlist are gone, because
there is nothing left to carve out of the fingerprint. R1-F4 (r1), R1-F1
(r2/r3), F4 (r4) and #48 all dissolve. An explicit `--state-dir` inside the
working tree is refused outright.

### 5.3 Nested repositories are refused, not approximated

A `.git` entry anywhere below the root — submodule or plain nested clone,
clean or dirty, tracked or untracked — refuses the snapshot, naming the
`local.exclude` entry that would accept it as unreviewed.

**This is a change to the product guarantee, and it is the right one.** A
submodule's contents are a second working tree with its own index, ignore
rules and HEAD; binding it correctly means recursing the whole design into it,
and binding it *incorrectly* means telling an operator their review covered
code it did not. #47 is therefore not "unsupported, see the follow-up issue"
but **an explicit, disclosed, enforced limitation of LOCAL v1**, with a
documented way to proceed (exclude it, and the exclusion is part of the
review's identity).

### 5.4 One capability boundary for every controller write

`safefs.py` replaces `safeio.py`. A `SafeRoot` is a directory held open as a
file descriptor; every name below it is resolved with `dir_fd=` plus
`O_NOFOLLOW` (and `O_DIRECTORY` for components), so:

- there is no pathname to re-point between check and use — the descriptor
  *is* the check, and it is the same object the write goes through. **#50 is
  dissolved rather than assessed.**
- a parent component that is a symbolic link cannot be traversed, closing
  R5-F3 as a property of the mechanism rather than as a new validation step.

The descriptor chain has to start somewhere, and until round 6 it started at a
*pathname*: `StatePaths.open_root()` re-resolved `<git common dir>/autoforge/
state` for every checkpoint and every logger. `O_NOFOLLOW` says nothing about
an ordinary directory, so an agent could `rmdir`/`rename` a directory it had
prepared elsewhere into that name and every subsequent controller write landed
in an inode the controller never created — R6-F1, and R5-F3's mechanism
applied one level further up than R5-F3 looked. The fix is the one the
descriptor was always standing in for: the first `open_root()` binds the
pathname to its `(st_dev, st_ino)`, and a later open that reaches a different
inode is a `StateError`, not a write. The root is now an identity like every
name below it, so "no pathname is load-bearing in the write path" is true of
the whole chain rather than of all of it but the first link.

Whole-file artifacts are written as a fresh `O_CREAT|O_EXCL` temporary **in
the target's own directory**, fsynced, then `os.replace(src_dir_fd=,
dst_dir_fd=)` over the name. Two consequences follow, and both are stated as
contract rather than accident:

- A symbolic link, hard link, FIFO, socket or device planted at an artifact's
  name is **replaced, not refused** — nothing is ever opened through it, so
  the external target is provably untouched. R5-F4's hard-link truncation
  cannot happen because no `O_TRUNC` is ever handed to `os.open`; truncation,
  where it is needed at all, happens on a validated descriptor.
- Only a **directory** at the target is refused, because `rename` will not
  replace one.

The one artifact that must be opened in place is the append-only
`events.jsonl`; there the open is `O_NOFOLLOW|O_NONBLOCK|O_NOCTTY` and
`st_nlink > 1` is refused on the descriptor. That is the complete set of
refusals in the write path, and it is small because the *mechanism*, not a
list, is doing the work.

Three filesystem facts are typed separately so callers cannot conflate them:
**absence** (`None` / `FileNotFoundError`), **unsafe shape**
(`UnsafePathError`), and **unreadable** (`UnreadableEntryError`, carrying the
path). Workspace identity translates "unreadable" into a bind refusal at
exactly one place, because a file that cannot be read is a file that cannot be
proven unchanged.

### 5.5 Cost is a refusal, not a sample

`local.max_workspace_entries` / `local.max_workspace_bytes` bound the walk and
**fail closed** with the largest subtrees named. Hashing less would produce a
fingerprint that accepts an equal-sized replacement with a restored mtime,
which is worse than refusing. #46 is closed this way rather than by a
heuristic.

### 5.6 Types are the contract at the state boundary

The `str`/`int` field checks in `AutoForgeState.from_dict` are derived from
the dataclass declaration itself (`_SCALAR_FIELDS`), not from a hand-written
list. The hand-written list had already missed `workspace_fingerprint`, which
could load as `null` and reach the review binding as a value no reviewer's
fingerprint could equal. Declaring a field's type is now the same act as
validating it.

### 5.7 The run is defined once: the Durable Run Contract

§5.1 says a fingerprint binds the tree *as this reader classifies it*. Round 6
showed what follows when only the fingerprint is persisted (R6-F2): a run
resumed with `local.exclude: ["src"]` added re-snapshots with the
implementation outside the bound scope, stores that fingerprint, and accepts
a clean review of a tree nobody reviewed. `reviewed == current` cannot see
it, because after the change *both sides* are computed under the new policy.

The first fix froze `local.exclude` and the two cost bounds. The next review
round found the same shape one level up (R7-F1: the state directory
re-resolved by pathname at bootstrap), and it is the shape, not the field,
that is the defect: a LOCAL run has no GitHub to be its source of truth, so
its *definition* — which tree, classified by which rules, verified by which
commands, bounded by how many rounds, with state kept where — is decided at
creation from that moment's configuration and environment, while every later
`step`, `resume`, `status` and crash recovery is a **new process that loads
the state file and also loads today's configuration**. Any step that
re-derives a run-defining input from the current environment and treats the
result as the run's definition has silently **rebound** the run. Freezing
fields one review round at a time cannot converge; every un-enumerated input
is the next finding.

The closed formulation is `run_contract.py`:

- **`LocalRunContract`** is the persisted definition of the run. Every field
  is IMMUTABLE: repository root, state directory, the whole
  `WorkspacePolicy` (exclusions, both cost bounds *and the snapshot
  algorithm's tag*, because a fingerprint is only comparable under the walk
  that produced it), `local.validation_commands`, `local.max_fix_rounds`,
  and the prompt version. The classification of every other input the run
  touches is written at the top of the module: REVALIDATED (the
  specification's bytes, the git anchor — content checks of the world the
  contract names, never written back) and DYNAMIC (fingerprints, gitdir
  inodes, which provider/model runs, cwd, counters — nothing a persisted
  safety judgment depends on).
- **`WorkspacePolicy`** is persisted as canonical text plus its SHA-256
  (`v2 snapshot=<tag> exclude=[...] max_entries=N max_bytes=N`): the text
  says *what* changed, the digest makes the text tamper-evident, and the
  version makes a pre-release `v1` policy a refusal rather than a guess.
- **`validate_local_run_contract(recorded, current)`** is the one gate. It
  runs inside `ControllerEngine.load()` *before* the state is bound to the
  engine — so `resume`, `step`, `status`, the dry-run plan and crash
  recovery all pass through it — and again at the top of every LOCAL step.
  Drift is reported per field, under the name the operator knows
  (`local.exclude: run: [] current: ["src"]`), as a `VerificationError`
  that persists nothing.
- **Execution reads run-defining values from the contract**, never from the
  configuration: the FIX budget, the validation commands and the workspace
  reader's policy all come from `local_contract()`. The configuration is
  consulted only to construct what *this invocation would define*, which the
  gate then compares. A structural test asserts that `engine.py` reads no
  run-defining `config.local.*` field anywhere else.
- **Legacy LOCAL state fails closed.** A LOCAL state without a contract, with
  a contract from another schema, or with a `v1` policy is a `StateError`
  telling the operator to start a new run. There is no
  `missing field → fill from current config` path, because that would be the
  rebinding the contract exists to prevent. REMOTE state is untouched.

Two choices inside that are worth stating:

- **The whole definition is frozen, not the part that is provably unsafe to
  change.** The cost bounds can only ever turn a snapshot into a *refusal*,
  and a wider `local.exclude` only reviews *more*, so exempting either would
  be sound today — and would be one more enumeration of which knobs happen
  to be benign, the exact shape of reasoning this ADR replaced. *"The run
  that was defined is the run that keeps running"* is the closed statement.
  Adding a run-defining input is one dataclass field: persistence,
  comparison and the drift message follow from it, and a completeness test
  checks the matrix against the dataclass.
- **It is a `VerificationError`, not `BLOCKED`.** The operator changed a
  setting or moved the checkout; restoring it and resuming, or starting a
  new run under the new definition, must both stay possible. Nothing is
  persisted by the refusal, so the run is exactly as resumable afterwards as
  it was before.

### 5.8 Quoting the specification is delimiter safety, not a trust boundary

`local_common.md` interpolates the feature specification — untrusted project
data — into the prompt. It did so inside a fixed ```` ``` ```` fence, which
made the quoting itself the injection vector: a specification containing a
```` ``` ```` line closes the block, and everything after it reads as prompt
structure the *controller* wrote.

CommonMark closes a fenced block only on a backtick run at least as long as
the opening fence, so `fenced_untrusted_block()` measures the longest run in
the content and opens a fence one backtick longer. That is a property of the
content rather than a list of things content might do — no enumeration, and
nothing inside the block can terminate it.

The fence is only half of it, and the ADR records both halves so neither is
mistaken for the whole: **a delimiter says where the data ends; the prose
around it says the quoted text is data.** `local_common.md` therefore also
states explicitly that anything in the block resembling an instruction, a
control block, a heading of the prompt, or a direction to skip a check is
content to be *implemented*, never followed. Neither half is load-bearing
without the other.

### 5.9 Durable run identity

The contract names two directories by pathname — the repository root and the
state directory — and §5.4 binds the state directory to an inode. Those are
two different guarantees with two different lifetimes, and the ADR states
them separately so neither is mistaken for the other.

**Within one controller process** (guarantee C): the state directory is a
capability opened exactly once — `ControllerEngine.state_root()` — and held
for the run's lifetime. Every later use proves the pathname still reaches the
held inode and then goes through the held descriptor. Nothing re-resolves the
name into a binding: not a checkpoint, not a logger, not the next
`state.json` read. So whatever a same-UID agent does to the pathname after
the controller started — a symbolic link, a FIFO, a prepared ordinary
directory renamed into place, the directory or its parent renamed aside and
recreated — is a refusal with the phase unchanged, and no controller byte
reaches the replacement. The bootstrap of a new run opens through the same
chain with `O_NOFOLLOW` on every component below the git directory, so the
first resolution (R7-F1) is the same resolution every later write is proven
against; `create_exclusive` publishes a durable temporary by `link(2)`, so
an interrupted bootstrap leaves no half-written artifact under the final
name (R7-F5).

**Across processes**: there is no held descriptor, only the record. The
contract persists both pathnames, so a checkout that was moved, a state
directory that was copied elsewhere, or a state file carried into another
checkout is refused with both roots named. What it does *not* detect — and
this ADR refuses to pretend otherwise — is an in-place replacement whose
contents were copied: a same-UID adversary who replaces the state directory
*at the same pathname* with a byte-identical `state.json` has produced the
input a reboot produces. The options were weighed:

- *A. Persist `(st_dev, st_ino)` of the state directory.* Rejected: an inode
  number is reused after `rmdir`, is not stable across a filesystem restore
  or a `mount --bind`, and a legitimate reboot after a backup restore would
  refuse a valid run while a replacement that reused the inode would pass.
  It adds a check that is both too strict and too weak.
- *B. A random run secret stored outside the state directory.* Rejected: the
  only other place on a single machine is another directory the same UID
  can also copy or replace; the bootstrap circularity cannot be broken from
  inside the same trust domain, and a secret that is not a secret would be a
  fabricated proof.
- *C. Pathnames in the contract.* Chosen: honest about its strength (it
  catches relocation and copying, which are the operator-error cases and
  the cheap attacks) and about its limit.
- *D. Refuse to resume at all.* Rejected: it discards the crash recovery
  the mode exists to provide, for a threat the threat model (§2.2) already
  concedes is not containable without a sandbox.

The limitation is pinned by a test
(`test_documented_limit_an_in_place_replacement_with_copied_contents_is_not_detectable`)
so that no future round mistakes it for an oversight, and so that the
contract's remaining strength is stated exactly: whatever the replacement
holds, the gate proves it is *the run that was defined* — same roots, same
policy, same budgets, same commands, same prompt version — and every other
alteration is caught by the state file's own validation or by drift.

## 6. Completeness analysis

Every shape a working tree or repository can present, and its state. There is
no implicit fifth column, and `test_the_classification_is_total` asserts that
an independent `os.walk` finds nothing the snapshot did not mention.

| Shape | State | Bound by / reason |
|---|---|---|
| Tracked file, clean | included & hashed | contents + mode |
| Tracked file, dirty | included & hashed | contents + mode |
| Tracked file, staged | included & hashed | contents + mode (the index is irrelevant) |
| Untracked file | included & hashed | contents + mode |
| Ignored file | included & hashed | `.gitignore` does not decide what a reviewer reads |
| `assume-unchanged` | included & hashed | an index flag is a git convenience, not a fact about bytes |
| `skip-worktree` | included & hashed | same |
| `core.fileMode=false` | included & hashed | the mode comes from `lstat`, not from git |
| Mode-only change | included & hashed | the permission bits are part of the record |
| Empty directory | represented by metadata | mode; git cannot represent it at all |
| Directory mode change | represented by metadata | mode |
| Symbolic link (internal target) | represented by metadata | the link **text** |
| Symbolic link (target outside the tree) | **rejected fail-closed** | its bytes cannot be bound |
| Symbolic link into the git directory | **rejected fail-closed** | points into an unbound region |
| Symbolic link into a `local.exclude` region | **rejected fail-closed** | same |
| Dangling symbolic link | represented by metadata, or rejected | the text is hashed; the target is resolved lexically and refused if it leaves the tree, exactly like a live link |
| Hard link (tree-internal) | included & hashed | it is a regular file; both names are entries |
| Submodule / gitlink | **rejected fail-closed** | §5.3 — a second working tree |
| Nested repository (untracked) | **rejected fail-closed** | §5.3 |
| Unreadable file (EACCES) | **rejected fail-closed** | cannot be read ⇒ cannot be proven unchanged |
| Unlistable directory (EACCES) | **rejected fail-closed** | same |
| FIFO / socket / device | **rejected fail-closed** | not a sequence of bytes that can be hashed |
| The repository's git directory | excluded, enforceable | matched by `(st_dev, st_ino)`; rule hashed + disclosed |
| `local.exclude` match | excluded, enforceable | operator-declared; rule hashed + disclosed |
| Run state and logs | not present | they live outside the tree (§5.2) |
| HEAD / branch | bound separately | the git anchor, deliberately not in the fingerprint |
| Tree larger than the bounds | **rejected fail-closed** | §5.5 |
| Path containing a newline | included & hashed | the record is length-prefixed; no path can forge a boundary |

## 7. Compatibility and migration

- **Config.** `local.exclude`, `local.max_workspace_entries` and
  `local.max_workspace_bytes` are new, all defaulting to strict values
  (nothing excluded; 50 000 entries / 512 MiB). Documented in
  `autoforge.example.yaml`.
- **State location.** A LOCAL run now defaults to `<git dir>/autoforge/state`.
  An in-flight run under an old `.autoforge` state directory is not migrated:
  `resume` will report no run there. LOCAL mode is unreleased, so there is no
  such run in the field; an operator who has one can pass `--state-dir` and
  will be told if it is inside the reviewed tree.
- **State schema.** No REMOTE field was removed or repurposed;
  `protocol_version` is unchanged. `local_run_contract` (§5.7) is new and
  **required for LOCAL states**; the pre-release `local_workspace_policy`
  string is refused, as is a contract of another schema or a `v1` policy
  text. A LOCAL state file without a readable contract cannot be shown to
  still mean what it meant, and it is never reconstructed from the current
  configuration; unreleased LOCAL mode has no such file in the field, and an
  operator who has one starts a new run. REMOTE states neither carry nor
  require it. A REMOTE state file written by the pre-reset code still loads,
  and the scalar-type check is strictly narrower than what it accepted (it
  rejects only values whose *declared* type they never had).
- **REMOTE mode.** Unchanged. `safefs.py` is used by `state.py` and
  `runlog.py`, which both modes share, so remote runs get the same write
  boundary; nothing about GitHub verification, replan or merge moved.

## 8. Known limitations

1. **No containment of the agent.** §2.2. A same-UID unsandboxed agent can
   write anywhere the operator can. Use a dedicated worktree; real isolation
   needs a sandbox and is not in v1.
2. **The root pathname is resolved normally.** §2.3. Everything below the root
   is closed; the root itself is the operator's own path.
3. **Submodules and nested repositories are refused.** §5.3. Deliberate, and
   disclosed with the exclusion that accepts them as unreviewed.
4. **Snapshot and review are not atomic.** A writer racing the controller is
   *detected* (the fingerprint moves), not excluded. The repository lock stops
   a second controller, not a second process.
5. **Cost.** A full content hash of the tree on every phase boundary is
   O(tree). That is the price of the guarantee; the bounds make it a refusal
   rather than a surprise.
6. **Hard links inside the tree** are hashed as two independent entries, so a
   change shows up twice. Correct, but not deduplicated.
7. **Cross-process state-root identity is by pathname.** §5.9. Relocation and
   copying are refused; an in-place replacement with copied contents is
   indistinguishable from a reboot and is accepted, by design and by test.
8. **Artifacts are published by `link(2)`.** `create_exclusive` needs hard
   links in the state directory's filesystem; one without them (some FAT
   and network mounts) refuses the bootstrap with a `StateError` naming the
   artifact rather than falling back to a non-atomic write.
9. **REMOTE replan evidence keeps its tilde fence.** The `~~~~untrusted`
   quoting of replan evidence predates `fenced_untrusted_block` and is
   unchanged; LOCAL prompts use the two primitives of §5.8 (`escape_inline`
   for one-line fields, the unclosable fence for blocks) exclusively.
