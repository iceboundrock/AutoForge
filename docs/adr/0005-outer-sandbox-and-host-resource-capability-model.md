# ADR 0005. Outer sandbox: provider-neutral host-resource capability model

- **Status:** accepted. This is a design record. #167's part is implemented
  (`src/autoforge/sandbox.py`, `src/autoforge/sandbox_bwrap.py`, the
  optional description on both executor requests, ADR 0002 §4e), and so
  is #168's (`src/autoforge/sandbox_policy.py`,
  `src/autoforge/agent_worktree.py`, each adapter's
  `sandbox_requirements`, the engine's routing and the `sandbox:` config
  section), and so is #169's (the typed capability probe in
  `sandbox_bwrap.py`, the engine's pre-launch gate and `preferred`,
  `sandbox.read_only_paths`, `src/autoforge/doctor_sandbox.py`, the
  per-class policy in the dry-run plan and the parity suite,
  `tests/test_sandbox_parity.py`). D13.1 is amended by #225 (a script's
  absolute `#!` line is reached as written), D6.3 and D6.4 by #223
  (the controller's reads of the admin dir follow no link the agent
  planted), and D12.3, D15.3 and D17.1 by #280 (a run is pinned to the
  `sandbox.mode` it was created under, in state protocol 12, and a
  `--config` file an enforced launch could rewrite is refused). It is implemented by #167 (the launch
  description, the policy compiler and the Linux `bwrap` backend in the
  executor layer), #168
  (provider declarations, engine facts, the routing of every launch, the git
  surface, `sandbox.mode` with `disabled` and `required`) and #169
  (`preferred`, the pre-launch capability gate, `doctor`, operator
  projections, cross-provider parity). The number is assigned when the
  record merges: if another ADR merges first, this record takes the next
  free number, and its index entries move with it.
- **Decides:** #166, for EPIC #158 (Wave 2A).
- **Where:** placement is decided in §2.13. Existing code this record
  builds on: `src/autoforge/executor.py`, `executor_duplex.py`,
  `providers.py`, `engine.py`, `git_transport.py`, `premerge.py`,
  `config.py`, `doctor.py`, `runlog.py`, `locking.py`, `safefs.py`,
  `run_contract.py`.
- **Related:** ADR 0001 (LOCAL workspace boundary; §2.2 calls a sandbox the
  honest answer to containment), ADR 0002 (nothing an agent starts outlives
  its invocation), ADR 0003 (Pi provider), ADR 0004 (authority boundary;
  §2.11 hands the absolute-path cases to this wave); #165 (logical
  isolation); #149 and #132 (what a provider may load); #148, #151 and #152
  (SDK helpers); #161 (candidate identity); #171 and #172 (Wave 2B exports);
  #23 (protocol numbering).

## 1. Context

### 1.1 What a launch can reach today

#166 was planned against `main` `2fff4b9`. The line numbers below are
re-read on `main` `5cc6f77`, where #160–#164 have landed.

AutoForge launches these processes outside itself:

