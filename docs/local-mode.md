# Local mode (no GitHub)

Sometimes there is no GitHub to orchestrate: a coding interview, a scratch
repository, an air-gapped machine, or simply a change you want driven by the
same review loop without opening an Issue. `autoforge local` is a separate,
explicit workflow for exactly that: the same controller, the same
"never trust the agent" discipline, none of the GitHub lifecycle.

```text
features/<slug>.md  →  INITIALIZING → ANALYZE_EXECUTE → REVIEW
                                         → clean ─────────────▶ DONE
                                         → findings ──────────▶ FIX → REVIEW
                                         → findings after the fix budget ─▶ BLOCKED
```

The design record behind this mode, including its threat model and its
known limitations, is
[ADR 0001](adr/0001-local-mode-workspace-identity-and-filesystem-boundary.md).
This page is the operator's guide; the ADR is authoritative where they
differ in depth.

## What the controller does and does not do

The *controller* never touches GitHub in a local run: no `gh` invocation,
no Issue, no PR, no comment, no push, no branch, no merge, no EPIC update, no
follow-up issue, and no replan. The `GitHubClient` is built on first *use*
and a `LOCAL` run never has one, so there is no path from the controller to
`gh` at all: no authentication is needed and none is consulted. Nothing is
faked either: there are no placeholder PR URLs in local state or local
prompts. The local prompt templates are separate files that never ask for
a PR, an Issue read, a comment, a push, a merge or a follow-up Issue; they
tell the agent not to do any of it, and the local result protocol rejects a
`follow_up_created` resolution outright. Tests drive a full local run with a
GitHub double that raises on any attribute access.

That is a property of the controller, not of the agents it launches. An agent
is a same-UID subprocess with `gh` on `PATH` and network access; it is
instructed not to use them, and the controller can neither prevent nor detect
a `gh` write, a `git push` or a network request made by the agent. What the
controller *does* detect is a moved git anchor: HEAD and the checked-out branch
are compared with the values the run was pinned to before and after every
phase, and before every correction relaunch within one, so a commit, reset,
checkout or branch switch blocks the run before another agent starts. The
block reason ends with the repository to inspect and the `local run` command
that starts the next run of the same feature, with the `--config` /
`--state-dir` this one was started with. It detects
nothing else. Real containment is a sandbox, which LOCAL v1 does not provide
under the default `sandbox.mode: disabled`
([ADR 0001](adr/0001-local-mode-workspace-identity-and-filesystem-boundary.md)
§2.2 and §8.1; isolation is tracked in #10).

`sandbox.mode: required` (#168,
[ADR 0005](adr/0005-outer-sandbox-and-host-resource-capability-model.md))
runs every LOCAL agent and validation command inside an outer sandbox, or
refuses it: a host that cannot enforce one is refused at the start of the
step, before anything is saved, charged or launched (#169). The
repository root is writable and its git directory
read-only, so a commit, stash or branch switch fails outright instead of
being detected afterwards, and the LOCAL state, the lock, other worktrees
and the rest of your home are absent. The agent still runs as your UID on
the host network, so a network request is still neither prevented nor
detected ([Configuration: outer sandbox](configuration.md#outer-sandbox)).
`sandbox.mode: preferred` is `required` on a host that can enforce; on one
that cannot, every LOCAL launch runs with logical isolation only, and says
so on stderr at the launch and in its execution record (`enforced: false`,
with the reason), never as "sandboxed" (D12.2). `autoforge local doctor`
shows which of the two this host gets
([Usage: outer sandbox](usage.md#outer-sandbox)).
A LOCAL launch also masks the REMOTE default state directory
`<root>/.autoforge`, but it cannot know a REMOTE state directory configured
elsewhere inside the root (ADR 0005 D13.2): if you run REMOTE runs from the
same checkout, keep their state directory at its default or outside the
repository root. The mode is not part of the run contract, so a run may be
resumed under the other one.

**No commit is ever required and HEAD never has to move.** The implementation
and its fixes live in the working tree; AutoForge never commits, stages,
pushes, stashes, resets or switches branches on your behalf.

## The interview-sized example

The commands are written `uv run autoforge ...`, the form inside a
development clone of AutoForge. With the command installed
([Installation](../README.md#installation)), drop the `uv run`.

```bash
uv run autoforge local init add-transaction-filter
# a human (with or without an AI) refines features/add-transaction-filter.md
uv run autoforge local run features/add-transaction-filter.md
```

## Commands

```bash
uv run autoforge local doctor            # config, git, agent CLIs — never gh, never auth
uv run autoforge local doctor --feature features/add-transaction-filter.md
uv run autoforge local init <slug>       # writes features/<slug>.md from a template
uv run autoforge local init <slug> --force        # only this overwrites an existing file
uv run autoforge local run features/<slug>.md --dry-run
uv run autoforge local run features/<slug>.md
uv run autoforge local run features/<slug>.md --allow-dirty

uv run autoforge status                  # mode-aware; no Issue/PR fields for a local run
uv run autoforge step                    # step / resume / status are mode-agnostic:
uv run autoforge resume                  # they read the mode from persisted state
```

`autoforge local doctor` runs the local subset of `autoforge doctor` and
omits the `gh`, `gh auth status`, `origin` and branch-rule checks entirely.
It keeps the outer-sandbox rows (the capability probe, what `sandbox.mode`
does on this host, the LOCAL launch classes' policy, each reachable
provider's declaration and sandboxed `--version`, hard links and sandbox
directories in the checkout, and loopback TCP listeners), none of which
calls GitHub ([Usage: outer sandbox](usage.md#outer-sandbox)).
It keeps the two launch-environment rows, which read only the local git
configuration and `~/.netrc` and run each launch shell once as a login
shell (no GitHub call): what an agent's environment
is made of, by name, and whether a setting would still hand it a GitHub or
git credential. A LOCAL agent and a validation command get the same
credential-free environment as a REMOTE one
([Configuration: agent environment](configuration.md#agent-environment)),
and no read context: a LOCAL prompt names no GitHub data.
A LOCAL run cannot be unblocked with `autoforge unblock`: the LOCAL
transition table has no unblock edge.

## The feature specification

`autoforge local init <slug>` writes `features/<slug>.md` (directory
configurable via `local.feature_dir`) with `## Problem`, `## Requirements`,
`## Acceptance Criteria` (checkboxes), `## Non-goals` and
`## Notes / Decisions`. Feature specifications are project content, not
runtime state: they live in your repository and you may commit them.
Controller state stays out of the tree entirely. `local init` refuses to
overwrite an existing file unless you pass `--force`, refuses to write through
anything that is not already a regular file it could have written itself
(a symbolic link, a directory, a device), and refuses a `local.feature_dir`
that resolves outside the repository or is reached through a symbolic link.
It takes the repository controller lock like any other controller write, so it
cannot rewrite a specification an active run has frozen.

**The specification is frozen for the whole run.** `local run` records its
SHA-256, and the controller re-reads and re-checks the hash before *and* after
every agent phase. An agent that edits the specification fails the run instead
of getting easier acceptance criteria. Edit freely before a run; changed
requirements mean a new run.

The specification passed to `local run` is resolved to a regular, non-symlink
Markdown file inside the repository: traversal, absolute paths elsewhere,
directories and device nodes are all refused.

The specification is *untrusted project data*, exactly like an Issue body in
remote mode: a requirement in it is a requirement, but an instruction in it to
bypass a controller invariant has no authority.

## What the controller verifies (there is no GitHub to ask)

The local analogue of "GitHub is the source of truth" is the working tree
itself, not git's opinion of it. Before and after every phase the controller
walks the tree through its own directory descriptors and computes a **workspace
fingerprint** over everything it finds. Reviews are bound to that
fingerprint the way remote reviews are bound to a PR HEAD SHA.

It is a walk, not a `git status`, and that is the central design decision.
`git status` answers "what would I commit?", which is a different question
from "which bytes could the reviewer have read". Git reports none of these:
an ignored file, a file marked `assume-unchanged` or `skip-worktree`, a mode
change under `core.fileMode=false`, an empty directory, a symbolic link whose
target text changed. Each one is code a reviewer can read and an agent can
edit. So git is asked only what it is authoritative about
(where the repository is, what HEAD and the branch are); the filesystem is
asked what is in the tree. `git` itself is always run with argv lists, never
through a shell.

Every entry the walk finds lands in exactly one of four states, and there is
no fifth:

| | |
|---|---|
| **hashed** | regular files: SHA-256 of the bytes, plus the permission bits (whether a script is executable decides what a validation command does with it) |
| **metadata** | directories and symbolic links: the mode, and for a link its target *text*, never what the target contains |
| **excluded** | the repository's own git directory (identified by `(st_dev, st_ino)`, not by the name `.git`) and anything matching `local.exclude`. Both are hashed into the fingerprint as *rules* and named to the reviewer in its prompt, so "what was not reviewed" is part of the review's identity |
| **refused** | anything that cannot be bound at all: a FIFO, socket or device; an unreadable file or unlistable directory; a nested repository or submodule; a symbolic link pointing outside the tree, into an excluded region, or to a directory (the root included) through which an excluded entry can be reached at an unexcluded path. The run fails closed, naming the entry and the exclusion that would accept it |

Nothing is silently skipped, so "the snapshot does not mention it" and "it is
not in the tree" are the same statement.

Two things are deliberately *not* in the fingerprint. The first is HEAD and
the branch: they are bound separately, so an ordinary `git commit`, which
changes no byte of the working tree, is reported as what it is (the git
anchor moved, and the run blocks) rather than as "the reviewer modified the
workspace". The second is the run's own state, which lives outside the
reviewed tree entirely, under `<git dir>/autoforge/state`: a controller that
writes into the tree it fingerprints would either invalidate its own review on
every step or have to carve a region out by name, a region an agent could
then write into without moving the fingerprint. An explicit `--state-dir`
inside the working tree is refused for the same reason.

The walk is bounded by `local.max_workspace_entries` and
`local.max_workspace_bytes`. These are refusal thresholds, not sampling ones:
a tree too large is refused with the largest subtrees named, because a
fingerprint that fell back to metadata for the remainder would accept an
equal-sized replacement with a restored mtime.

With that, the controller checks for itself:

| Phase | Verified independently of what the agent claimed |
|---|---|
| `ANALYZE_EXECUTE` | feature spec hash unchanged; the workspace really changed (and matches the agent's `changed_workspace` claim); every configured validation command exits 0 |
| `REVIEW` | feature spec hash unchanged; the tree still matches the fingerprint the controller bound after the last verified write phase (`REVIEW` never binds a new one; a tree that drifted, whatever moved it, is `BLOCKED` before the reviewer is launched); the reviewed fingerprint is exactly that value; the reviewer did not modify the workspace; `needs_fix_round == (findings > 0)`; finding IDs unique and in-round |
| `FIX` | feature spec hash unchanged; every open finding has a resolution; a `fixed` resolution actually changed the workspace; validation commands still pass |

### Runtime writes and the run log

Runtime writes go through one capability boundary (`safefs.py`): the state
directory is opened once as a descriptor, and every name below it is resolved
with `dir_fd=` and `O_NOFOLLOW`, so no path component can be redirected
between the check and the use. Whole-file artifacts are written as a fresh
temporary in the target's own directory and renamed over the name (on Linux
an unnamed `O_TMPFILE` inode that has no directory entry while it is
written; elsewhere an `O_CREAT|O_EXCL` file, re-inspected before it is
published), which means a symbolic link, hard link, FIFO or device planted at
an artifact's name is *replaced*: it is never opened, so whatever it pointed
at is provably untouched.

The append-only `events.jsonl` is the one artifact that must be opened in
place:

- The open is `O_NOFOLLOW|O_NONBLOCK|O_APPEND` and refuses a hard link
  outright. The descriptor is opened before the agent is launched and held
  until the invocation's line is appended after it returns, and immediately
  before the write the controller checks that the name still denotes the
  inode it holds and that the inode still has one name.
- What that guarantees is the inode: the line lands in the file that was
  inspected as a single-named regular file under the log directory, never in
  a foreign file reached through a planted name. A file the agent renamed or
  linked over `events.jsonl` while it ran, however ordinary it looks, is
  refused and receives nothing, and so is a journal the agent unlinked or
  moved away (the line is not written into a silently recreated second
  journal).
- It does not guarantee the name in the one syscall between that check and
  the write: a same-user agent can move the journal's inode out of the log
  directory there (link it elsewhere and unlink it, or rename it), and the
  line then lands in the controller's own journal at its new name, or unlink
  it outright and the line is lost with the file. That agent could already
  read, copy and delete every byte of the journal, so this is stated as the
  limit (ADR 0001 §8.10) rather than guarded.
- Between two invocations the journal is owned by name like every other
  artifact: a regular file at `events.jsonl` when the next invocation opens
  it is the journal, subject to the size check below.
- The controller never reads the journal: the step sequence is recovered
  from the step directory names, and each line is appended in place, so the
  cost of logging an invocation is the line rather than the journal. Those
  names are untrusted too: only an entry of exactly the shape the controller
  publishes counts, and one of that shape that is not a directory, or
  numbers a step past what a run can make, is refused as a corrupt run log
  before the launch rather than turned into the next step's path.
- What the journal is checked for is its size, on the held descriptor,
  before the agent is launched and again at the append after it returns; the
  run's log directory is probed for a new entry before the launch as well,
  and listed under a budget of its own. A same-user agent that plants an
  oversized journal or a million names there makes the controller refuse,
  not run out of memory or time.

`run_id`, which names `logs/<run_id>`, is validated as a single safe path
component whenever state is loaded, not trusted because the controller
generated it once. The full design is ADR 0001 §5.4 (one capability boundary
for every controller write) and §5.5 (cost is a refusal, not a sample).

## Dirty working trees

v1 policy, chosen for correctness over convenience: the working tree must be
clean apart from the feature specification itself (so a brand-new,
uncommitted `features/<slug>.md` is fine). Otherwise `local run` refuses and
names the paths, so you can commit or stash them. `--allow-dirty` starts
anyway and records those paths in the run: they are reported in `status` and
to the reviewer, never silently absorbed into the implementation baseline.
Separating pre-existing edits from agent edits in the same file is a heuristic,
and a heuristic is not a trust boundary.

What `--allow-dirty` does *not* do is loosen the binding: the snapshot taken
at the start of the run hashes those dirty files like every other entry, so
their contents at the moment the run began are pinned exactly as a clean
file's are. The recorded paths are disclosure, not an exemption.

## Review/fix bound

Local mode does not use the 20-round remote machinery. The default is one
fix round: `REVIEW → FIX → REVIEW`, then `DONE` if clean and `BLOCKED` if
findings remain. Configure it with `local.max_fix_rounds` (0 means a single
review pass, and any finding blocks). A blocked local run leaves every change
in your working tree, untouched, for you to inspect.

Review rounds are routed to profiles exactly as in remote mode, so a bound of
five or more fix rounds reaches `review_round_6_plus`; that profile is then
required by `local doctor` and at the start of the run. See
[Configuration](configuration.md#local-mode-configuration) for which
profiles a local run needs.

## Validation commands

Optional, controller-owned and controller-run: argv arrays, never shell
strings, never auto-detected from your stack:

```yaml
local:
  feature_dir: features
  max_fix_rounds: 1
  validation_commands:
    - ["./gradlew", "test"]
    - ["npm", "--prefix", "frontend", "run", "build"]
```

They run after `ANALYZE_EXECUTE` and after `FIX`, are logged like any other
invocation, and a non-zero exit (or a timeout) means the phase is *not*
verified: the run stops with the phase unchanged for `resume`. `--dry-run`
lists them and executes none of them. The fingerprint a review is bound to
is taken *after* they ran, so a build cache or a generated file they leave
in the tree is part of the reviewed tree rather than a drift the next phase
would refuse. They must converge (ADR 0006 D7.2, #173): a passing command
that changed the files the fingerprint covers passed on a tree that no
longer exists, so the whole set runs once more from the tree it left,
skipping what already ran there and left it unchanged. The phase is
verified only when that confirming run ends on the same tree with every
command passing; a command that rewrites covered files on every run (a
timestamp, an appended log) never converges and leaves the phase
unverified, naming the remedy: exclude what it writes (`local.exclude`, in
a new local run) or make it not write covered files. An excluded path's
own name is part of the fingerprint, so a command that only writes inside
an excluded directory that already exists converges without a second run.
They run under the same allow-listed environment as an agent
(`execution.env_allowlist`), not the operator's whole shell, with the same
variables the controller sets and the same preflight: a command gets no
GitHub or git credential, and a refused preflight (a credential helper in
the repository's git configuration, say) leaves the phase unverified like a
failing command. When the outer sandbox is enforced they run inside it
like the agent whose tree they check, sharing the checkout's
persistent cache at `~/.cache`, on the host network unless
`sandbox.validation_network: isolated`. What they run must then be
visible inside, under `/usr`, in the repository or in
`sandbox.read_only_paths` (a tool installed elsewhere under your home or in
`/opt` is not, until you declare it there), and a command the sandbox
refuses is not run and leaves the phase unverified too.

Every command that runs leaves a `ValidationRecord` in the state file
(#172; [State and recovery](agent-guides/state-and-recovery.md#validation-evidence)):
the workspace it ran against (fingerprint, workspace-policy digest, run
id), the command, policy P, the outcome (`PASS`, `FAIL`, `TIMEOUT`), the
fingerprint of the tree before it ran and after (the next command's
before), and a bounded, redacted tail of its output. A record is reused
rather than run again (#173): a `PASS` that started and ended on the tree a
command would run on, or a `FAIL` or `TIMEOUT` on it, which is final for
that tree, so a relaunched agent that leaves the tree as it was fails
again without the command running. `REVIEW` runs no command: before the
reviewer launches, the controller binds the records the write phase made
for the bound fingerprint (`review_evidence`), and the reviewer's prompt
presents them (the command, its outcome, its exit status and duration).
A run whose state has no such evidence (one persisted before the records
existed) enters `BLOCKED` before the reviewer: "no validation evidence for
this LOCAL run; start a new local run". A clean review over a record that
did not pass is refused, as in REMOTE. `REVIEW → DONE` is entered only in
the save that issues a `local` review certificate (#174, ADR 0006 D12.5),
with no GitHub call: the controller's record that this round reviewed this
exact workspace clean under these records, set and policy, copied from the
binding. It is never shown to an agent, and it is no merge permission
(LOCAL never merges). A clean round it cannot certify exactly, or one whose
`local.validation_commands` set or policy changed after the reviewer was
presented its evidence, enters `BLOCKED` instead and leaves the work in the
working tree; a LOCAL run has no unblock, so start a new local run.
`autoforge status` shows the certificate and whether it applies. A `Ctrl-C` during a
command leaves an attempt marker; `resume` records it `INTERRUPTED` and
runs the phase's commands again. At most 16 commands, each at most 32
argv elements of at most 1,024 characters and 4,096 in all: a
configuration beyond that does not load, and a run whose contract an older
controller wrote with more enters `BLOCKED` before its next agent.

## Dry run

`local run --dry-run` is side-effect-free in the usual AutoForge sense: it
prints the mode, the feature path, its frozen hash, the current fingerprint,
the phase, the selected profile, the prompt template, the validation commands
that *would* run and the legal next transitions. It invokes no agent, runs
no validation command, and writes no state file.

## Resume and recovery

A local run is durable and resumable exactly like a remote one: state lives in
`<git dir>/autoforge/state/state.json`, is written atomically, and holds the
mode, feature path, frozen hash, base HEAD, bound and reviewed fingerprints,
review/fix rounds and open findings. `Ctrl-C` then `autoforge resume` continues
from the persisted phase, re-reading the real working tree. Each agent
invocation is logged, redacted, beside it under
`<git dir>/autoforge/state/logs/<run-id>/`, not under `.autoforge/` as in a
remote run. `--state-dir`, or a non-default `state_dir` in config, moves
both; a directory inside the working tree is refused. Layout:
[State and recovery: runtime artifacts](agent-guides/state-and-recovery.md#runtime-artifacts).

### The run contract

The state also holds the run's contract: the repository root, the state
directory, the workspace policy (`local.exclude`, both cost bounds and the
snapshot algorithm), `local.validation_commands`, `local.max_fix_rounds`,
`workflow.max_total_steps` and the prompt version, as they were when the run
started. A resumed run may revalidate its contract but never redefines it:
every later invocation (`resume`, `step`, `status`, a dry run, a crash
recovery) compares what it would define against the record before it binds
the run, and refuses with each moved field named
(`local.exclude: run: [] current: ["src"]`) rather than reviewing a tree
under rules nobody reviewed it under. Restore the setting to resume, or start
a new run under the new one. A LOCAL state file without a readable contract is
refused, never filled in from today's configuration. The design is ADR 0001
§5.7 (the Durable Run Contract).

### Launch checkpoints and the three-launch bound

Recovery never trusts what the dead process believed. Every fact the next
transition depends on (the fingerprint, the git anchor, the specification
hash) is re-derived after the restart, and an illegal combination of fields
fails at the moment the state file is read rather than somewhere downstream.

The one thing that *is* carried across is a checkpoint written **before** a
write-capable agent is launched, recording the phase and the fingerprint it
started from. That is what makes "crashed before implementing" and "crashed
after implementing" distinguishable without asking the agent: the resumed
attempt is judged against the tree from before the *first* attempt, so work
already in the tree counts, and re-entry is bounded rather than endless:
three launches per phase entry, counting every launch (the entry's own, a
correction retry after a malformed `CONTROL_RESULT`, a resumed attempt),
each written to the state file immediately before the agent starts, then
`BLOCKED`. The `execution.max_correction_attempts` setting can never
multiply that bound.

Only a launch is counted: a step that is refused before the agent starts (a
corrupted event journal, a run log directory the controller cannot publish
into, an unusable execution profile, a prompt template that cannot be
rendered) charges nothing, so repairing the cause and resuming does not spend
the bound. A correction the controller declines to launch charges nothing
either: one after a launch that moved the git anchor (the step blocks), or
after a launch that may have left a process running in the tree (the step
fails, naming it).

The checkpoint belongs to the phase that wrote it: a state file holding one
under a different live phase is refused at load rather than closed by that
phase without the recorded work ever being examined; only a run that ended
in `BLOCKED` or `FAILED` keeps it, as evidence.

## Configuration

The `local:` block of the config file (`feature_dir`, `max_fix_rounds`,
`validation_commands`, `exclude`, `max_workspace_entries`,
`max_workspace_bytes`) is described in
[Configuration](configuration.md#local-mode-configuration);
[`autoforge.example.yaml`](../autoforge.example.yaml) lists every key with
its default.

## Related documents

- [ADR 0001](adr/0001-local-mode-workspace-identity-and-filesystem-boundary.md):
  threat model (§2.2), the workspace walk (§5.1), the state directory
  outside the tree (§5.2), the capability boundary (§5.4), cost bounds
  (§5.5), the run contract (§5.7), `REVIEW` never binding a fingerprint
  (§5.10) and known limitations (§8).
- [State, persistence and recovery](agent-guides/state-and-recovery.md):
  atomic state writes and the repository lock shared with remote mode.
- [Secrets and logging](agent-guides/secrets-and-logging.md): what is
  redacted before it reaches the run log.