| Launch | Site | Executor path | Working directory |
|---|---|---|---|
| Claude Code CLI, `output_format: text` | `providers.py` `AgentProvider.execute` :465 | one-shot | REMOTE worktree / LOCAL root |
| Claude Code CLI, `stream-json` | `ClaudeCodeProvider.execute` :582 | duplex, stdin `/dev/null` | same |
| OpenCode CLI | `OpenCodeProvider.execute` :744 | one-shot | same |
| Pi RPC | `PiProvider.execute` :1040 | duplex | same |
| Pi auth preflight | `PiProvider._auth_preflight` :1174 | one-shot | same |
| Claude SDK helper; OpenCode SDK helper and its server | not on `main` (#151, #152) | duplex, one containment owner (#148) | same |
| LOCAL validation commands | `engine.py` :3036 | one-shot | LOCAL root |
| pre-merge verification commands | `engine.py` `_run_premerge_command` :5002 | one-shot | the tree `premerge.export_commit_tree` (:169) exports into a `mkdtemp` directory |

Every one of them is spawned by `executor._spawn` (:1194) with
`start_new_session=True` and `contain_orphans=True`. Its environment comes
from `config.execution.environment_names()` (`config.py` :182, :311) plus
the provider's names, built by `executor._child_environment` (:1183). It
runs as the operator's UID, with the operator's whole filesystem, the host
PID namespace and every path-named socket.

**Controller-owned paths a launch can open by absolute path:**

- REMOTE state and run logs: `.autoforge/` in the controller checkout
  (`config.py` :70, `DEFAULT_STATE_DIR`). The engine says so: "The run log
  lives where the agents write" (`engine.py` :7680).
- The repository lock: `<git common dir>/autoforge/controller.lock`
  (`locking.py` :65–66).
- LOCAL state: `<git dir>/autoforge/state` (`engine.py` `local_state_paths`
  :646).
- Every issue's worktree: `<git common dir>/autoforge/worktrees/*`
  (`agent_worktree_path` :954).

**Credentials it can open the same way:** `~/.config/gh/hosts.yml`,
`~/.git-credentials`, `~/.netrc`, `~/.ssh/*`, a keyring under
`~/.local/share/keyrings` or behind the Secret Service on the session bus,
and the SSH agent socket under `$XDG_RUNTIME_DIR`.

**Controller reads that follow agent-writable git metadata:**

- `_ensure_agent_worktree` (:1038) decides reuse with
  `git -C <path> rev-parse --show-toplevel --git-common-dir`
  (`_worktree_identity` :980). That follows the worktree's `.git` file. It
  also consults the registry (`_registered_worktree_roots` :1004).
- `_check_analyze_result` (:3620) reads the candidate with `git -C
  <worktree>` (`_worktree_git` :3600): `symbolic-ref -q HEAD` and
  `rev-parse --verify -q HEAD^{commit}`. The `FIX` and `REPLAN_REEXECUTE`
  candidate reads do the same (`_check_fix_result` :3712,
  `_check_replan_result` :9596).
- LOCAL's anchor reads (`LocalWorkspace.head_sha`, `branch`) run git with the
  checkout as its working directory, so git finds the repository through the
  checkout's `.git` entry.

**What earlier records leave to this one.** ADR 0001 §2.2: "Real isolation
is a sandbox (container, user namespace, separate UID), which is out of
scope for v1." ADR 0004 §2.11, "What Wave 1 does not guarantee": absolute-path
reads and writes, shared local repository state, and the integrity of the
shared object store. #165, Target contract item 8: the same-UID
absolute-path case.

### 1.2 Measured behaviour

Two sets of measurements back this record. All of them used scratch
directories, fake repositories, fake sentinel files and fake sockets; no real
credential was read.

- **Planning (#166):** Fedora 44, kernel 7.2, SELinux enforcing, bubblewrap
  0.12.0, git 2.55.0.
- **While writing this record (marked "re-measured"):** Fedora 44, kernel
  7.2.9, SELinux enforcing, bubblewrap 0.12.0, git 2.56.0.

**Filesystem** (`bwrap`, an empty tmpfs root plus explicit binds):

- A host file that is not bound does not exist inside. A read by absolute
  path, through a symlink planted in the worktree, or by `..` traversal from
  the worktree fails with `ENOENT`.
- `/usr` and `/etc` bound read-only refuse writes. A private `--tmpfs /tmp`
  is writable. `/run` is absent unless bound.
- With `--clearenv`, `SSH_AUTH_SOCK` and `XDG_RUNTIME_DIR` are unset, and the
  sockets they named are unreachable.
- `/etc/resolv.conf` on this host is a symlink to
  `../run/systemd/resolve/stub-resolv.conf`. It dangles inside unless its
  target is projected, and DNS breaks.
- A hard link from a bound directory to another bind fails with `EXDEV`.
- A single file bound read-write can be rewritten in place, but `rename()`
  over it fails with `EBUSY`. A read-only bind refuses writes.
- Re-measured: a single file bound read-only over a path inside a writable
  bind refuses writes (`EROFS`) and renames (`EBUSY`).
- Re-measured: an empty `--tmpfs` mask over a subdirectory of a read-only
  bind, with carve-outs bound into it and the mask then remounted
  read-only (`--remount-ro` after the carve-outs, never before, or `bwrap`
  cannot create their mount points), hides everything else under the masked
  path and refuses writes.
- Re-measured: `--size` bounds a private tmpfs (a 4 MiB write into a 1 MiB
  `/tmp` stopped at 1 MiB). `--dev /dev` provides `core`, `fd`, `full`,
  `null`, `ptmx`, `pts`, `random`, `shm`, `stderr`, `stdin`, `stdout`, `tty`,
  `urandom` and `zero`.

**Processes and namespaces:**

- Without `--unshare-pid`, a sandboxed process can signal the operator's
  other same-UID processes.
- With a user namespace, `/proc/<pid>/environ`, `cwd` and `root` of host
  processes are not readable.
- A process group spans the PID namespace: `killpg(SIGTERM)` on the outer
  group reaches members inside.
- SIGTERM to the outer `bwrap` process alone is not forwarded to the child.
  `bwrap` exits, and the PID namespace is torn down.
- Re-measured: the span works in the other direction too. A command inside
  that runs `kill(0, SIGKILL)` or `kill(0, SIGTERM)` kills the outer `bwrap`
  (status 137 or 143), and the status stream then holds `child-pid` only,
  exactly as for a setup failure.
- Re-measured: without `--unshare-ipc`, a SysV shared-memory segment the
  operator's process created on the host is listed inside; with it, it is
  not. A key added to the inherited session keyring on the host is readable
  inside a `bwrap` user namespace, and is not after the process joins a new
  session keyring. The user keyring is already per user namespace: a key
  in the host's `@u` is neither found nor readable inside.
- When the main command exits, the kernel SIGKILLs every process still in
  the PID namespace, at once. Re-measured: a same-group background child
  that trapped SIGTERM got no SIGTERM, a `setsid` child was killed the same
  way, `bwrap` returned the main command's status 0.31 s after a 0.3 s
  command, and nothing was left on the host.
- In-sandbox orphans are re-parented to the namespace's init, not to the
  controller, so the controller's subreaper (ADR 0002 §4b) never sees them.
- `bwrap` nests inside the outer sandbox unless the outer one passes
  `--disable-userns`; then the inner `bwrap` fails (`ENOSPC`).
- Re-measured: inside the sandbox, a process that creates a nested user and
  mount namespace (`unshare -Urm`) can neither unmount a mask (the sentinel
  under it stayed hidden) nor remount a read-only bind read-write
  (`permission denied`), and writes there still fail with `EROFS`. The
  kernel locks mounts inherited from a more privileged mount namespace
  (`mount_namespaces(7)`).
- Rootless podman with default settings gives a user and a PID namespace but
  refuses to mount a new `/proc` (`EPERM`). It works only with
  `--security-opt unmask=/proc/* --security-opt label=disable`. The presence
  of a `bwrap` binary therefore says nothing about whether it can run.
- Re-measured, `--json-status-fd`:

  | Case | Exit status of `bwrap` | Records written |
  |---|---|---|
  | the command exits 1 | 1 | `child-pid`, then `exit-code: 1` |
  | the command exits 127 | 127 | `child-pid`, then `exit-code: 127` |
  | the command is killed by SIGTERM | 143 | `child-pid`, then `exit-code: 143` |
  | a bind source is missing | 1 | `child-pid` only |
  | the command cannot be executed | 1 | `child-pid` only |
  | a namespace cannot be created | 1 | none |

  A `child-pid` record therefore does not prove that the command ran. The
  `exit-code` record does.

**Network** (the shared network namespace, `bwrap`'s default):

- Abstract-namespace Unix sockets and loopback TCP listeners on the host are
  reachable from inside.
- With `--unshare-net`, both are refused, and so is all other network
  access, including a provider's model API.
- Re-measured: with a Landlock domain scoped to abstract Unix sockets
  (`LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET`; this kernel reports Landlock ABI
  10, and the scope needs ABI 6) applied to the process that then runs
  `bwrap`, a connection from inside to a host abstract socket fails with
  `EPERM`; a socket created inside connects normally; and `bwrap` still sets
  up its namespaces, masks and read-only remounts. No X11 or session-bus
  abstract socket listens on this host, so neither escape was observed
  directly.

**Git** (a linked worktree, files ref backend):

| Common-dir exposure | `git status` | detached commit | named-branch commit | stash | config / hooks / `objects/info/alternates` write |
|---|---|---|---|---|---|
| common dir read-only; `<common>/autoforge` and `<common>/worktrees` masked; the worktree and its own admin dir `<common>/worktrees/<id>` read-write; a private object directory (`GIT_OBJECT_DIRECTORY`, the planning design; D6.1 uses an overlay instead, re-measured below) with the common objects as an alternate | works | works (git prints a harmless `packed-refs.lock: Read-only file system`) | fails | fails cleanly, no data loss | refused |
| as above, but `objects/` read-write | works | works | fails | — | `objects/info/alternates` writable: the agent can redirect the controller's object lookup |
| as above, plus `refs/` and `logs/` read-write | works | works | works | — | the agent can move the operator's `main` (a loose ref shadows the packed one) |
| reftable backend, common dir read-only | works | works | fails; per-ref write granularity is impossible, the table stack is shared | — | refused |

- A per-worktree ref (`refs/worktree/...`) works in every read-only variant,
  because it lives in the worktree's own admin dir.
- Commits written into a private object directory are invisible to the host
  until imported. `GIT_ALTERNATE_OBJECT_DIRECTORIES=<private> git rev-list
  --objects <candidate> --not <base> | git pack-objects --stdout | git
  index-pack --stdin --fix-thin --strict` imports them, after which
  `git fsck` is clean. Re-measured with git 2.56.0, end to end in `bwrap`:
  `git status`, `git add` and a detached commit work; `git branch`,
  `git config` and a write to `objects/info/alternates` fail; the lock is
  invisible; `git --git-dir=<common> rev-parse worktrees/<id>/HEAD` on the
  host names the new commit, `git -C <worktree> cat-file -t HEAD` on the host
  fails until the import and succeeds after it; the operator's `main` is
  unchanged.
- The worktree's `.git` file and its admin dir are agent-writable when they
  are bound only as part of their writable parents. Rewriting the `.git` file
  or `<common>/worktrees/<id>/commondir` from inside succeeded, after which
  `git -C <worktree> rev-parse HEAD` on the host answered for a decoy
  repository, while `git --git-dir=<common> rev-parse worktrees/<id>/HEAD`
  still returned the real candidate.
- Re-measured: git honours an `info/alternates` file *inside* a directory
  named by `GIT_ALTERNATE_OBJECT_DIRECTORIES`. With such a file in a private
  object directory pointing at another repository's object store, a host git
  process in the shared repository read an object that exists only in that
  other repository.
- In every variant, objects of the operator's unpushed branches stay
  readable through the shared object store.
- Re-measured: `GIT_OBJECT_DIRECTORY` applies to every repository a process
  touches, not only to the worktree. With it set, `git init` of a temporary
  repository creates no `objects/` there and a commit in it writes into the
  named directory; without the variable, git does not recognise that
  repository at all.
- Re-measured: an overlay instead, `bwrap --overlay-src <common>/objects
  --overlay <private> <work> <common>/objects`, inside the read-only common
  dir. `git status`, `git add` and a detached commit work, and the new
  objects land in `<private>`; a temporary repository created inside keeps
  its own objects; the shared store is byte-identical afterwards; a write to
  `objects/info/alternates` lands in `<private>/info/alternates`; deleting a
  shared object from inside leaves it in the shared store and puts an
  overlay whiteout (a character device 0:0) in `<private>`. The strict
  import from `<private>` then succeeds and `git fsck` is clean. Before the
  import, the host's `git gc` fails (`fatal: bad object
  worktrees/<id>/HEAD`) and `git fsck` reports the missing objects.
- Re-measured: `git --git-dir=<common> symbolic-ref -q worktrees/<id>/HEAD`
  exits 1 for a detached worktree `HEAD` and prints the branch for an
  attached one.
- Re-measured: a linked worktree's submodule repositories live in its own
  admin dir (`<common>/worktrees/<id>/modules/<name>`), not in the main
  worktree's `<common>/modules/<name>`.

### 1.3 Terms

- **Projection:** a host path made visible inside the sandbox. Its access is
  *writable* or *read-only*. It appears at its own host path, except the
  closed set of relocations in D4.2.
- **Mask:** an empty, read-only directory mounted over a path inside a
  read-only projection, so that nothing below it is visible except the
  carve-outs mounted into it.
- **Overlay:** a read-only host directory made visible with a writable,
  controller-owned upper directory over it, so that writes land in the
  upper directory and never in the host directory (D6.1).
- **Denied root:** a host path that no projection may expose (D4.2, D4.3).
- **Carve-out:** an engine-supplied projection that lies inside a mask or a
  denied root. The set is closed (D4.2).
- **Inaccessible:** not projected, so absent inside.
- **Launch class:** what is launched, which decides its row set in §2.5:
  *REMOTE agent*, *LOCAL agent*, *LOCAL validation command*, and *export
  command* (pre-merge verification today; #172's export-backed validation
  later).
- **Launch description:** the backend-neutral, compiled result of §2.13: the
  projections, masks, private mounts, environment and network position of
  one launch.
- **Enforced:** the backend established the compiled launch description for
  that launch, as its status records prove (D10.8).

## 2. Decisions

Each decision is labelled `D<item>.<n>` after #166's Target contract items,
and names exactly one implementing child, or says that it is documentation
only. §7 repeats the assignment as one table and lists where a decision
differs from a child issue's body. Where a child's body and this record
disagree, this record wins.

### 2.1 Vocabulary (item 1)

**D1.1 (documentation only).** Four mechanisms, kept apart in every
document this wave touches:

| Term | What it answers | Owner | Mechanism |
|---|---|---|---|
| **Logical isolation** | What does AutoForge *hand* a child process? | #165 (Wave 1) | environment allow-list and refusal, empty gh configuration, disabled credential helpers, withheld SSH agent socket, preflight, credential doctor |
| **Outer sandbox** | What can a child process *reach* when it deliberately tries? | this record; #167–#169 | an OS-enforced filesystem, process and IPC boundary around every launch AutoForge makes outside itself |
| **Process containment** | What may *outlive* the invocation? | ADR 0002 | process group, exit grace, SIGTERM then SIGKILL, Linux subreaper, execution facts |
| **Provider-native sandbox** | What does a provider restrict on its own? | the provider; its settings are #149's (SDK) or `providers.py`'s (CLI) | for example a CLI's built-in Bash sandbox |

A provider-native sandbox is never the AutoForge boundary (D11.1).

**D1.2 (#169).** **"Sandboxed" is a fact about one launch.** No plan line,
`status` line, stderr notice, `doctor` row, execution record or document
calls a launch sandboxed unless the outer sandbox was enforced for that
launch. A launch under `disabled`, or under a `preferred` fallback, is
described as "logical isolation only".

*Rationale.* The three mechanisms answer different questions and fail
differently. Conflating them is how "agents are separated from your
checkout" becomes "agents are sandboxed" in an operator's head, and the
second claim is false until this wave lands on a capable host.

*Rejected.* **One word for all of it** ("isolation"). It hides which of
the three a given guarantee rests on.

### 2.2 Threat model (item 2)

**D2.1 (#168).** The threat model below is recorded in
`docs/agent-guides/github-safety.md`, beside Wave 1's (ADR 0004 D11.1).

**Actors.** As in ADR 0004 §2.11. Trusted: the operator and the
controller. Untrusted: the agent (its output, and anything a prompt
injection makes it do), any tool or subprocess it starts, and
repository-defined validation and pre-merge commands, which are the change's
own code. GitHub is the source of truth.

**In scope.** Any untrusted actor above, running as the operator's UID,
that deliberately tries to:

- read or modify controller state, the lock, the run logs, or the
  controller checkout;
- read or modify unrelated worktrees or repositories;
- read credential files, keys or keyrings by absolute path, or through
  symlinks, traversal or hard links it creates;
- connect to the SSH agent, the session bus or another local IPC endpoint:
  a path-named or abstract-namespace socket, SysV IPC, the kernel session
  keyring;
- signal or inspect the operator's other processes;
- write outside its allowed paths;
- steer the controller's own later git reads through repository metadata.

**Out of scope, stated explicitly:**

- kernel or `bwrap` exploits;
- a malicious operator;
- network egress, including exfiltration of whatever the sandbox can
  legitimately read (§2.9);
- resource exhaustion (CPU, memory, disk, PIDs) beyond what ADR 0002 and
  D8.1's tmpfs bounds already limit;
- side channels;
- credentials the operator deliberately projects (§2.7, D15.4);
- the operator's own unsandboxed tools;
- launches under `disabled` or a `preferred` fallback, which keep logical
  isolation only (§2.12).

**What an enforced launch guarantees:**

1. **G1. Nothing of the controller is reachable.** Controller state, run
   logs, the lock, the REMOTE controller checkout, and every worktree but
   the launch's own are absent inside. The one controller-owned input an
   agent reads, the per-launch read context (ADR 0004 D10.2), is read-only.
2. **G2. No credential the boundary hides is reachable by path or by local
   IPC.** GitHub and git credential files, `~/.ssh`, file keyrings, the SSH
   agent socket, the session bus and every other path-named socket under
   `/run` and `$XDG_RUNTIME_DIR` are absent. No abstract-namespace socket of
   the host can be connected (D9.1), and neither the operator's SysV IPC
   objects nor the inherited session keyring are reachable (D10.1).
   Symlinks and `..` resolve inside the sandbox's own mount tree, and a new
   hard link to a file outside fails.
3. **G3. No host process is visible, and none is signallable except the
   launch's own outer `bwrap`.** The sandbox has its own PID namespace and
   procfs. The outer `bwrap` shares the command's process group, so the
   command can kill it; D10.8 treats that as the command's own abnormal
   end, never as a setup failure.
4. **G4. Writes land only in the launch's writable projections and private
   mounts** (§2.5).
5. **G5. Shared repository state is read-only.** No shared ref,
   configuration, hook, alternates file or shared object can be written
   (§2.6). The controller reads the candidate through the common dir and
   imports its objects strictly (D6.3, D6.5).
6. **G6. Nothing outlives the invocation,** inside the namespace included
   (§2.10).

**What an enforced launch does not protect:**

- **The projected model credential** is readable by every tool in the same
  sandbox (D7.4).
- **The repository** the agent is given is readable, and so is the
  operator's unpushed work in the shared git directory: the objects of
  unpushed branches and unreachable objects, the main worktree's `index`,
  `refs/stash`, the reflogs and any in-progress operation state (D6.7).
- **The network** is shared with the host: loopback TCP services, the local
  DNS stub, and general egress (§2.9). A loopback service that acts on
  request (an X server listening on TCP, a debugger or container-daemon
  port, a local development server with an execute endpoint) is an
  **escape**, not only an exposure: G1–G6 describe what a process inside
  reaches through the filesystem, local IPC and signals, and none of them
  holds against such a service. `doctor` lists the listeners (D9.3).
- **Pre-existing hard links** in a writable workspace alias their outside
  inodes (D4.4).
- **Credential-shaped values in repository configuration** that #165's
  preflight does not refuse (it refuses a token-bearing remote URL and a
  repository-local credential helper), because the common dir is projected
  read-only as a whole (D6.1).
- **A `dir-rw` provider directory** is a persistent write channel shared by
  every launch that projects it, across issues and roles. It is never the
  provider's own operator configuration root (D7.2), so it reaches the
  operator's unsandboxed sessions only if the operator points one at it
  (D7.1).

*Rationale.* An enforced sandbox can promise only what a mount, PID and
IPC boundary decides. Listing the residuals keeps the promise exact, and
each residual names the decision that leaves it.

*Rejected.*

- **Network isolation in scope.** Every provider needs its model API, and
  in one network namespace provider traffic and tool traffic cannot be told
  apart (§2.9).
- **Treating projected model credentials as protected.** Tools run in the
  provider's own process tree and can read what the provider reads.

### 2.3 Which launches the sandbox covers (item 3)

**D3.1 (#168).** **Every launch AutoForge makes outside itself, under one
policy compiler:**

- every agent provider: Claude Code CLI (one-shot and stream-json), OpenCode
  CLI, Pi RPC and Pi's per-launch auth preflight; the Claude SDK helper, and
  the OpenCode SDK helper with its server, once #151 and #152 land;
- repository-defined commands: LOCAL validation commands and pre-merge
  verification commands, and #172's export-backed validation when it lands.

**Outside the sandbox, as in ADR 0002 §4b:** the controller's own `git` and
`gh` plumbing, and `doctor`'s read-only version and authentication checks
of provider binaries, which run no prompt and no repository code. #169's
`doctor` adds one sandboxed minimal launch per configured provider (D14.1)
to prove that its declaration is enough.

*Rationale.* Repository-defined commands are the change's own code. A
sandbox that wrapped agents only would leave exactly the same code an
unsandboxed path one phase later. The controller holds the operator's
credentials on purpose and needs the operator's real repository; ADR 0004
§2.7 already bounds what agent-writable state can make it run.

*Rejected.*

- **Agents only.** Leaves the change's code unsandboxed in validation and
  pre-merge verification.
- **Sandboxing the controller's own git and `gh`.** They are trusted, hold
  the credential the sandbox exists to hide, and need the whole repository.
- **Per-provider opt-in.** One boundary with holes per provider is not a
  boundary; parity is part of the contract (§8).

### 2.4 Allow-list root and canonicalization (item 4)

**D4.1 (#167).** **The sandbox root is an empty tmpfs, and every visible
path is an explicit projection, mask or private mount.** No deny list over a
bound `/` exists anywhere.

*Rationale.* The set of resources a same-UID process can reach cannot be
enumerated: every tool, keyring, socket and cache the operator ever
installed. An allow-list is complete by construction; a deny-list is
complete only until the next install.

*Rejected.* **A deny-list over a bound `/`** (or over the operator's home).
Each omission is a hole, and nobody can test for the hole they did not
think of.

**D4.2 (#167).** **Canonicalization rules every projection obeys,** applied
by the compiler before anything is spawned. A refusal is a typed error from
`src/autoforge/errors.py` that names the path and the rule, never a file's
contents.

- **C1. Resolved sources.** Every source is resolved with `realpath` on the
  host before launch, and the description holds only resolved, absolute
  paths. A relative source is refused.
- **C2. No link in a controller-owned path.** A controller-owned source (an
  engine fact: workspace, admin dir, private object directory and the
  overlay's work directory, cache, export, read context) must resolve to
  itself. A symbolic link anywhere
  below the controller's anchor is refused, as in `safefs` and
  `_ensure_agent_worktree`. A provider- or operator-declared source may be
  reached through links; what is checked and projected is the resolved
  path.
- **C3. Kinds.** Only directories and regular files are projected. A device
  node, socket or FIFO is refused, whoever declares it. The backend's own
  `/dev`, `/proc`, `/sys` and tmpfs mounts are not projections of host
  sources.
- **C4. Denied roots.** The denied roots are the engine's (D13.2: the state
  directory, the run logs, the lock, other worktrees and their admin dirs,
  `<git common dir>/autoforge`; for REMOTE, the controller checkout; for
  LOCAL, whose workspace is that checkout, `<git dir>/autoforge` and the
  REMOTE state directory inside the root) plus the fixed forbidden sources
  of D4.3.
  - A projection whose source equals or lies inside a denied root is
    refused, unless it is a carve-out.
  - A denied root whose innermost enclosing projection is read-only must be
    masked by the engine in the same description; otherwise the launch is
    refused.
  - A denied root whose innermost enclosing projection is writable is
    masked when it is a direct child of that projection's root, and refused
    otherwise. A deeper one could be moved on the host by renaming one of
    its ancestors from inside, and a forged copy created at its path; a
    mount point refuses rename (`EBUSY`, measured for a pinned file), and
    so does the projection's root, so a direct child cannot be moved.
- **C5. Carve-outs are a closed set,** supplied by the engine only, never by
  a provider or an operator:
  - the git common dir, read-only with the overlay over its `objects`
    (D6.1), when it lies inside the REMOTE controller checkout, which is the
    default (`<checkout>/.git`);
  - the issue's own worktree, its admin dir and its sandbox directory
    (D8.4);
  - the per-launch read context (read-only; ADR 0004 D10.2);
  - the controller-owned configuration locations #165 points the child at
    (read-only; D8.3);
  - for LOCAL, its own sandbox directory.

  #149's SDK roots join the set only if #149 places them under controller
  runtime state, each projected on its own and never by exposing the state
  directory.
- **C6. Overlaps.** A provider- or operator-declared read-only projection
  that lies inside a read-only engine projection is already visible: it is
  dropped and the description records that it is covered (an install root
  under `/usr`, a CA bundle under `/etc`). Every other overlap between
  declared projections, or between a declared and an engine projection
  (equal, nested or containing), is refused. Engine projections and the
  relocations below are ordered deterministically: parents before children,
  a mask after its parent and before its carve-outs, read-only remounts of
  masks last.
- **C7. Relocations are a closed set.** Every projection appears at its host
  path, except the private home (D8.1), and the `projected` items whose
  declared sandbox path lies inside the private home: a `file-ro` or
  `dir-rw` auth item (D7.1), a provider config root (D8.3), and an
  operator-declared path under the operator's home (D15.4).

**D4.3 (#167).** **Forbidden sources.** Besides the engine's denied roots,
the compiler refuses, for every projection:

- a source that is, or contains, the operator's home: the home itself and
  each of its ancestors, `/` included. Paths *inside* the home are not
  refused by this rule; the next ones decide them;
- a source that is, contains, or lies inside:
  - the GitHub and git credential and configuration locations: the gh
    configuration directory (`$GH_CONFIG_DIR` and `$XDG_CONFIG_HOME` as the
    controller's own environment sets them, never the values #165 gives a
    child; `~/.config/gh`), `~/.git-credentials`, `$XDG_CONFIG_HOME/git`,
    `~/.gitconfig`, `~/.netrc`;
  - `~/.ssh` and `~/.gnupg`;
  - keyrings and password stores: `~/.local/share/keyrings`,
    `~/.password-store`;
  - `/run` and `$XDG_RUNTIME_DIR`.

The list is defence in depth, not the boundary. The boundary is D4.1's
empty root; this list keeps a declaration or an operator path from
re-exposing what the boundary hides, and its completeness is not claimed.
The one system exception is the target of `/etc/resolv.conf` (D5.1), which
the backend projects as a single regular file even when it lies under
`/run`.

*Rationale for D4.2 and D4.3.* Every projection is checked on the resolved
path, on the host, before the spawn, because that is the only point where
the controller sees the same inode the kernel will mount; a check inside
the sandbox would run where the agent can interfere, and a check on an
unresolved path would let a link decide what is mounted. Controller-owned
paths are held to "no link at all" because a link there can only have been
planted. Kinds are limited because a device or socket is an IPC endpoint,
not a file. The forbidden list exists because declarations come from
provider code and operator configuration, which can be wrong; it catches
the mistakes that would hurt most.

*Rejected.*

- **Checking unresolved paths, or resolving inside the sandbox.** A link
  would choose the mount source, or the check would run where the agent
  can race it.
- **Following links in controller-owned paths.** No legitimate layout needs
  one, and `safefs` already refuses them for the same paths.
- **Relying on the forbidden list as the boundary.** It is a deny-list
  (D4.1).
- **Refusing everything inside the operator's home.** Install roots,
  toolchains and repositories legitimately live there; the empty root
  already hides the rest of the home.

**D4.4 (#169).** **The hard-link residual is recorded, not scanned per
launch.** `EXDEV` stops new links across mounts. A hard link that already
existed inside a writable workspace before the launch still aliases its
outside inode, so a sandboxed agent can read and write through it. That can
only come from a directory written outside the sandbox: the operator's own
LOCAL checkout, a REMOTE worktree created before this wave or used under
`disabled`, or a sandbox directory (D8.4) that an agent running under
`disabled` wrote, since such an agent can write anywhere. The pre-merge
export, the private home and the temp are created by the controller and
written only from inside. `doctor` reports regular files with
`st_nlink > 1` in the LOCAL workspace, in the existing REMOTE issue
worktrees and in the sandbox directories, bounded, by path only. The
operator's remedy for a REMOTE worktree, or for a sandbox directory whose
objects the controller has imported (D6.5), is to remove it; AutoForge
recreates it at the next launch.

*Rationale.* A per-launch walk costs time proportional to the tree
(`node_modules` alone runs to six figures of entries) and cannot tell a
planted link from a legitimate one: pnpm's store, `cp -l` and `git clone
--local` all create links on purpose. Refusing would break those trees;
warning at every launch would be noise.

*Rejected.*

- **A backend scan at every launch, refusing on `st_nlink > 1`.** False
  positives on legitimate trees, and a cost every launch pays.
- **A scan that only warns at every launch.** The same cost for a message
  nobody can act on mid-run.

### 2.5 Resource categories and default classification (item 5)

**D5.1 (#168).** The engine maps every launch to this table, with no
provider branch. "Absent" means not projected.

| Resource | REMOTE agent | LOCAL agent and LOCAL validation command | export command (pre-merge) | Notes |
|---|---|---|---|---|
| workspace | the issue worktree: writable | the repository root: writable | the exported tree: writable, per launch | the export is a controller-owned directory (D8.4) |
| the workspace's `.git` entry | the worktree's `.git` file: read-only, pinned | the root's `.git` (directory or file): read-only, pinned | absent; an export has none | D6.6 |
| the worktree's own admin dir `<common>/worktrees/<id>` | writable, with `commondir` and `gitdir` pinned read-only | read-only when the LOCAL checkout is a linked worktree | absent | `HEAD`, `index`, `logs/HEAD`, `refs/worktree/`: agent data, never trusted (D6.3) |
| git common dir (`config`, `hooks`, `refs`, `packed-refs`, `info`, and every entry not masked) | read-only | read-only, together with the git dir | absent | read-only `.git` makes LOCAL's no-commit, no-stash, no-branch rule mechanical |
| git objects | an overlay at `<common>/objects`: the shared store as its read-only lower layer, the issue's private object directory as its writable upper layer | read-only | absent | D6.1, D6.5 |
| `<common>/modules` (the main worktree's submodule repositories) | masked; the issue worktree's own are in its admin dir (measured) | read-only, with the git dir | absent | D6.7 |
| `<common>/autoforge` (lock, worktrees root, sandbox dirs) | masked; carve-outs: the issue's worktree and its sandbox directory | masked | absent | |
| `<git dir>/autoforge` (LOCAL state) | inside a masked path: `<common>/autoforge`, or another worktree's admin dir | masked | absent | |
| other worktrees and their admin dirs | masked or absent | masked or absent | absent | |
| the REMOTE controller checkout, `.autoforge/` state and run logs | absent; its git dir is the common-dir carve-out (C5) | the checkout *is* the workspace; LOCAL state is masked inside the read-only git dir, and the REMOTE state directory `.autoforge/` is masked when present | absent | state anywhere: C4 masks or refuses it |
| per-launch read context (ADR 0004 D10.2) | read-only carve-out | n/a (LOCAL has none) | n/a | lands with #165 |
| temp (`/tmp`, `TMPDIR`) | private tmpfs, per launch | private tmpfs, per launch | private tmpfs, per launch | D8.1 |
| home (`HOME`, `XDG_*`) | private tmpfs, per launch, at the operator's home path | same | same | D8.1 |
| build and tool cache (`XDG_CACHE_HOME`) | ANALYZE_EXECUTE, FIX and REPLAN_REEXECUTE: per issue, persistent, controller-owned; REVIEW and every other agent launch: inside the per-launch home | LOCAL agents that produce a change and LOCAL validation: per checkout, persistent, controller-owned; a LOCAL REVIEW launch: inside the per-launch home | none persistent: inside the per-launch home | never the operator's own caches (D8.1, D8.4) |
| provider executables and libraries | read-only: the resolved install roots | same (agent only) | absent | not all of `~/.local` |
| system runtime | `/usr` and `/etc` read-only; `/bin`, `/lib`, `/lib64` and `/sbin` in the host's own form (recreated as links on a merged-`/usr` host, measured; projected read-only where they are directories, not measured); the target of `/etc/resolv.conf` projected read-only | same | same | DNS works (measured) |
| operator-declared read-only paths | read-only | same | same | D15.4 |
| provider configuration | projected, as the provider declares and #149 / #132 select | same (agent only) | absent | D8.3 |
| provider model auth | projected through its declaration | same (agent only) | absent | §2.7 |
| GitHub and git credentials | absent, refused | absent, refused | absent, refused | composes with #165 (D4.3) |
| SSH (`~/.ssh`, the agent socket) | absent, refused | same | same | |
| keyrings, D-Bus, path-named IPC sockets, `/run`, `$XDG_RUNTIME_DIR` | absent | same | same | |
| `/var`, `/opt`, `/srv`, `/mnt`, `/media`, other homes | absent | same | same | an operator declares what a build needs (D15.4) |
| unrelated repositories and files | absent | same | same | the allow-list root's default |
| `/proc` | a new procfs in a new PID namespace | same | same | host processes invisible |
| `/dev` | minimal: `bwrap --dev` (measured list in §1.2) | same | same | |
| `/sys` | read-only | same | same | |
| network | the host network namespace, host abstract sockets unreachable | the same for a LOCAL agent; a LOCAL validation command: host by default, isolated on opt-in | host by default; isolated on opt-in | §2.9 |

**Notes on the rows that needed a choice:**

- **`/sys` is read-only.** Runtimes size heaps and thread pools from the
  cgroup limits there (JVM, Go, Node), and headless browsers read it.
  Nothing in it is a credential. Read-only removes the one write path it
  would offer, the user's delegated cgroup files. Absent would mis-size
  or break those runtimes; read-write would expose cgroup controls.
- **The export command gets no persistent cache.** Pre-merge verification is
  merge evidence. It must depend on the exported commit and the system only,
  not on a cache the issue's agents, or another PR's verification, could have
  written.
- **LOCAL validation shares the LOCAL agent's cache.** The validation
  commands run the workspace the agent wrote, so the agent controls their
  code already; a separate cache would protect nothing.
- **Only the launches that produce a change share a persistent cache.**
  ANALYZE_EXECUTE, FIX and REPLAN_REEXECUTE of one issue share it. Each
  one's output is reviewed, so what one leaves for the next gains it
  nothing a review does not see. A REVIEW launch gets its cache inside its
  per-launch home. Providers load code and data from the cache (OpenCode
  keeps a `bin/` directory and `models.json` under `~/.cache/opencode/`), so
  a cache the implementer wrote would let it run code inside the reviewer's
  provider: the cross-phase poisoning D8.1 exists to prevent. The same holds
  for code the reviewer runs itself: a test it executes can write only that
  launch's cache.

*Rationale.* One table, keyed by launch class rather than by provider, is
what makes parity testable (§8) and keeps provider semantics out of the
engine.

*Rejected.*

- **A per-provider table.** Every provider would get its own boundary.
- **The operator's own caches read-write.** Cross-issue and operator
  poisoning: an agent of issue N would write what the operator's next build,
  and issue N+1's agents, execute.

### 2.6 The git worktree surface (item 6)

**Candidates**, judged on §1.2's evidence. The surface must support three
things: `git status`, an agent-local commit, and later controller
publication under #160 and #161.

- **A. Read-only common dir with a private object directory.** The
  worktree and its own admin dir are writable. A private object directory,
  owned by the controller per issue, takes new objects as the upper layer
  of an overlay over the common object store. The agent commits on a
  detached `HEAD`. The
  controller reads the candidate through the common dir and imports exactly
  its objects with `--strict` before publishing. Closes configuration, hook,
  alternates and ref tampering, and works on both ref backends.
- **B. Read-only common dir with `refs/` and `logs/` writable.** Supports a
  named-branch commit. The agent can move the operator's `main` and every
  other branch (measured), and it is impossible under reftable.
- **C. A satellite clone per issue,** with the operator's object store as a
  read-only alternate. Fully separate refs, configuration and hooks, and the
  operator's refs and unpushed branches are not visible. It costs a clone
  and a fetch path, changes `_ensure_agent_worktree`, and changes what the
  "never delete" rule applies to.
- **D. Whole common dir writable.** Today's exposure.

**D6.1 (#168).** **Design A**, laid out as §2.5's rows:

- the common dir projected read-only as a whole;
- `<common>/autoforge` and `<common>/worktrees` masked, and in REMOTE
  `<common>/modules` too (the main worktree's submodule repositories; the
  issue worktree's own live in its admin dir, measured);
- the issue's worktree and its own admin dir writable, as carve-outs;
- an overlay at `<common>/objects`: the shared store as its read-only lower
  layer, and as its upper layer the private object directory
  `<common>/autoforge/sandbox/<key>/objects`, with the overlay's work
  directory beside it (D8.4). Every git process inside sees an ordinary
  repository; no `GIT_*` object variable is set, so other repositories the
  agent or a test suite creates keep their own objects (measured).
- Git's automatic or manual maintenance inside cannot modify the shared
  store: new objects land in the private object directory, and a deletion
  leaves an overlay whiteout there (both measured). Nothing relies on what
  maintenance does inside.

**D6.2 (#168).** **The agent commits on the worktree's detached `HEAD`,** as
ADR 0004 D1.1 and #161 already require. A phase that ever needs to name a
local revision for an agent uses `refs/worktree/*`, which lives in the
worktree's admin dir; no phase needs one today.

**D6.3 (#168).** **Candidate identity: the issue worktree's `HEAD`,
addressed through the common dir.**

- The controller reads it as `worktrees/<id>/HEAD` with
  `--git-dir=<git common dir>` (and `symbolic-ref -q` of the same name for
  the detached check, which exits 1 when detached, measured), through
  `local_git_request`'s hardening (ADR 0004
  D7.3, D7.5). Before reading, it requires `<common>/worktrees/<id>/HEAD` to
  be a regular file, decided with `lstat`.
- It never reads the candidate through the worktree's `.git` file, the
  admin dir's `commondir` or `gitdir`, or `git -C <worktree>`.
- K1 pushes `<sha>:refs/heads/autoforge/<n>` with compare-and-swap, as #161
  implemented it. No local branch is created.
- **This applies in every mode,** `disabled` included: the redirect of
  §1.2 needs no sandbox, only a writable `.git` file, which every
  unsandboxed agent has today.

*Amended by #223 (2026-10-16): no reference is resolved past `HEAD`.*
`symbolic-ref` follows a chain of symbolic references by default, so a
regular `HEAD` holding `ref: worktrees/<id>/refs/worktree/<name>`, where
`<name>` is a link the agent placed in its writable admin dir, made the
controller's git open and parse whatever host file the link names, outside
the boundary (a FIFO there stalled the read until its timeout; measured).
The detached check is `symbolic-ref -q --no-recurse`, which reads `HEAD`
alone: an attached `HEAD` is reported and the reference it names is never
opened. `rev-parse` runs only on a detached `HEAD`, which names an object
id and no reference. Reading the `HEAD` file's content instead, as the
strict import does (D6.5), would take a reftable worktree, whose `HEAD`
file is a stub, for an attached one. Nor is `HEAD` read through a link
above it: `<common>/worktrees` and `<common>/worktrees/<id>` are `lstat`-ed
as well, and one that is a link or not a directory is named and refuses
the candidate, since git would read `worktrees/<id>/HEAD` through it (an
agent without an enforced sandbox can move the registry away and leave a
link to a copy whose `HEAD` it controls).

*Consequence for #161 (merged).* #161 already defines the candidate as the
worktree's detached `HEAD` and pushes it to the controller-named remote
branch, so its rule stands. Two of its reads change, and #168 makes both
changes: the `HEAD` read moves from `git -C <worktree>` to the common-dir
address above, and "the candidate is a commit in the shared object store"
becomes "after the controller's strict import (D6.5), the candidate is a
commit in the shared object store". The same holds for #163's and #164's
candidates.

**D6.4 (#168).** **The worktree is bound to its admin dir through the
registry, and no agent-writable pointer is followed.** `_ensure_agent_worktree`
reuses an existing worktree only when all of these hold, and refuses it,
naming the inconsistency, otherwise:

- exactly one entry `<common>/worktrees/<id>` has a `gitdir` that names
  `<worktree>/.git`;
- the worktree's `.git` is a regular file that names that admin dir, in
  either form git writes (absolute, or relative under
  `worktree.useRelativePaths`);
- the worktree's path passes the existing link and location checks.

The check replaces `git -C <path> rev-parse --show-toplevel
--git-common-dir`. Like D6.3, it applies in every mode. A worktree the
operator moved breaks the pointers for git too; the refusal names
`git worktree repair`. An agent that rewrites its own pointers can only make
the check refuse, never make it name another worktree: under an enforced
sandbox the other admin dirs are masked, and its own pointers are pinned
(D6.6).

*Amended by #223 (2026-10-16): the registry is listed without git.* Reuse
first asks whether the worktree is registered at all, and the sandbox's
denied roots (D13.2) name every other registered worktree. Both read the
registry's list of roots, which `git worktree list --porcelain` used to
supply; but git resolves each listed worktree's `HEAD` for that listing,
and follows a `HEAD` that is a link (to a FIFO, it stalled the reuse and
every `resume` until its timeout; measured). The list is now read from the
registry itself, next to the binding (`registered_worktree_roots`): the
main worktree as git names it (the common dir less a trailing `/.git`),
and each `<common>/worktrees/<id>` that is a real directory with a regular
`gitdir` file, at the path that file names, read without following a link
and without git. An entry git marks `prunable` (its `gitdir` names a
`.git` that is gone, and it is not `locked`) is left out, as before. An
entry that is a link or whose `gitdir` is not a regular file names no
root, where git would read through it; the binding refuses such a
worktree in any case. The registry directory itself is no exception: the
listing and the binding `lstat` `<common>/worktrees`, open it with
`O_NOFOLLOW`, and reach each entry and its `gitdir` and `locked` files from
that descriptor, never by path. A registry that is a link (an agent
without an enforced sandbox can move the directory away and leave one) is
refused with a `VerificationError` naming it, before the directory it
names is opened, and so is one that is not a directory or cannot be read;
the operator puts the directory back and resumes.

**D6.5 (#168).** **The controller imports the private objects strictly, and
never follows an agent-written alternate.**

- **When.** After every enforced launch that had a private object
  directory, before its result is checked; and before any launch or
  candidate read for an issue whose private object directory holds objects,
  in every mode, so that a crash between a launch and its import, or a
  change of mode, leaves no commit unreadable. The import is idempotent.
- **What.** The objects reachable from the worktree's `HEAD`, read as in
  D6.3, that the shared store lacks:
  `rev-list --objects <head> --not <revisions the store holds> | pack-objects
  --stdout | index-pack --stdin --fix-thin --strict`, with the private
  directory named through `GIT_ALTERNATE_OBJECT_DIRECTORIES` for the first
  two processes only, every process hardened as in ADR 0004 D7.3. How the
  excluded revisions are chosen is #168's; a missing object still fails a
  later read closed (ADR 0004 D6.4).
- **Refusals before reading.** The controller first walks the private
  directory without following links. An `info/alternates` or
  `info/http-alternates` file, or any entry that is neither a directory, a
  regular file nor an overlay whiteout (a character device 0:0, which is
  skipped), blocks the run (`BLOCKED`), naming the entry: that is tampering,
  and git would otherwise read another repository's objects with the
  operator's authority (measured, §1.2). A whiteout only hides a shared
  object from the agent's view; git's lookup on the host finds the object
  in the shared store first, and fails closed if it is gone. The overlay's
  work directory is never read. The agent has exited and its namespace is
  gone before the walk, so nothing can change the directory between the
  walk and the read.
- **Failure.** An import that fails is never retried and publishes nothing.
  At a candidate read, an import that fails, or after which the candidate is
  not a commit in the shared store, is a candidate refusal under the phase's
  existing rules (#161, #163, #164). Before a launch, it refuses the launch
  with a typed error, as a setup failure does (D10.8).
- **Composition with ADR 0004 D7.5.** Unchanged and still required.
  `index-pack` writes objects under ids it computes from their bytes, and
  D7.5 still re-hashes every commit a decision reads, because under
  `disabled` agents write the shared store directly.

**D6.6 (#168).** **Pointer files are pinned read-only** (defence in depth):
the worktree's `.git` file, and the admin dir's `commondir` and `gitdir`,
are projected read-only over their writable parents (measured: `EROFS` on
write, `EBUSY` on rename). For LOCAL, the root's `.git` entry is projected
read-only, whatever its kind. D6.3 and D6.4 do not rely on the pins; the
pins keep the agent's own git working against the right repository and stop
a LOCAL agent from redirecting LOCAL's anchor reads, which run git in the
checkout and have no common-dir address.

**D6.7 (#168).** **Exposing the operator's unpushed work in the shared git
directory is accepted** in REMOTE and in LOCAL, and recorded in the threat
model (D2.1): the objects of unpushed branches and unreachable objects, the
main worktree's `index`, `refs/stash`, the reflogs (`logs/`), and any
in-progress operation state (`MERGE_MSG`, `rebase-merge/`, `sequencer/`).
The agent is already given the repository, and each of these is a view of
objects the shared store holds anyway; masking the files would hide
nothing the objects do not reveal. `<common>/modules` is the exception
because it is a set of other repositories, with their own configuration:
REMOTE masks it (D6.1). LOCAL needs no different answer: a LOCAL agent
works in the operator's own checkout, submodules included.

*Rationale.* Design A is the smallest exposure that supports all three
uses on both ref backends. No phase needs a shared ref (ADR 0004 D1.1), so
nothing in the common dir needs to be writable. A private object directory
moves the one shared write an agent's work needs (objects) out of the
shared store, which also closes ADR 0004 §2.11's "integrity of the shared
object store" residual for enforced launches. Reading the candidate through
the common dir needs nothing the agent can redirect, and the strict import
checks every object it adds by content.

*Rejected.*

- **B.** The agent could move every branch of the operator, and it cannot
  work under reftable.
- **C.** It protects unpushed-object confidentiality only, at the cost of a
  second clone, a fetch path and a changed worktree lifecycle. It can be
  revisited if an operator needs that confidentiality.
- **D.** Today's exposure.
- **Checking the `.git` file and `commondir`, then reading through
  `git -C <worktree>`.** Two agent-written pointers would have to be
  validated against the registry before every read, only to arrive where
  the common-dir address starts.
- **Adding the private directory to the shared store's
  `objects/info/alternates`.** It would make an agent-writable directory a
  permanent source for every controller read; the import keeps the shared
  store the only store.
- **`GIT_OBJECT_DIRECTORY` naming the private directory, with the shared
  store as an alternate.** It was the planning design. The variable applies
  to every repository the agent touches: a temporary repository a test
  suite creates gets no `objects/` of its own and is not a repository to
  any git process that runs without the variable, which includes
  AutoForge's own git requests, whose environment is an allow-list
  (measured). An overlay confines the redirect to the one directory.
- **Sandbox-only candidate addressing.** The redirect works without a
  sandbox, so the hardening belongs in every mode.

### 2.7 ProviderAuthProjection (item 7)

**D7.1 (#168).** **A provider declares its model credential as data,** as
zero or more items, each in one mode:

- **`none`.**
- **`env`:** a named environment variable, for example `ANTHROPIC_API_KEY`,
  subject to #165's refusal rules. GitHub and git names are never
  projectable.
- **`file-ro`:** one regular file, mounted read-only at a declared path
  inside the private home.
- **`dir-rw`:** one provider state directory, mounted read-write at its host
  path or at a declared path inside the private home. Required where the
  provider rewrites its credential by rename (measured: `EBUSY` on a
  single-file bind), for example Pi writing `auth.json` and
  `models-store.json` into `$PI_CODING_AGENT_DIR` at startup
  (`docs/pi-policy.md` §8). An environment variable that names it is set to
  its sandbox path.
- **`keyring`:** a login stored in a keychain or behind the Secret Service.
  It is declared so that it can be refused (D7.2, D7.3); it is never
  granted.

The engine grants a declared item only as a projection under §2.4's rules;
a provider never emits a mount. A `dir-rw` directory is a persistent write
channel shared by every launch that projects it, across issues and roles:
what the provider loads from it is decided by its loading policy (#132 for
Pi, #149 for SDK helpers), never by the sandbox, and AutoForge never reads
it as configuration. It is a directory dedicated to AutoForge's launches,
never the provider's own operator configuration root (D7.2): the operator
points the provider at it (for example through `PI_CODING_AGENT_DIR`) and
logs in there once.

**D7.2 (#167).** **A forbidden source is refused at compile time,** never
at launch. These fail before anything is spawned:

- an auth item whose source is, contains or lies inside a denied root or a
  D4.3 forbidden source (GitHub credentials, `~/.ssh`, keyrings, controller
  state, another worktree);
- every `keyring` item;
- a `dir-rw` item whose source is, contains or lies inside one of the
  provider's *operator configuration roots*: the directories its CLI loads
  settings, hooks, instructions, plugins or extensions from in the
  operator's own sessions, as the provider declares them (D13.1; for
  example `~/.claude`, `~/.pi/agent`, `~/.config/opencode`). A sandboxed
  agent that could write there would plant code that the operator's next
  unsandboxed session runs: an escape, not a persistence channel.

AutoForge never reads, copies or logs a credential's contents. The compiler
uses `lstat` and `realpath` on the path only, and errors name the path and
the rule (consistent with #149: "Do not copy/read stored credential files in
AutoForge").

**D7.3 (#169).** **Keyring-backed provider logins are unsupported under an
enforced sandbox, and `doctor` says so.** A login stored in a keychain or
behind the Secret Service cannot be projected without exposing the keyring.
`doctor` names a provider whose declaration is `keyring`, or whose
`dir-rw` item D7.2 would refuse, and the supported alternatives (a file or
dedicated directory credential, or an `env` key).

**D7.4 (documentation only).** **Accepted exposure:** every tool in the
sandbox can read the projected model credential. It is a model credential,
never a GitHub mutation credential. `docs/agent-guides/secrets-and-logging.md`
states it (#168's documentation).

*Rationale.* The provider knows what its CLI needs (`providers.py` owns the
CLI); the controller knows what must never be reachable; the backend knows
how to mount. Declaring as data keeps each of the three in its place. The
modes cover every measured credential layout: a variable, a file read in
place, and a directory rewritten by rename; `keyring` names the one layout
that cannot be projected, so that it is refused by rule rather than by
accident.

*Rejected.*

- **Copying the credential into the sandbox.** AutoForge would read and
  hold credential contents, and a refreshed token would be lost at teardown.
- **A `file-rw` mode.** A rename-based refresh fails on a single-file bind
  (measured), so the mode would fail at the first refresh.
- **Projecting the keyring or the session bus.** That exposes every secret
  the operator stores, GitHub tokens included.
- **Allowing `dir-rw` on the operator's own provider directory, with a
  warning.** A warning does not stop an agent from writing a hook the
  operator's next interactive session runs outside any sandbox.

### 2.8 HOME, XDG, configuration and the sandbox directories (item 8)

**D8.1 (#168).** **A private home per launch.**

- An empty tmpfs mounted at the path the controller's `$HOME` names, so
  `$HOME` keeps its value. In the usual case, where the password database
  names the same directory, tools that take the home from there (ssh, some
  shells) land in the same private directory. Projections under the home
  (install roots, operator paths, the repository when it lives there) appear
  inside it at their host paths.
- When `$HOME` passes through a symbolic link (`/home` linking to
  `/var/home` on some image-based distributions), the tmpfs is mounted at
  the resolved path and the link is recreated inside, so that `$HOME` and
  the resolved paths of projections under it (C1) name the same directory.
  Not measured.
- It is discarded with the namespace. Nothing in it persists except what is
  mounted into it: the persistent cache at `$XDG_CACHE_HOME`, for the
  launches §2.5 gives one, and `dir-rw` auth and declared configuration
  items.
- `/tmp` is a second private tmpfs per launch, and `TMPDIR` names it.
- Both are bounded by `bwrap --size` where the installed `bwrap` supports it
  (measured on 0.12.0); the backend records the bound, or that the kernel's
  default applies.

*Rationale for per launch.* Providers treat user-level instruction and
configuration files in the home as trusted input from the operator:
`~/.claude/CLAUDE.md`, an OpenCode or Pi user-level `AGENTS.md`, user
settings and hooks. A home that persisted across the phases of an issue
would let the implementer write instructions that the reviewer's provider
then obeys, which breaks the independence of `REVIEW`. A per-launch home
gives every phase the operator's declared configuration and nothing an
earlier agent wrote. The same reasoning keeps a REVIEW launch off the
persistent cache (§2.5), because providers load code from their cache too.
tmpfs needs no deletion: nothing an agent writes outlives its invocation,
temp files included.

*Rejected.*

- **A home per issue.** Cross-phase poisoning of the reviewer's trusted
  configuration (above).
- **A home per provider profile.** The same, across issues.
- **One persistent cache for every role of an issue.** The same poisoning,
  through the provider's cache instead of its configuration.
- **The operator's home, read-only.** A deny-list problem again: it holds
  every credential and keyring the operator has.
- **A private home at a separate path.** Password-database tools would then
  write into the skeleton directories at the operator's home path, and two
  homes would be visible.
- **A disk-backed temp directory per launch.** It accumulates unless the
  controller deletes an agent-written tree, which turns teardown into a
  `safefs` problem; tmpfs vanishes with the namespace. A build that needs
  large scratch space uses the cache.

**D8.2 (#168).** **The environment is rewritten, never passed through
dangling.**

- `HOME` keeps its value (above). `XDG_CONFIG_HOME`, `XDG_DATA_HOME`,
  `XDG_STATE_HOME` and `XDG_CACHE_HOME` are set to their defaults inside the
  private home (`~/.config`, `~/.local/share`, `~/.local/state`,
  `~/.cache`), whatever their host values. `XDG_RUNTIME_DIR` is unset.
  `TMPDIR` is `/tmp`.
- A path-valued name AutoForge knows (the above; `PATH`; the CA-bundle names
  of `DEFAULT_ENV_ALLOWLIST`; what #165 points at a controller-owned
  location, such as an isolated gh configuration; and the path-valued names
  a provider declares, such as `PI_CODING_AGENT_DIR`) passes only when its
  value resolves inside a projection. Otherwise it is unset, and `PATH`
  keeps only its entries that resolve inside. The dry-run plan and the
  execution record name each unset variable, by name only.
- A name AutoForge does not know to be a path passes unchanged, as today.
  If it names a host path, that path is absent inside, so it can only fail.
- `--clearenv` precedes the computed environment, so nothing else reaches
  the child (#167).

**D8.3 (#168).** **Configuration roots are declared, not chosen here.**
#149's isolated SDK configuration and session roots, Pi's
`PI_CODING_AGENT_DIR`, and the controller-owned locations #165 points `gh`
and git at (an empty gh configuration, the controller-supplied author
identity) are projections the provider or #165 declares, read-only unless
declared `dir-rw`. The sandbox enforces the boundary around what #149, #132
and #165 select; it chooses no SDK setting itself. A declared configuration
root may not lie inside the persistent cache (refused), so an agent can
never plant configuration through the cache. Repository instruction files
(`AGENTS.md`, `CLAUDE.md`) are reached through the workspace, as today;
whether a provider trusts them is #149's and #132's concern, not the
sandbox's.

**D8.4 (#168).** **Controller-owned sandbox directories.**

| Directory | Where | Lifetime |
|---|---|---|
| private home, temp | tmpfs inside the sandbox | per launch; gone with the namespace |
| REMOTE cache, for the launches that produce a change (§2.5) | `<git common dir>/autoforge/sandbox/<key>/cache`, where `<key>` is the worktree name (`<n>` or `epic-<n>`) | created at the issue's first enforced launch; never deleted by AutoForge |
| REMOTE private object directory, and the overlay's work directory | `<git common dir>/autoforge/sandbox/<key>/objects` and `<git common dir>/autoforge/sandbox/<key>/overlay-work` | same |
| LOCAL cache, for LOCAL launches that produce a change and LOCAL validation | `<git dir>/autoforge/sandbox/local/cache` | created at the checkout's first enforced LOCAL launch; never deleted by AutoForge |
| export | a controller-owned directory per launch: `premerge.export_commit_tree`'s today; #171 and #172 take whatever location #168 chooses | as today |

- They are created through the controller's filesystem boundary (`safefs`),
  refusing a link in any component, and never lie inside the reviewed tree
  or the state directory.
- They live under `<git common dir>/autoforge/` whatever
  `execution.worktree_dir` says, because that location is controller-owned
  and outside every working tree of the checkout.
- AutoForge creates them once and never deletes, moves or prunes them,
  consistent with the worktree rule in AGENTS.md "Runtime model". The
  operator removes them with the worktree.

*Rationale.* The git directory is already where AutoForge keeps its
per-checkout runtime state (the lock, the worktrees, LOCAL state), outside
every working tree. Keeping the sandbox directories there keeps one
lifecycle rule for everything the controller creates locally.

*Rejected.*

- **Next to `execution.worktree_dir`.** That directory is the operator's
  choice and may be anywhere, a working tree's parent included.
- **Under the state directory.** It would put agent-writable trees inside a
  denied root.
- **Automatic cleanup.** It would make the controller delete agent-written
  trees, and the operator owns local lifecycle.

**D8.5 (#169).** **`doctor` lists the sandbox directories and their sizes,**
so that an operator sees what never-deleted caches and object directories
hold.

### 2.9 Network position (item 9)

**D9.1 (#167).** **Launches share the host network namespace by default,
with abstract Unix sockets scoped.** The backend never passes
`--unshare-net` unless the description asks for it.

- Every enforced launch runs in a Landlock domain with
  `LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET`, applied to the launch's own process
  before `bwrap` runs and never to the controller. A process inside can
  connect to abstract sockets created inside, and to none of the host's
  (measured). That closes the abstract-socket escapes a shared network
  namespace otherwise leaves open: an X server that accepts the operator's
  UID by its peer credentials (a `si:localuser` grant, which some desktops
  configure) would accept synthetic input into the operator's terminals,
  and a session bus that listens on an abstract address would accept
  `systemd-run --user`.
- The scope needs Landlock ABI 6 (Linux 6.12) with Landlock enabled. It is
  part of the capability (D14.1): a host without it cannot enforce.
- The remaining exposure is documented: loopback TCP services, the local
  DNS stub, and general egress. "Network sandboxed" is never claimed.

**D9.2 (#168).** **An opt-in isolated network for repository-defined
commands only.** The operator may request an isolated network for LOCAL
validation commands and for pre-merge verification commands, separately, in
configuration. It is off by default, never applies to an agent, and is never
a silent change: the plan and the execution record state it. With it,
loopback services and abstract sockets are unreachable (measured), and so is
any dependency download, which is why it is opt-in.

**D9.3 (#169).** **`doctor` lists the TCP listeners on loopback and
wildcard addresses in the host network namespace,** by address and port
only, because each one is reachable from inside, and flags the X11 ports
(6000–6063), which an X server listening on TCP uses.

*Rationale.* Every provider needs its model API. In one network namespace,
provider traffic and tool traffic cannot be told apart, so isolating an
agent means mediating its egress, which is a later wave. Abstract sockets
are the exception: they are local IPC that happens to be scoped by the
network namespace, they include the two known same-UID escapes, and
Landlock removes them without touching the network. Repository-defined
commands need no model API, and for an offline test suite an isolated
network removes the whole loopback attack surface for one flag.

*Rejected.*

- **`--unshare-net` for every launch.** No provider would reach its API.
- **Isolated by default for repository commands.** Most test suites download
  dependencies; a default that breaks them would be turned off everywhere.
- **Egress mediation** (a proxy, an allow-list, a separate namespace for
  tools, or a user-mode network stack such as `pasta` in a separate
  namespace for every launch, which would also hide loopback and abstract
  sockets). A later wave; no issue is created here.
- **Enforcing without the abstract-socket scope, with `doctor` flagging an
  X11 or bus socket.** A display server or bus that starts after `doctor`
  ran would turn every launch into an escape while its record says
  sandboxed.
- **`required` refusing only when such a socket is listening.** The same
  race, decided by a deny-list of socket names.

### 2.10 Process ownership and composition with ADR 0002 (item 10)

**D10.1 (#167).** **User, mount, PID and IPC namespaces, and a fresh
session keyring, are mandatory** for an enforced launch. Without a PID
namespace, host processes can be signalled; without an IPC namespace, the
operator's SysV IPC objects are listed and, being the same UID, attachable;
and a key in the inherited session keyring is readable inside a user
namespace until the process joins a new session keyring (all measured).
The launch's process joins a new anonymous session keyring before `bwrap`
runs; the user keyring needs nothing, as it is already per user namespace
(measured). A launch without any of these is not enforced.

**D10.2 (#167).** **No new process owner.** The backend process (`bwrap`) is
the executor's child for exactly one invocation, spawned where the command
would have been, with `start_new_session=True` as today; so nothing inside
has a controlling terminal. `bwrap --new-session` is deliberately not
passed: the session is already new and has no terminal, and a session inside
would take the command out of the process group the executor signals.
Containment (`contain_orphans`) stays on for the outer process. There is no
daemon, service, broker or long-lived owner, and "one contained invocation
per controller process" (ADR 0002 §4b) still holds; the capability probe
(D14.1) never overlaps an invocation.

**D10.3 (#167).** **Teardown inside the namespace is the kernel's, and it
has no grace.** When the main command exits, the kernel SIGKILLs everything
left in the namespace at once (measured). That is accepted. ADR 0002's exit
grace and SIGTERM step keep applying to the outer process group on every
path, and no in-sandbox init is added. #167 amends ADR 0002 with a dated
note (§4e) that records this for sandboxed launches, as #132 did with §4b,
together with D10.5's consequence: the kill grace no longer reaches inner
processes.

*Rationale.* The kernel's teardown is stronger than ADR 0002's and
deterministic: no leftover inside can survive the invocation, a `setsid`
daemon included. What it costs is a clean shutdown for a helper an agent
backgrounded, whose output is discarded anyway. ADR 0002 already forbids
relying on such a process. Git, the one tool whose interrupted state the
next phase reads, is crash-safe, and its automatic maintenance can write
nothing shared from inside (D6.1).

*Rejected.* **An in-sandbox init that sends SIGTERM and waits a grace.** It
is a new program in the trust path, running where the agent can reach it,
to preserve a nicety the workflow does not rely on.

**D10.4 (#167).** **Execution facts.** In-sandbox leftovers are not counted:
`bwrap` returns the main command's status within the command's own runtime,
after the kernel has killed what was left (measured), so there is no moment
at which the controller could count. The execution record of every enforced
launch says that its inner leftovers were removed by namespace teardown,
uncounted; it never reports them as none. Additive fields, bounded and
redacted, with no environment value: whether the sandbox was enforced, the
backend and its version, the teardown fact, the tmpfs bounds (D8.1), the
names of unset path variables (D8.2), the network position, and a setup
failure's reason (D10.8). ADR 0002's facts keep their meaning for the outer
group and the controller's own children.

**D10.5 (#167).** **Timeout, cancellation and kill are unchanged at the
executor, and the kill grace does not reach inside.** `_terminate_group`
sends SIGTERM, then SIGKILL, to the outer group. The SIGTERM reaches every
member inside that stayed in the group (measured), and at the same moment
the outer `bwrap`, which exits on it (measured); the namespace is then torn
down, and everything inside is SIGKILLed. Inner processes therefore get no
kill grace in practice, only the instant between the two signals. That is
accepted for the reason D10.3 gives. A member that left the group inside
the namespace is SIGKILLed by the same teardown. The duplex path still
closes stdin first and gives the exit grace before any signal, as today
(ADR 0002 §4a), so a command that shuts down on end of input still does so
cleanly.

**D10.6 (#167).** **Controller death kills the sandbox.** `--die-with-parent`
is `PR_SET_PDEATHSIG`, which fires when the *thread* that forked exits. The
invariant: the executor spawns a sandboxed launch only from a thread that
outlives the invocation. Today that is the thread that calls `execute()` and
waits for its result, or calls `start_duplex()` and holds the handle until
`finish()`. A spawn from a short-lived worker thread is forbidden, and a
test pins the invariant. This is new: today an agent outlives a controller
killed with SIGKILL.

**D10.7 (#168).** **Duplex and SDK helpers.** Pi RPC, Claude's
stream-json and the SDK helpers run through `bwrap`'s stdio unchanged. An
SDK helper and the OpenCode server it starts share one sandbox and one
containment owner, consistent with #148, #151 and #152: no nested
`contain_orphans`, no ADR 0002 exemption. The server's loopback port is in
the shared network namespace (D9.1), so #152's server authentication still
applies.

**D10.8 (#167).** **A setup failure is a launch refusal, never an agent
result.** The backend passes `--json-status-fd`.

- **Setup failure:** the outer `bwrap` exited by itself (not by a signal)
  and its status stream carries no `exit-code` record: a missing bind
  source, an executable the sandbox cannot run, a refused namespace or
  procfs. It is a typed launch refusal, an `ExecutionError` handled on the
  same engine path as today's failure to spawn an executable
  (`executor._spawn`). Its reason is `bwrap`'s message, bounded and
  redacted. Nothing is attributed to the command, and no `CONTROL_RESULT`
  is parsed.
- **Abnormal end:** the outer `bwrap` was ended by a signal the executor did
  not send (the OOM killer, a kill from outside, or the command signalling
  its own process group, which reaches the outer `bwrap`, measured). It is
  the command's abnormal termination, on the existing failure path, exactly
  as an unsandboxed command killed by a signal; it is never a refusal. The
  status stream is not trusted to tell the two apart, because a killed
  `bwrap` leaves `child-pid` only, as a setup failure does (measured).
- The `child-pid` record is not the discriminator either: it is written for
  a missing bind source and an exec failure too (measured, §1.2).

**What ADR 0002 §4b's "What still escapes" becomes,** for enforced
launches:

- **No longer escapes:** a descendant that makes itself a subreaper (it is
  still inside the PID namespace); `systemd-run --user` (the session bus is
  absent by path and unreachable by abstract address, D9.1); a container
  runtime reached through a path-named socket (`/run` and `$XDG_RUNTIME_DIR`
  are absent); an X server reached through its abstract socket (D9.1).
- **Still escapes:** anything reached over TCP or UDP: an SSH host, a
  TCP-exposed container daemon, an X server listening on TCP, a loopback
  service that acts on request (§2.2).

### 2.11 Provider-native sandboxes (item 11)

**D11.1 (#167).** **The outer sandbox does not pass `--disable-userns`.** A
provider-native sandbox may run inside it; it is never counted as the
AutoForge boundary, and its settings remain #149's (SDK) or `providers.py`'s
(CLI) concern.

*Rationale.* `--disable-userns` buys kernel attack-surface reduction, and
kernel exploits are out of scope (§2.2). It breaks a provider's own
`bwrap`-based sandbox (measured `ENOSPC`) and every repository test suite
that itself uses namespaces. A nested namespace does not weaken the boundary:
the kernel locks inherited mounts, so a nested user and mount namespace
cannot unmask a path or remount a read-only bind (measured).

*Rejected.*

- **Always passing it.** Breaks nested sandboxes for a threat outside the
  model.
- **A configuration knob now.** No operator needs it yet; it can be added as
  an opt-in later, as ADR 0002 §4 argues for its graces.

### 2.12 Modes and fallback (item 12)

**D12.1 (#168).** **`sandbox.mode`**, under strict key validation, with
`disabled` (the default) and `required`.

- **`required`:** every covered launch is enforced, or refused; it never
  runs with logical isolation only, and a refusal is never silently
  converted into a fallback. With #168 alone, the refusal comes from the
  backend at the launch (D10.8, D14.2), after the engine has saved the
  attempt counter, as today's failure to spawn an executable does. #169's
  gate (D12.2) moves it before anything is saved or charged.
- **`disabled`:** logical isolation only, as on `main` plus #165, except the
  mode-independent git hardening of D6.3–D6.5.
- **The default at introduction is `disabled`,** so that upgrading breaks no
  operator whose host lacks user namespaces. Any later default change is a
  separate, recorded decision.

**D12.2 (#169).** **`preferred`, and the pre-launch gate.**

- **LOCAL:** enforced when the host is capable; otherwise the launch runs
  with logical isolation only, visibly: in the dry-run plan, in `status`, on
  stderr at the launch, and in every execution record (`enforced: false`,
  with the reason). It is never called sandboxed.
- **REMOTE:** `preferred` refuses exactly as `required` does. A REMOTE run's
  launches are unattended; a fallback nobody watches is a silent downgrade.
- **The gate.** Under `required`, and under `preferred` in REMOTE, the
  capability is established before anything is spawned, before the attempt
  counter is saved and before a LOCAL launch is charged, so a refusal leaves
  state byte-identical. The refusal is a configuration or capability error
  that names the missing capability, never a phase failure, and needs no
  recovery. The result is cached for the controller process, never across
  processes.
- **After the gate.** A setup failure at a launch the gate allowed (D10.8)
  stays on the spawn-failure path: the attempt is charged, as today. Under
  `preferred`, the fallback is decided only at the gate, never after a
  failed launch, so a launch that was meant to be enforced never re-runs
  unenforced.

**D12.3 (#168).** **The mode is not part of LOCAL's run contract.** It stays
DYNAMIC, like the provider, model and command (`run_contract.py` :58): it
decides how a launch is contained, not what the run is. A run started under
one mode resumes under another.

*Amended by #280 (2026-10-24): the mode is pinned, beside the contract.*
"Resumes under another" held for a stronger mode and made a weaker one a
silent downgrade. A command run without the run's `--config` falls back to
the built-in `disabled`. A configuration file in the LOCAL root, which every
enforced launch writes, let an agent switch the sandbox off for every launch
after its own. The mode stays out of the contract, which refuses any drift
and is never migrated. Instead every run, LOCAL and REMOTE, records the mode
it is created under (`state.sandbox_pin`, `src/autoforge/sandbox_pin.py`).
`step`, `resume` and `unblock` (and a scheduler's worker resuming an issue)
refuse a weaker mode before any phase executes, with nothing written, unless
`--accept-config-change`, which writes the change to the run log before the
run is pinned to it. Strength is what a launch gets: `disabled` is weaker than
LOCAL's `preferred`, which is weaker than `required`; REMOTE's `preferred` is
`required` (D12.2). A stronger mode moves the pin up with the command's next
save. The pin holds configuration, never enforcement (D16.1).

**D12.4 (#169).** **What each surface shows.**

- **Dry run:** the compiled policy by launch class and role (paths where
  they are known without creating anything), the mode and its outcome. A
  dry run runs no capability probe: a probe is a launch, and the plan must
  be the same on every host.
- **`status`:** the configured mode and what it means. It neither probes nor
  reads run logs; under `preferred` it says that LOCAL launches fall back
  when the host cannot enforce, and points at `doctor`.
- **`doctor`:** the capability result and what the mode will do on this
  host (D14.1); under `disabled`, that launches have logical isolation only,
  reported as a risk.

*Rationale.* `required` must never run a launch unsandboxed; `preferred`
must never be mistaken for enforcement; `disabled` must never be invisible.
A REMOTE fallback would make the first two indistinguishable in exactly the
runs nobody watches.

*Rejected.*

- **`preferred` falling back in REMOTE too.** A silent downgrade in
  unattended runs.
- **`required` as the introduction default.** It would stop every operator
  whose host lacks user namespaces at upgrade.
- **The mode in LOCAL's run contract.** It would strand a run whose host
  changed capability, for no safety judgment that depends on it.
- **A probe in the dry run.** It would make a dry run launch processes and
  vary by host.

### 2.13 Request, compile, enforce: placement (item 13)

Placement follows `docs/agent-guides/architecture.md`: no workflow
semantics in the executor, no CLI flags outside `providers.py`, no provider
semantics in the engine. Modules are named; classes and functions are not.

**D13.1 (#168).** **Providers declare requirements as data,** in
`providers.py` and its SDK siblings: executables and their resolved install
roots (from `profile.command` on the host, never all of `~/.local`),
configuration roots, auth items (§2.7), the provider's operator
configuration roots (D7.2), environment names (still subject to #165),
extra writable provider state, and whether the network is needed.
The launch names the executable by a path that exists inside the sandbox.
Each declaration is checked against the installed CLI's own documentation
and `--help` (AGENTS.md "Before changing code").

*Amended by #225 (2026-10-16): a script's `#!` line, as written.* The
kernel runs a script through the interpreter its `#!` line names, looked up
as written, not through the resolved interpreter whose install root the
declaration projects. An absolute `#!` path is therefore also a *spelled
path* of the declaration (`ProviderRequirements.spelled_paths`). The
compiler looks it up component by component as the kernel would inside.
Each symbolic link on its way that nothing of the description shows is
recreated as a link at its host path, where nothing is mounted (the empty
root, the private home or temp), as the links on the way to the home
(D8.1) and the system runtime's top-level links are. The directories that
hold those links are made on the way, empty, and never projected. So is a
directory the path leaves by `..` that nothing else makes, since the
kernel leaves a directory only from inside it (PR #232 R2-F1:
`../transit/../runtime/bin/sh`, with `transit` shown nowhere). The
launch is refused before the spawn, naming the path (rule
`D13.1 spelled path`, or C4 and D4.3 for a denied root and a forbidden
source), when a link or a directory made would lie in a mask, a denied
root, a forbidden source or a kernel filesystem, when the path loops, or
when it does not lead to where its resolved program is shown. In
`#!/usr/bin/env name` that program is `env`, which the kernel looks up as
written like any other: its path is a spelled path, and its resolved
install root is projected as an interpreter's is (PR #232 R1-F1: an `env`
reached through a link no root shows). `name` is not a spelled path: `env` looks it up on `PATH`, whose
first entry is the interpreter's directory. Tests: `tests/test_sandbox.py`
(the links, the directories left by `..`, each refusal),
`tests/test_sandbox_policy.py` (the declaration and its description),
`tests/test_doctor_sandbox.py` (the row) and `tests/test_sandbox_routing.py`
(a fake agent behind one link, behind one passing through a directory it
leaves by `..`, behind a Homebrew-shaped chain and behind a link to `env`,
through the real backend).

*Rationale.* Projecting the directory that holds a link would show
everything beside it: a Homebrew `opt/` directory links to every installed
package. Launching through the resolved interpreter instead would change
what the interpreter sees as its own path (`argv[0]`, from which Python
finds a virtual environment's `pyvenv.cfg`), and would re-implement the
kernel's `#!` argument rules in the controller. A recreated link shows only
its own contents, a path the operator's layout already names, and the
lookup inside stays the kernel's.

**D13.2 (#168).** **The engine supplies controller facts as data,** with no
provider branch: the launch class, the workspace, the worktree's admin dir,
the private object directory, the cache, the export directory, the read
context, the pinned pointer files, and the denied roots (the state
directory and run logs wherever they resolve, the lock,
`<git common dir>/autoforge` minus the carve-outs, other worktrees, for
REMOTE the controller checkout, and for LOCAL the REMOTE default state
directory `.autoforge/` inside the root, `DEFAULT_STATE_DIR`). A REMOTE
state directory configured elsewhere inside a LOCAL root is not known to a
LOCAL launch; `docs/local-mode.md` says to keep it outside the root or at
the default. The mapping from launch class and facts to §2.5's classes is a
pure function in the engine layer.

**D13.3 (#167).** **The launch description and the policy compiler** live in
a new executor-layer module. The description is typed, immutable and
backend-neutral, and carries no workflow, provider or GitHub semantics. The
compiler takes generic projections and applies D4.2 and D4.3. It is pure
except for `lstat` and `realpath` of the paths it is given, and launches
nothing.

**D13.4 (#167).** **The Linux backend** turns a description into a `bwrap`
argv (projections, masks, overlays and private mounts), applies the launch's
Landlock scope (D9.1) and new session keyring (D10.1) to the launch's own
process before `bwrap` runs, passes the status descriptor, parses the status
records (D10.8) and offers the minimal capability probe (D14.1). `bwrap`
argv appears nowhere else. `executor.py` and `executor_duplex.py` accept an
optional launch description; with none, behaviour is unchanged byte for
byte.

**D13.5 (#169).** **Operator-facing uses** of the probe (the gate,
`doctor`) live in `engine.py` and `doctor.py`.

*Rationale.* Each party contributes what it alone knows: the provider its
CLI, the engine the controller's paths, the compiler the rules, the backend
the mount syntax. Keeping the compiler pure makes every refusal testable
without a namespace.

*Rejected.*

- **Providers emitting `bwrap` flags.** CLI knowledge and mount syntax would
  mix, and every provider would re-derive the rules.
- **The resource table in the executor layer.** It names workflow roles
  (worktree, export, read context), which the executor must not know.
- **The compiler in the engine.** The rules are provider-neutral and
  workflow-neutral; the backend's tests need them without an engine.

### 2.14 Platform capability levels (item 14)

**D14.1 (#169).** **Capability is established by a real minimal launch,**
never by finding a binary or reading a version: the probe launches a trivial
command under a description of the same shape as a real launch (user, PID,
mount and IPC namespaces, a new procfs, a minimal `/dev`, a read-only bind,
a mask with a carve-out, an overlay with a writable upper directory, the
Landlock abstract-socket scope, a new session keyring, the status
descriptor) and reports supported, or unsupported naming the failing
capability. It writes nothing outside a temporary directory it removes. #169
also runs each configured provider's minimal launch (`--version` or the
like) inside the sandbox in `doctor`.

| Platform | Level |
|---|---|
| Linux with working unprivileged user, PID and IPC namespaces, a new procfs, an unprivileged overlay mount and Landlock ABI 6 (Linux 6.12 or later, Landlock enabled) | supported |
| Linux without one of them: user namespaces disabled by sysctl or by LSM policy, a nested container that refuses a new `/proc`, a kernel before 6.12 or with Landlock disabled, a filesystem that cannot be an overlay's upper layer | unsupported: `required` refuses; `preferred` follows D12.2 |
| macOS | unsupported in Wave 2A; no evidence of an equivalent boundary, and no parity is promised |
| Windows | not a first-class AutoForge platform (POSIX `flock`, a Linux-only subreaper); no backend issue |

**D14.2 (#167).** **The backend refuses everywhere else.** On a platform
that is not Linux, or where `bwrap` is missing, a request carrying a launch
description is a typed launch refusal. It never runs unsandboxed silently;
the fallback policy is D12.2's.

Behaviour that was not measured is listed in §4 and left to #169 to verify
and document.

*Rationale.* Capability is a property of the host at the moment of the
launch (namespaces, LSM policy, the container it runs in, the kernel), and
only a launch of the same shape proves all of them together. A platform
table that names the boundary's ingredients lets an operator see which one
a host lacks, and a refusal that names it is actionable.

*Rejected.*

- **Capability by binary presence or version.** Rootless podman shows that
  a working `bwrap` can still be refused a procfs (measured).
- **A weaker "supported" level without the Landlock scope or the IPC
  namespace.** "Sandboxed" would then mean different things on different
  hosts, which D1.2 forbids; such a host is unsupported, and `preferred` in
  LOCAL or `disabled` makes that visible.

### 2.15 Upgrade and compatibility (item 15)

**D15.1 (documentation only).** **Existing configurations stay valid.**
The new keys live under a new `sandbox:` block, each added by the child
that implements it, under strict key validation (`docs/configuration.md`).

**D15.2 (#168).** **Existing worktrees are reused after D6.4's check,
never refused for their age.** A worktree created before this wave, and
possibly written by unsandboxed agents, may hold planted symlinks, hard
links and rewritten admin metadata.

- Symlinks resolve inside the sandbox's own mount tree, so they reach
  nothing outside.
- The controller trusts none of its metadata (D6.3, D6.4), and an
  inconsistent binding refuses with a recovery instruction.
- Pre-existing hard links are D4.4's residual, reported by `doctor`.
- AutoForge still never deletes or moves them.

*Rationale.* A worktree may hold the only copy of an agent's unpublished
commits mid-phase. Refusing it would demand that the operator delete work
to upgrade.

*Rejected.* **Refusing every pre-wave worktree with a recovery
instruction.** It costs unpublished work, for a residual the sandbox's
resolution rules already neutralise except for hard links.

**D15.3 (#168).** **A run stopped mid-phase resumes under the new mode with
no state migration.** The mode is configuration, the candidate read is
mode-independent, and the private object directory starts empty as the upper
layer of an overlay over the shared store, so commits made before the
upgrade stay readable. D6.5's import keeps commits made under enforcement
readable after a change of mode.

*Amended by #280 (2026-10-24): to a weaker mode only by the operator's
decision.* A run stopped mid-phase still resumes under a stronger mode with
no migration of its sandbox directories, and its candidate stays readable
after a change either way. A weaker mode is refused unless the operator
passes `--accept-config-change`, which the run log records (D12.3 as
amended). REMOTE needs this pin as much as LOCAL. Its controller runs
`github.command` with the GitHub credential, every enforced REMOTE agent
writes its agent worktree, and a REMOTE `resume` without `--config` falls
back to `disabled` too. #219's recorded configuration
(`src/autoforge/config_fingerprint.py`) does not hold the mode, so both modes
are pinned. The `--config` file itself must lie outside every directory
an enforced launch writes and keeps: the checkout, every worktree
registered to the repository and its sandbox directories, and every
directory a provider's declaration makes writable (a `dir-rw` item such
as a dedicated `CLAUDE_CONFIG_DIR`, `PI_CODING_AGENT_DIR` or OpenCode
database directory, or a `state` directory), reached through a symbolic
link in one or aliased by a second hard link included (D4.4). The
providers' directories are asked of every known provider's own
declaration (D13.1) over the environment the command runs in, the one its
launches inherit, the providers no profile of the file names included:
the file under check cannot say which providers matter, since an agent
that rewrote it could name another. A directory a provider was given only
under another environment is not known to a later command; the operator
runs a run's commands from one environment. Under `required` or
`preferred` every command, the scheduler's worker and `doctor` refuse such
a file (`config_file_problem` in `src/autoforge/sandbox_policy.py`), and
`doctor` then runs no command the file names. The path is the one the
loader opens, made absolute without being normalized, and is walked as
the kernel looks it up: a `..` after a link is taken where the link
leads, a path that looks up a name in such a tree and leaves it again by
`..` is within reach (the agent can replace that entry), and the walk
must end at the file the path opens, whose own link count is read.
Checking only the file's resolved location would miss a replaced
intermediate entry; lexically collapsing `..` first examines another
file than the one loaded.

*Rejected.* **Masking the configuration file in the LOCAL root.** A file
in a writable projection cannot be masked, and a mask deeper in the tree
is undone by renaming one of its ancestors. The same holds in a
provider's writable directory, so the file is refused there too, never
masked. **Asking only the providers the file's profiles name.** The
profiles are what the file says, which an agent that rewrote it decides,
and another configuration's launches write the directories of others.

**D15.4 (#169).** **Operator-declared read-only paths are supported**, in
configuration, for toolchains, interpreters and files a build needs outside
`/usr` and `/etc` (a Linuxbrew prefix, a language toolchain under the home,
a corporate CA bundle, a registry configuration such as `~/.npmrc`). They
obey D4.2 and D4.3 and are refused at load when they name a forbidden
source, the operator's home or an ancestor, or anything that overlaps a
denied root; one that `/usr` or `/etc` already covers is dropped (C6). One
under the operator's home appears at the same place inside the private home
(C7). There are no operator-declared writable paths. A registry token in a
projected file is readable by every tool in the sandbox, and
`docs/configuration.md` says so.

*Rationale.* Builds that relied on the operator's caches, toolchains or
home files lose them under the empty root. Read-only declarations restore
what a build needs without opening a write channel out of the sandbox.

*Rejected.*

- **Operator-declared writable paths.** A write channel shared by every
  issue's agents and the operator.
- **No operator paths.** Every toolchain outside `/usr` would be
  unreachable, and operators would switch the sandbox off.

### 2.16 Sandbox metadata is never workflow authority (item 16)

**D16.1 (documentation only).** Sandbox facts are recorded in
`execution.json` and `events.jsonl`, and the mode and capability are shown
in `status` and `doctor`. No transition, verification, recovery or merge
decision reads them. A record saying "sandboxed" proves nothing about the
agent's claims: `CONTROL_RESULT` validation, GitHub read-back and candidate
verification are unchanged.

*Rationale.* A sandbox limits what an agent can reach; it does not make what
the agent says true.

*Rejected.* **Relaxing any verification for an enforced launch.** The
verifications guard against wrong claims, which a sandbox does not prevent.

### 2.17 State and schema (item 17)

**D17.1 (documentation only).** **No state protocol bump.** Sandbox facts
are additive fields in run logs, which are unversioned and never read by the
controller. The mode and the operator paths are configuration. The sandbox
directories are derived from the git directory and the worktree name, so
nothing about them is persisted. If a child ever finds a persisted field
unavoidable, it justifies it, coordinates it on #158 and #23 first, and
makes it its own bump, never reusing #160's and never combined with #126 or
#134.

*Rationale.* Nothing in this record needs to survive a crash except what the
filesystem already holds: the worktree, the private object directory and
the cache.

*Rejected.* **Persisting the enforcement state of each launch.** The
controller would then read it, contradicting D16.1.

*Amended by #280 (2026-10-24): protocol 12.* The exception was used once.
The sandbox pin (D12.3 as amended) is a persisted field, `sandbox_pin`, and
is protocol 12 of its own, announced on #23. It holds the configured mode,
never whether a launch was enforced, so D16.1 stands. A protocol-11 file
loads with `{}` and is pinned by its first `step`, `resume` or `unblock`.
A file labelled older than 12 that carries the field is refused, and so is
a protocol-12 file without it
([State and recovery](../agent-guides/state-and-recovery.md)). A sandbox
fact added later (#273) goes into the same record under a new pin schema
whose parser still reads schema 1.

## 3. Process lifecycle

For both the one-shot path (`execute()`: Claude text, OpenCode, Pi's
preflight, repository commands) and the duplex path (`start_duplex()`: Pi
RPC, Claude stream-json, SDK helpers), under an enforced launch:

| Event | Result | Facts |
|---|---|---|
| normal exit | the command's status is returned, from its `exit-code` record; whatever it left inside was SIGKILLed by the kernel as it exited | the sandbox facts (D10.4), inner leftovers "removed by teardown, uncounted"; ADR 0002's facts for the outer group, normally none |
| idle timeout, maximum runtime, loop stop | SIGTERM then SIGKILL to the outer group, reaching every inner member still in it; `bwrap` exits on the SIGTERM and the namespace is torn down, so inner processes get no grace (D10.5); the duplex path closes stdin first and gives the exit grace, as today | `timed_out` or `stopped` and `timeout_limit` as today, plus the sandbox facts |
| `KeyboardInterrupt` | as a timeout: the executor's guard kills the outer group; the duplex path closes stdin first and gives the exit grace | as today, plus the sandbox facts |
| controller death | `--die-with-parent` SIGKILLs `bwrap`; the namespace is torn down; nothing survives | none (the controller is gone); `resume` follows the existing recovery |
| setup failure | `bwrap` exits by itself with no `exit-code` record: a typed launch refusal, on the spawn-failure path, nothing attributed to the command (D10.8) | the refusal reason (bounded, redacted) and `enforced: false` |
| `bwrap` killed by a signal the executor did not send (the OOM killer, an outside kill, or the command signalling its own group) | the command's abnormal termination, on the existing failure path; never a refusal (D10.8) | as for an unsandboxed command killed by a signal, plus the sandbox facts |
| a process inside calls `setsid` | killed at teardown with the rest | as for a normal exit |

Under `required`, a host that is not capable never gets this far: the gate
refuses before the spawn (D12.2).

## 4. Platform behaviour

| Environment | Measured | Result |
|---|---|---|
| Fedora 44, kernel 7.2.x, SELinux enforcing, bubblewrap 0.12.0, git 2.55.0 / 2.56.0 (planning host and this record's) | yes | supported; every §1.2 measurement |
| rootless podman, default settings | yes (planning) | user and PID namespaces work, a new procfs is refused (`EPERM`): unsupported unless run with `--security-opt unmask=/proc/* --security-opt label=disable` |
| an AutoForge sandbox nested in a sandbox that passed `--disable-userns` | yes | the inner `bwrap` fails (`ENOSPC`) |
| hosted CI, `ubuntu-latest` | on every CI run | the `sandbox` job installs `bwrap` from the distribution, records its version and `--overlay`, the user-namespace sysctls, an unprivileged namespace launch and the Landlock ABI in its summary (`tests/sandbox_host.py`), and requires the real-backend tests wherever the runner measures capable (docs/development.md, "The outer sandbox") |
| Ubuntu 23.10 and later with AppArmor's `kernel.apparmor_restrict_unprivileged_userns` | no | #169 verifies and documents |
| Debian with `kernel.unprivileged_userns_clone=0` | no | #169 verifies and documents |
| Docker, default seccomp profile | no | #169 verifies and documents |
| WSL2 | no | #169 verifies and documents |
| a kernel before 6.12 (for example the default kernels of Ubuntu 24.04 and Debian 12), or Landlock disabled | no | no abstract-socket scope: unsupported (D14.1); #169 verifies and documents |
| macOS | n/a | unsupported in Wave 2A |
| Windows | n/a | not an AutoForge platform |

## 5. What does not change

- **`CONTROL_RESULT` stays untrusted,** and GitHub read-back stays
  authoritative (D16.1).
- **Controller-owned effects** (ADR 0004, #160–#164) stay as they are: the
  same kinds, records, reconciliation and transport. The only change to a
  candidate is how the controller reads and imports it (D6.3, D6.5).
- **LOCAL makes zero GitHub calls,** and its run contract is unchanged
  (D12.3).
- **Dry-run makes zero writes,** launches no agent and no repository command,
  and runs no probe (D12.4).
- **ADR 0002's facts keep their meaning** for the outer group (D10.4); its
  graces hold for the outer group (inner processes get none, D10.5), and
  its bounds and "one contained invocation per controller process" hold.
- **The event journal stays audit-only.**
- **No state protocol bump** (D17.1), but for #280's sandbox pin
  (protocol 12, D17.1 as amended).
- **#165's logical isolation stays in force** inside the sandbox, and alone
  under `disabled` or a `preferred` fallback. This record re-implements none
  of it.
- **ADR 0004's controller-side git hardening** (§2.7) is unchanged and still
  required.
- **Merge safety, redaction and output bounds** are unchanged.

## 6. Composition with other decisions

- **ADR 0001 (LOCAL workspace boundary).** Unchanged. Under enforcement, the
  LOCAL agent can no longer write the git directory or LOCAL state, so §2.2's
  "not achievable" items 1 and 2 become achievable for enforced launches.
  The fingerprint, the review binding and fail-closed refusal are unchanged.
- **ADR 0002 (executor).** Composed, and amended by #167 with §4e (D10.3):
  the executor's rules hold for the outer group, the namespace's teardown
  removes everything inside at the main command's exit, and inner
  processes get no kill grace (D10.5).
- **ADR 0003 and `docs/pi-policy.md`.** Pi's RPC runs through `bwrap`'s
  stdio unchanged. §7's advice to use a container, VM or dedicated UID is
  met by an enforced launch for the filesystem and process boundary, and not
  for the network. §8's `auth.json` exposure becomes a `dir-rw` projection
  (D7.1). Context files from directories above the worktree no longer load
  under enforcement: the parents of a projection are empty directories.
  #168 updates §7 and §8.
- **ADR 0004 (authority boundary).** Extended. §2.11's first three "does not
  guarantee" items (absolute-path reads, absolute-path writes, shared local
  repository state) are guaranteed for enforced launches (G1, G2, G5),
  within §2.2's residuals; the network item is not. Its "integrity of the
  shared object store" item is closed for enforced launches by D6.1 and
  D6.5; D7.5 stays. Its §4 note on Wave 2A holds: no agent-written shared
  ref is relied on, the worktree's admin dir stays writable, and the
  controller's runtime directories, git contexts and read-context
  directories are never projected writable. The sandbox directories (D8.4)
  are not runtime state in that sense: the controller creates them for
  agents to write, never reads the cache, and reads the private object
  directory only through the strict import (D6.5).
- **#165 (logical isolation).** Consumed, not duplicated. Its
  controller-owned configuration locations are projected read-only (D8.3).
  Its preflight's refusal of token-bearing remote URLs and repository-local
  credential helpers is what makes a read-only common dir acceptable (§2.2).
- **#149 and #132.** They select what a provider loads; the sandbox projects
  exactly that (D8.3).
- **#148, #151 and #152.** One sandbox and one containment owner per SDK
  helper and its server (D10.7).
- **#161, #163 and #164 (merged).** Their candidate rule stands; the read
  and the import change (D6.3, D6.5).
- **#171 and #172 (Wave 2B).** The export stays a controller-owned workspace
  of its own launch class; #171 takes the export location #168 chooses, and
  #172's validation in an export uses the same class. A policy digest for
  #172 can be derived from the role-level inputs of D13.2.
- **#23.** Not involved unless D17.1's exception is ever used. #280 used
  it (protocol 12).

## 7. Decision ownership

Each decision has exactly one implementing child, or is documentation only.

| Child | Decisions |
|---|---|
| #167 | D4.1, D4.2, D4.3, D7.2, D9.1, D10.1–D10.6, D10.8, D11.1, D13.3, D13.4, D14.2 |
| #168 | D2.1, D3.1, D5.1, D6.1–D6.7, D7.1, D8.1–D8.4, D9.2, D10.7, D12.1, D12.3, D13.1, D13.2, D15.2, D15.3 |
| #169 | D1.2, D4.4, D7.3, D8.5, D9.3, D12.2, D12.4, D13.5, D14.1, D15.4 |
| documentation only | D1.1, D7.4, D15.1, D16.1, D17.1 |
| #280 (amendments) | D12.3, D15.3, D17.1 |

### 7.1 Where this record differs from a child issue's body

- **#167, Target contract item 5:** a setup failure is a `bwrap` that
  exited by itself with no `exit-code` record; a `bwrap` ended by a signal
  the executor did not send is the command's abnormal termination. The
  `child-pid` record is not the discriminator: it is written for a missing
  bind source and an exec failure too (D10.8).
- **#167, item 7:** inner leftovers are never counted; the record says
  "uncounted" (D10.4).
- **#167, item 3:** masks are remounted read-only after their carve-outs are
  mounted, never before (§1.2).
- **#167, item 2:** the forbidden-source list is D4.3's, including the
  operator's home and its ancestors.
- **#167:** an IPC namespace, a new session keyring and the Landlock
  abstract-socket scope are mandatory (D9.1, D10.1); the backend mounts
  overlays (D13.4); a `dir-rw` item on a provider's operator configuration
  root is refused (D7.2); inner processes get no kill grace (D10.5).
- **#168, items 5 and 3:** candidate addressing through the common dir and
  the registry-based worktree check apply in every mode, `disabled` included
  (D6.3, D6.4), so under `disabled` the behaviour is `main` plus #165 plus
  those two reads. The import refuses an alternates file or a link in the
  private object directory by blocking (D6.5).
- **#168:** the private object directory is the upper layer of an overlay
  over `<common>/objects`, never named by `GIT_OBJECT_DIRECTORY` (D6.1).
  REMOTE masks `<common>/modules` (D6.1). Only the launches that produce a
  change share a persistent cache; REVIEW gets none (§2.5, D8.1).
- **#168:** the opt-in isolated network for repository-defined commands is
  #168's (D9.2), and the export launch class gets no persistent cache
  (§2.5).
- **#169, item 1:** the capability also requires an IPC namespace, an
  unprivileged overlay and Landlock ABI 6 (D14.1).
- **#169, item 5:** operator-declared projections are allowed, read-only only
  (D15.4). #169's `doctor` also reports hard links (D4.4), the sandbox
  directories (D8.5) and loopback TCP listeners (D9.3).

## 8. Tests the implementing children must add

All of them use fake credentials, fake sockets, fake files and `tmp_path`
repositories only. No real credential, login or provider call.

**#167 (backend and compiler):**

- Forbidden sentinels: a sentinel outside every projection is unreadable by
  absolute path; fake `~/.config/gh/hosts.yml`, `~/.ssh/id_*`,
  `.git-credentials` and a fake keyring file under a fake operator home are
  unreachable; a fake listening socket standing in for the SSH agent socket
  is unreachable.
- Symlink and traversal escapes: a symlink planted in a writable projection
  that points at a sentinel resolves to nothing; `..` from the workspace
  reaches nothing of the parent.
- Bind semantics: a read-only projection refuses write, unlink and rename; a
  writable one allows them; a mask hides what is under it and refuses
  writes; a carve-out under a mask works; a pinned file refuses write and
  rename; a hard link across projections fails with `EXDEV`; a single-file
  writable bind refuses rename; an overlay leaves its lower directory
  byte-identical and puts writes and whiteouts in its upper directory; a
  `--size` bound stops a write at the bound.
- Local IPC: a SysV segment and a session-keyring key created on the host
  for the test are unreachable inside; a test listener on a host abstract
  address refuses the connection (`EPERM`), and an abstract socket created
  inside works.
- Nested namespaces: a process that creates a nested user and mount
  namespace cannot unmask a path or remount a read-only bind read-write.
- Every compiler refusal of D4.2 and D4.3, before any spawn, naming the path
  and the rule; C4's mask of a denied root that is a direct child of a
  writable projection and refusal of a deeper one; C6 dropping a covered
  read-only declaration; a `dir-rw` item on a declared operator
  configuration root refused (D7.2).
- Processes: a host process is neither visible nor signallable; a `setsid`
  daemon and a same-group child that traps SIGTERM are gone after a normal
  exit, a timeout and a cancellation, with the facts recorded; a duplex round
  trip and shutdown work through the backend; the spawn-thread invariant.
- Setup failure: a missing bind source, an exec failure and a refused
  namespace (a fake `bwrap` on `PATH`) are launch refusals, distinct from a
  command that exits 1; a command that kills its own process group
  (`kill(0, SIGKILL)`) is an abnormal termination, never a refusal.
- No description: the existing executor and duplex suites run unchanged.

**#168 (routing and git surface):**

- A sandboxed fake agent, validation command and pre-merge command cannot:
  read or modify state, the run logs or the lock; read or modify the
  controller checkout; read another issue's worktree or admin dir; write
  outside the allowed paths; reach credential sentinels under `HOME` or
  `XDG`, `~/.ssh` keys or a fake SSH-agent socket; escape through a planted
  symlink or `..`; move a shared branch, write `config`, hooks or
  `objects/info/alternates`; redirect the controller's candidate read.
- They can: read and write their workspace; commit locally on the detached
  `HEAD`; run a fake test and build that use temp and cache; execute the
  provider binary; read the projected configuration and auth fixtures, and
  write them where `dir-rw`; terminate with no surviving descendants,
  recorded as #167 defines.
- Git: the candidate is read through the common dir in every mode, and a
  worktree whose `.git` file or `commondir` was rewritten to a decoy is
  refused or read correctly, never redirected; the import brings exactly the
  candidate's objects into the shared store, and an `info/alternates` file or
  a link in the private object directory blocks while an overlay whiteout
  is skipped; a temporary repository created inside keeps its own objects
  and is readable by a git process with no `GIT_*` variable; both ref
  backends where the installed git supports them.
- Recovery: a crash between a launch and its import leaves the commit
  readable at the next launch or candidate read (D6.5); a run stopped under
  `disabled` resumes under `required`, and, with `--accept-config-change`,
  the other way round, with the candidate readable (D15.3 as amended by
  #280; without it the weaker mode is refused).
- The environment: `HOME`, `XDG_*` and `TMPDIR` are rewritten, a dangling
  path variable is unset and named, `PATH` keeps only reachable entries.
- A per-launch home: a file an agent writes into the home is gone at the
  next launch. The cache: a file an ANALYZE_EXECUTE launch writes into it is
  present for the next FIX launch and absent from the next REVIEW launch.
- The isolated network (D9.2): a validation command with it on cannot reach
  a test loopback listener, and the plan and the execution record say so.
- LOCAL: the git dir is read-only (a commit, stash or branch fails), LOCAL
  state and a REMOTE `.autoforge/` in the root are masked, a worktree nested
  deeper in the root refuses the launch, and there are zero GitHub calls
  (`ExplodingGitHub`).
- Every provider declaration compiles, and one naming a forbidden source is
  refused before launch.
- A full scripted REMOTE loop and a LOCAL loop under `required`.

**#169 (modes, doctor, parity):**

- The probe reports each failing capability distinctly: `bwrap` missing, a
  user or IPC namespace refused, a procfs refused (a fake `bwrap`, with the
  measured podman behaviour cited), a bind refused, an overlay refused, the
  Landlock scope unavailable.
- `required`, and `preferred` in REMOTE, refuse before any spawn, with state
  byte-identical, REMOTE and LOCAL.
- `preferred` in LOCAL falls back visibly in the plan, `status`, stderr and
  every execution record, never as "sandboxed"; a setup failure after the
  gate allowed a launch never falls back.
- `disabled` is reported by `doctor`, the plan and `status`.
- `doctor` reports keyring-only provider logins, a `dir-rw` item that would
  be refused, hard links (the sandbox directories included), loopback TCP
  listeners, the sandbox directories and their sizes, and operator paths
  that would be refused, with no secret in its output.
- A dry run launches nothing, the probe included.
- The parity suite: every provider path (Claude Code CLI, OpenCode CLI, Pi
  RPC and its preflight, the SDK helpers or a provider-neutral test adapter,
  repository-defined commands) meets the same contract with no engine branch.

## 9. Non-goals

- No production code, tests, prompts or configuration in this change.
- No change to the text of ADR 0001, 0002, 0003 or 0004. #167 adds ADR 0002
  §4e (D10.3).
- No re-implementation of #165's logical isolation.
- No SDK settings, plugin, MCP, tool, hook, skill, agent, permission or
  auth-mode policy (#149), and no Pi resource policy (#132).
- No change to which writes are controller-owned, and no new effect journal,
  receipt or recovery log (#160).
- No network egress mediation, proxy, firewall or DNS policy.
- No daemon, service, broker, container runtime dependency, separate-UID
  installer, privileged helper or seccomp filter.
- No macOS or Windows backend.
- No class or function names imposed on the implementation.

## 10. Consequences

- **On a capable Linux host with `required`,** the absolute-path cases that
  ADR 0001 and ADR 0004 could not close are closed for every launch
  AutoForge makes: an agent or a repository command can no longer read the
  operator's credentials by path, touch controller state, another worktree
  or the operator's refs, or signal the operator's processes.
- **The residuals are explicit:** the network, including a loopback
  service that acts on request, which is an escape; the projected model
  credential; the readable repository and the operator's unpushed work in
  the shared git directory; pre-existing hard links; credential-shaped
  values in repository configuration that #165's preflight does not refuse;
  and a `dir-rw` directory's persistence.
- **Enforcement needs a recent kernel:** Linux 6.12 or later with Landlock
  enabled, unprivileged user namespaces and overlay mounts (D14.1). Older
  hosts can run `disabled`, or `preferred` in LOCAL, and are told so.
- **Builds pay for a fresh home.** Tool installs and downloads outside
  `XDG_CACHE_HOME` repeat at every launch, and toolchains outside `/usr` must
  be declared read-only by the operator.
- **Not supported under enforcement in Wave 2A:** keyring-only provider
  logins (D7.3); a `dir-rw` login in the provider's own configuration root,
  which needs a dedicated directory and a login there (D7.2); Git LFS
  operations that write into the common dir (`<common>/lfs` is read-only;
  not measured); a LOCAL checkout that is, or contains, the operator's home
  (D4.3); a LOCAL root with another worktree nested below its top level, for
  example under `.claude/worktrees/` (C4). Submodules work in a REMOTE
  worktree, whose submodule repositories live in its own admin dir
  (measured).
- **The operator's `git gc` and `git fsck` fail while an issue worktree's
  `HEAD` names a commit that is not yet imported** (measured: `fatal: bad
  object worktrees/<id>/HEAD`, and `missing blob`). The window is a running
  enforced launch, or a crash before the import; the controller imports
  after every enforced launch and before the next launch or candidate read
  (D6.5).
- **Inspecting an issue worktree on the host** may need the private object
  directory as an alternate until the controller has imported the agent's
  commits (`GIT_ALTERNATE_OBJECT_DIRECTORIES=<git common
  dir>/autoforge/sandbox/<key>/objects`); `docs/usage.md` says so (#168).
- **A controller killed with SIGKILL** now takes its running agent with it
  (D10.6).
- **Documentation follows behaviour** in the implementing children:
  `docs/agent-guides/architecture.md` (the executor and sandbox boundary,
  #167; provider declarations and engine facts, #168),
  `secrets-and-logging.md`, `state-and-recovery.md` (run logs are no longer
  "where the agents write" when sandboxed), `github-safety.md` (the threat
  model, candidate addressing and the import), `testing.md`,
  `docs/configuration.md`, `docs/usage.md`, `docs/local-mode.md`,
  `docs/pi-policy.md` §7 and §8, README key properties, and AGENTS.md
  "Runtime model" (the isolation terms, #169). If implementation
  contradicts a decision here, the implementing child amends this record,
  with a recorded rationale.
