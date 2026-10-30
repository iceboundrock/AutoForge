# ADR 0009. Retiring LOCAL mode: legacy runs, the state protocol, sequencing and the baseline

- **Status:** proposed. This is a design record; nothing in it is
  implemented, and it changes no behaviour. RET-A2 and RET-A3 start once
  the maintainer signs off its decisions on #384. No sign-off has been
  given yet; when it is, this line links it and records what it changed.
  ADR 0001 stays accepted, and nothing in any ADR is marked superseded,
  until the removal ships (D11).
- **Decides:** #384 (RET-A1), for EPIC #383.
- **Planned against:** `main` `4b03151` (2026-10-30); every line number in
  this record is at that commit. #384 was written against `01c5b7e`. Of the
  ten commits since, none touches LOCAL's commands, state, transitions or
  prompts. Two change what a LOCAL run meets, and the `CHANGELOG.md`
  entries say so: #395 relaunches a model API outage in REMOTE only, and
  under #302 a LOCAL run whose contract recorded `prompt_version: v1`
  cannot resume once that line is deleted.
- **Will amend:** ADR 0001, superseded except for the parts REMOTE still
  relies on, and the LOCAL decisions of ADR 0003 to 0008 that Appendix A.4
  lists, superseded in part. Both take effect when the removal ships (D11).
- **Related:** ADR 0001 (LOCAL mode), ADR 0005 (the LOCAL launch classes
  and `preferred`), ADR 0006 (LOCAL candidate identity, the convergence
  rule, the `local` certificate), ADR 0008 (the scheduler, REMOTE-only);
  #23 (state protocol numbering); #135 (the live Pi smoke test); #155
  (provider parity tests); #255 (the Public Alpha launch EPIC); #295 (the
  `0.1.0a0` registration prerelease).
- **Inputs:** the repository at `4b03151` and #384's text. EPIC #383,
  #155 and #255 could not be read when this record was written; what it
  assumes about them is stated where it is used (§5). #384's review of
  #155 and #255 is therefore not done, and that acceptance item stays open
  until both are read (§5).

## 1. Context

### 1.1 What retiring LOCAL means

EPIC #383 removes LOCAL mode: `autoforge local` and the workflow behind it,
which drives agents over a `features/<slug>.md` specification and binds
reviews to a fingerprint of the working tree instead of a pull request.
Why it goes is the EPIC's decision and is recorded there; this record does
not argue it. It decides how: what is deleted and what stays, what happens
to a LOCAL run that already exists, what the state protocol does, how the
removal is released, in which order the work lands, and what the suite
costs before any of it, so that the removal's effect can be measured
rather than claimed.

LOCAL is not a module. It is a second topology over the shared engine and
state:

- its own commands (`local init`, `local run`, `local doctor`) and a
  mode-aware `status`, `step` and `resume`;
- its own transition table (`LOCAL_LEGAL_EDGES`; `DONE` straight after a
  clean `REVIEW`; no `unblock` edge), prompt templates
  (`prompts/local_*.md`) and result models;
- its own candidate identity: the workspace walk and fingerprint
  (`local_workspace.py`) and the durable run contract (`run_contract.py`),
  where REMOTE binds a PR, a HEAD and a base;
- twelve state fields of its own (`feature_spec_*`, `base_head_sha`,
  `base_branch`, `local_run_contract`, the two fingerprints,
  `local_fix_rounds`, `local_pending_*`, `baseline_dirty_paths`), written
  at their empty values into every REMOTE state file, and its own default
  state directory, `<git dir>/autoforge/state`;
- its own branches inside shared mechanisms: two launch classes in the
  outer sandbox, the only fallback `sandbox.mode: preferred` has, a
  workspace variant of `ValidationRecord`, a `local` certificate kind, a
  LOCAL subset of `doctor`, the `local:` config block.

By the inventory's estimate (Appendix A.1), about 4,500 of the 74,840
lines under `src/autoforge/` go. 387 of the suite's 4,254 test functions
touch LOCAL, and 46 of those carry coverage REMOTE needs (Appendix A.2);
the six modules that exist only for LOCAL are 568 of the 11,723 collected
items and 6.4% of the measured test time (Appendix B).

### 1.2 What is in the field

- `autoforge-cli` `0.1.0a0` was published to PyPI on 2026-10-28 (tag
  `v0.1.0a0`, commit `6e9495a`) and ships LOCAL mode. It is a pre-Alpha
  registration prerelease, not the Public Alpha (`CHANGELOG.md`,
  `docs/releasing.md`).
- It writes state protocol 12, as `main` does at `4b03151`. `state.py`,
  `run_contract.py`, `local_workspace.py`, `evidence.py`, `certificate.py`,
  `locking.py`, `safefs.py` and `run_namespace.py` are byte-identical
  between the tag and `4b03151`: today the two read each other's state
  files and contend for the same lock.
- How many LOCAL runs exist is unknown. AutoForge has no telemetry, and
  PyPI's download counts were not consulted; they could not say which mode
  an install used.
- `SECURITY.md` supports only the latest commit of `main`; `0.1.0a0` is
  already "anything older".

### 1.3 Constraints

From #384: no silent coercion, deletion or migration of a LOCAL run; a
LOCAL fingerprint never becomes PR evidence; a `local:` block is a strict
error; ADR 0001 is not marked superseded before the removal ships. From
the repository contract: a state file's protocol label, never its shape,
decides whether it loads, and a change to persisted state takes the next
number, announced on #23 (`state-and-recovery.md`, "Numbering a new
protocol"); configuration keys are validated strictly; REMOTE behaviour,
its verification and every safety gate stay as they are.

## 2. Decisions

### D1. What goes and what stays

**Delete what only LOCAL reaches; keep every shared mechanism, minus its
LOCAL branch.** Appendix A classifies each reference with one of four
labels, and the removal follows the label:

- **DELETE:** LOCAL-only; nothing REMOTE or the scheduler uses depends on
  it.
- **PRESERVE SHARED:** REMOTE uses it too. It stays; a LOCAL branch inside
  it goes.
- **LEGACY READ/REFUSE:** survives, reduced, so that a `0.1.0a0` LOCAL
  artifact is recognised and refused or shown read-only (D3 to D6).
- **REPLACE:** a shared purpose that today is implemented or tested only
  through LOCAL. The replacement lands first (D12, step S1), or REMOTE
  breaks or loses coverage.

Three findings of the inventory shape the work more than the rest:

- **REMOTE reads the working tree through LOCAL's reader.**
  `ControllerEngine.workspace()` (`engine.py` :1806–1823) builds a
  `LocalWorkspace` from `config.local.*` for every run, and REMOTE uses its
  git plumbing (`root`, `git_dirs`, `head_sha`, `branch`, the status
  entries, `uncommitted_paths`) for the agent worktree root, worktree
  creation, sandbox facts, the candidate read, the checkout anchor and the
  uncommitted-work note. That plumbing moves to a mode-neutral module that
  reads no `local:` key before `local_workspace.py` and the `local:` block
  can go. The walk, the fingerprint and the feature specification go.
- **`safefs.py` stays.** It is the write boundary of REMOTE's state, run
  logs and read context, as ADR 0001 §7 ("REMOTE mode") recorded when LOCAL
  introduced it, and the scheduler's namespaces and bookkeeping open their
  directories through it.
- **Every REMOTE state file carries LOCAL's twelve fields**, and the
  loader refuses a field it does not know (`state.py` :1086–1100). Deleting
  the fields without a drop step would make every existing REMOTE state
  unloadable. D8 is that step.

### D2. LOCAL is frozen until the removal ships

From the sign-off of this record until the removal ships, LOCAL gets no
new feature and no new configuration key. A LOCAL defect is fixed only
when it is a security or data-loss defect, or when the fix is in shared
code anyway. A shared change need not extend LOCAL: it may refuse a LOCAL
run explicitly, naming this record, instead of implementing LOCAL's half.

Without the freeze, every shared change until the removal pays LOCAL's
cost twice: once to build the LOCAL half and once to delete it. Its cost
is that a LOCAL operator on `main` may meet a new refusal before the
removal; that operator can stay on `0.1.0a0` (D9).

### D3. A legacy LOCAL run is recognised by its content

**A legacy LOCAL run is a state file whose top-level `mode` is the string
`"LOCAL"`, whatever its protocol label and wherever it lies.** LOCAL's
default location keeps one probe, because an operator who upgrades and
types `autoforge resume` must be told about the run rather than told there
is none:

- A command given `--state-dir` reads that directory's `state.json` alone,
  as today: the flag is the operator's answer to which run is meant. A
  file there with `mode: "LOCAL"` is a legacy LOCAL run.
- A command not given `--state-dir` keeps today's two-location rule
  (`_bind_existing_run`, `cli.py` :634–667), whatever `state_dir` is
  configured to: it looks for `state.json` at the configured location
  (`.autoforge`, or a non-default `state_dir`, which LOCAL honoured too)
  and at `<git dir>/autoforge/state/`, where `<git dir>` is the checkout's
  own git directory (`<common dir>/worktrees/<name>` in a linked
  worktree). A non-default `state_dir` does not end the probe today
  (`engine.local_state_paths()` ignores it, `engine.py` :3061–3063), and
  it does not after the removal: a LOCAL run left at the default location
  from before the setting was changed is still found. The one match found
  is read to confirm its `mode`.
- Two matches stay a refusal, as today, with a message that names both
  files, calls each whose `mode` is `"LOCAL"` a legacy run this controller
  cannot continue, and names `--state-dir`. Picking one silently would be
  right only when the operator meant it; the refusal costs one flag.

Detection reads at most `MAX_STATE_FILE_BYTES` through the state root's
capability (`safefs.py`), as a load does, and decides on `mode` alone,
before any protocol, schema or contract check: a legacy file is neither
corrupt nor foreign and must not be reported as either. It never builds an
`AutoForgeState`, so it needs none of the LOCAL validators the removal
deletes, and it cannot fail the way `status` of a LOCAL run whose
configuration drifted fails today (the contract gate, `engine.py`
:3216–3219).

### D4. What each command does with a legacy LOCAL run

**No command writes anything under a legacy run's state directory**: no
relabel, no sandbox pin, no recorded configuration, no run log, no
`.corrupt-*` rename. Every refusal exits 2 (`StateError`) and names the
state file, the `controller_version` that wrote it (untrusted text,
redacted and bounded) and the fallback (D9).

| Command | Behaviour on a legacy LOCAL run |
|---|---|
| `status` | A read-only summary of a fixed field set: `mode`, `protocol_version`, `controller_version`, `run_id`, `phase`, `created_at`, `updated_at`, `feature_spec_path`, `block_reason`. A field is shown only when it has the type `0.1.0a0` writes, redacted and bounded like every `status` line; any other value prints as unreadable. Then the fallback. Exits 2, so that a script never reads a run this controller cannot continue as healthy. |
| `status --json` | `{"legacy_local_run": {...the same fields...}, "supported": false, "fallback": "..."}`, redacted. Exits 2. Not the raw document: validating it would need LOCAL's schema, which the removal deletes, and the file is already the operator's to copy. |
| `status --action` | Refused: a LOCAL run has no operator actions. |
| `step`, `resume` | Refused before any phase, launch or write. Taking the lock is allowed: it lives in the common dir, not in the state directory. |
| `unblock` | Refused, as today (LOCAL never had an unblock edge), now by the detector rather than the engine. |
| `run` | Refused when its state directory holds a legacy LOCAL run, under `--force` too. `--force` moves an unreadable state aside as `.corrupt-<timestamp>`; a legacy file is not unreadable, and once renamed the fallback cannot find it. The operator moves it. |
| `doctor` | One row: WARN when `state.json` exists at the default LOCAL location, naming it and the fallback. `lstat` only. An empty `<git dir>/autoforge/state` is no run (`local doctor`'s probe can leave one). |
| `epic ...` | The scheduler never created a LOCAL run. Its reverse guard keeps treating a legacy LOCAL state at `config.state_dir` as holding no issue (`ownership.py` :227), through the detector: turning it into a load problem would warn on every scheduling pass for a run that blocks nothing. A namespace state with `mode: "LOCAL"` stays a mismatch, as today (`scheduler_state.py` :1460). |

No `export` command is added. `status --json` is today's export, and the
legacy summary is its legacy form.

### D5. `autoforge local` and the `local:` block refuse explicitly

- **`autoforge local` stays as a stub** that exits 2 for any subcommand or
  argument, saying LOCAL mode was removed and in which version, and naming
  the fallback. It reads no configuration, takes no lock and touches no
  file. A bare argparse error would say only "invalid choice: 'local'". It
  stays until a later decision removes it.
- **A top-level `local:` key is a load error** for every command that
  reads configuration, `doctor` and the `epic` commands included, naming
  the removal and the remedy: keep the file as it is if a LOCAL run may
  still need the fallback, which reads the run's configuration `local:`
  block included (D9); delete the block from the copy the new release
  reads, and move `local.validation_commands` to `validation.commands` if
  REMOTE runs should run them. Today removing `local` from the known keys
  would already fail such a file, with "unknown key(s) under 'the top
  level': local"; the special case (a few lines beside the section-level
  removed-key mechanism, `config.py` :1225) exists to name the remedy.
  Ignoring the block with a warning was rejected: the operator would
  believe the validation commands it lists still run.

### D6. No LOCAL evidence becomes REMOTE evidence

A LOCAL run's fingerprint, run contract, `ValidationRecord`s, `local`
certificate and findings are never converted into a PR review binding, a
REMOTE `ValidationRecord`, a REMOTE certificate or anything a merge gate
reads, and no command adopts a LOCAL run into a REMOTE one.

The two identities do not denote the same bytes. A LOCAL certificate binds
a walk of the working tree under the run's workspace policy: ignored files
hashed, excluded entries named, mode bits, symbolic-link text, no commit.
A REMOTE certificate binds a commit's tree, exported byte-exact and proven
to hash back (ADR 0006 D6). The tree an operator later commits differs in
exactly the entries the two policies treat differently, so a converted
certificate would claim a review nobody did. An operator who wants a LOCAL
change merged commits it and runs REMOTE on an issue; the LOCAL review
does not carry over.

### D7. `sandbox.mode: preferred` keeps its name and means `required`

`preferred` falls back to logical isolation only in LOCAL; in REMOTE it
already is `required` (ADR 0005 D12.2; `sandbox_pin.mode_strength`,
`config.py` :743–748). With LOCAL gone it is `required` everywhere. It
stays an accepted value with that meaning, documented as such, and the
fallback code (`engine.py` :2646–2684, the `local=` arguments of the pin,
the policy and `doctor_sandbox.py`) goes.

Refusing `preferred` would make every configuration that names it, and
every run pinned to it, fail to load, for no gain in safety: no REMOTE
launch changes, a REMOTE pin of `preferred` compares exactly as before,
and REMOTE's validation policy keeps the configured mode as it records it
today (`ValidationPolicy.requires_enforcement` already reads REMOTE's
`preferred` as `required`). The cost is a value that is now only an alias;
the documents say so.

### D8. The state protocol takes the next number

**Removing LOCAL's fields from the state schema is a persisted change and
takes protocol N + 1**, where N is `__protocol_version__` on `main` when
the removal's PR is ready to merge (13 if no other bump lands first),
announced on #23 with the rule below. No number is reserved now.

- **What N + 1 writes:** no LOCAL field. It keeps `mode`, always
  `"REMOTE"`: `status --json` consumers read it, and the detection of D3
  stays the same across labels.
- **What an older REMOTE file loads as:** what today's controller loads it
  as, with the LOCAL fields dropped, and nothing more. The drop is not a
  new migration path: every per-protocol rule of the loader
  (`AutoForgeState.from_dict`, `state.py` :765–1084, up to the
  unknown-field check) stays and runs first, its migrations and its
  refusals alike. A protocol-1 to 3 file parked in
  `READY_FOR_MERGE` or `MERGE` (`_legacy_review_binding_refusal`), a
  protocol-1 to 4 file whose replan journal is in flight at a stage its
  label could not bind (`replan_txn.legacy_journal_refusal`), a file
  carrying state its label never wrote, a protocol-7 to 12 file missing a
  field its label always wrote, and protocol 6 stay refused, unchanged,
  because the binding they lack cannot be reconstructed now any more than
  before. The LOCAL fields are dropped only from a file those rules
  accept, and only when each is absent or holds the empty value every
  REMOTE save wrote (`""`, `0`, `{}`, `[]`). They arrived on 2026-09-10
  and 11, under protocol 1, so a protocol-1 file carries none, some or all
  of them, and every protocol-2 and later save carries all twelve. It is
  relabelled on its next save. A REMOTE file with a non-empty LOCAL field
  is a hand edit or a foreign file and is refused, unchanged, as today.
- **What an older LOCAL file is:** a legacy LOCAL run (D3, D4). Never
  loaded as state, relabelled or migrated.
- **What N + 1 refuses:** a file labelled N + 1 that carries any LOCAL
  field, or a `mode` other than `"REMOTE"`, is a hand edit or a foreign
  file. A workspace-variant `ValidationRecord` or a `local` certificate in
  any REMOTE file is corruption, as ADR 0006 D2.7's kind check already
  makes it. The records' and certificates' own `schema` stays 1: the
  REMOTE variants do not change shape, and the parser only narrows.

Two alternatives were rejected. Keeping label 12 with the LOCAL fields as
permanently empty columns leaves LOCAL's vocabulary in every future state
file and breaks the numbering rule. Dropping the fields under label 12
lets `0.1.0a0` load and continue a REMOTE run the newer controller saved,
running older code over state it no longer describes. Under N + 1,
`0.1.0a0` refuses every file the newer controller saved (`unsupported
protocol_version`), so the fallback installed beside the newer release
acts only on files `0.1.0a0` could have written itself.

`status --json` of a REMOTE run loses the twelve empty LOCAL keys. That is
a change to its document and goes into the changelog entry (D10).

### D9. The fallback is `0.1.0a0`, and it is unsupported

**`autoforge-cli==0.1.0a0` stays installable as the way to finish or
inspect a LOCAL run**, run from an environment of its own so that it never
replaces the operator's newer install, from the run's checkout, with the
configuration file the run was started with:

```bash
cd <the run's checkout>
uv tool run --from 'autoforge-cli==0.1.0a0' autoforge --config <the run's configuration> --state-dir <dir> status
uv tool run --from 'autoforge-cli==0.1.0a0' autoforge --config <the run's configuration> --state-dir <dir> resume
```

- **The run's configuration is part of the fallback.** `0.1.0a0` finds no
  configuration file by itself: without `--config` it runs on its
  defaults. It holds every command on a LOCAL run, `status` included, to
  the run contract, which records the repository root, the state
  directory, the workspace policy (`local.exclude` and both cost bounds),
  `local.validation_commands`, `local.max_fix_rounds`,
  `workflow.max_total_steps` and the prompt version, and it holds `resume`
  to the run's sandbox pin. A run started under any non-default value of
  these refuses both commands without its own file. So the operator keeps
  that file as it was, `local:` block included, apart from the copy the
  new release reads (D5), and passes it; `--config` is left out only for a
  run started without one. The repository root is the current directory's
  (`0.1.0a0`'s engine takes `.`), which is why the commands run from the
  run's checkout. `--state-dir` names the directory D4's refusal names. The
  refusal's fallback text shows these commands with that directory filled
  in and the configuration as a placeholder, since the new release cannot
  know which file the run was started with.
- **It is not yanked** while any release names it as the fallback. An
  exact `==` pin still installs a yanked release, but installers warn, and
  the fallback should not rest on that.
- **It is unsupported.** `SECURITY.md` already says so of every version
  but the latest `main`, and no `0.1.0a0.postN` can carry a fix: the
  version scheme has no `.postN` (`docs/releasing.md`). A LOCAL defect
  found after the removal is not fixed. `main` already has fixes
  `0.1.0a0` lacks that reach LOCAL launches too, among them #394 (a Claude
  CLI that runs on after its stream failed is stopped) and #347 (Claude
  Code's login-shell `PATH`); the changelog entry names them.
- **It shares a repository with the newer release safely.** The two take
  the same lock on the git common dir (`locking.py` is unchanged since the
  tag; the removal keeps it so, or this record is revisited), and D8 keeps
  each from loading state the other wrote under a newer protocol.
- **Without PyPI**, the tag is the source: `git checkout v0.1.0a0`, then
  `uv sync --locked`, in a clone of its own, and that clone's
  `.venv/bin/autoforge` is run in place of `uv tool run ... autoforge`
  above, from the run's checkout and with the same arguments.

### D10. Release communication

- **The removal ships in the next `0.1.0aN` prerelease**, with no
  deprecation release before it. Nothing before 1.0 promises
  compatibility (`docs/releasing.md` sets no deprecation rule), the only
  release with LOCAL is a pre-Alpha registration upload, and a deprecation
  release would be a release cut to say what the removal's own refusals
  (D4, D5) say at the point of use.
- **`CHANGELOG.md`** gets a `LOCAL mode is removed` entry under
  `## Unreleased`, marked **Behaviour change:**, in the PR that closes the
  entry points (S2): what goes, what each command does with a legacy run,
  the `local:` error, the `local` stub, `preferred`'s meaning; S3 adds the
  protocol bump with its migration rule and the `status --json` change.
  The release notes are that section (`docs/releasing.md`).
- **`docs/local-mode.md` becomes a short legacy page** (where a LOCAL run's
  state and logs are, what each command does with it, the fallback) rather
  than disappearing, and the README keeps a section headed `Local mode (no
  GitHub)` that says the mode was removed and links it. `0.1.0a0`'s PyPI
  description links `https://github.com/iceboundrock/AutoForge#local-mode-no-github`
  absolutely; that link should land on the explanation.
- **The other documents** are rewritten as Appendix A.3 lists, in S2 and
  S3, not before: until then LOCAL works, and the documents must describe
  it. That includes README's "Supported now" maturity tier, which lists
  LOCAL and which the Public Alpha is held to (§5).

### D11. ADR status follows the code

Nothing is marked before the removal ships. The PR that ships it (S3)
adds to ADR 0001's header: superseded by this record, except §5.4 (one
capability boundary for every controller write), the parts of §5.5 that
bound the run log and the state file, and the hard-link limitations of
§8.8 and §8.10, which keep describing `safefs.py` for REMOTE. Each LOCAL
decision of ADR 0003 to 0008 that Appendix A.4 lists gets a one-line note
naming this record. Accepted records are not rewritten: what they decided
for LOCAL stays readable as history, which the legacy handling and the
fallback still rely on. ADR 0007's LOCAL obligations (its LOCAL
interaction identity, D8.2, D8.5) are unimplemented and can be dropped
from the record by the child issue that implements them, without waiting.

### D12. Sequencing and exit tests

The removal is four steps after this record. Each leaves `main` green and
every REMOTE behaviour unchanged, and each has exit tests that must exist
and pass before it merges. #384 names RET-A2 and RET-A3 as the issues it
blocks; how S1 to S4 map onto EPIC #383's sub-issues is the EPIC's to
confirm, since this record could not read it.

| Step | Lands | Exit tests |
|---|---|---|
| S1. Replace first | Every REPLACE row of Appendix A: REMOTE's git reader out of `local_workspace.py` and free of `config.local`; a REMOTE or mode-neutral test for each shared mechanism tested only through LOCAL (A.2.3), the live Pi smoke test in a REMOTE form among them; the helpers other modules import from `test_local.py` moved to a shared home. LOCAL still works. | Each REPLACE row names its new test, and it passes with LOCAL still present; among them, H's two REMOTE candidate-validation tests of the credential preflight, without which S3 may not delete `test_local.py`'s. No REMOTE test is weakened. A test fails if `engine.workspace()` (or its successor) reads `config.local`. |
| S2. Close the entry points | D3 to D7: the detector, the `status` summary, the refusals of `step`, `resume`, `unblock`, `run` and `run --force`, the scheduler's skip through the detector, the `local` stub, the `local:` error, the `doctor` row, `preferred` as `required`. LOCAL becomes unreachable; its code is still there. `dist_check.py` drops its LOCAL dry run and checks the stub from the installed wheel instead. The changelog entry, `docs/local-mode.md`'s legacy page, the README and the operator documents of A.3. | State files produced by `0.1.0a0`'s own code (its `local run` against a fake agent CLI), committed as produced: each LOCAL phase, a pending launch checkpoint, `DONE`, `BLOCKED`, a drifted contract. Every command of D4 exits as specified and leaves the legacy state directory byte-identical (a hash of the tree before and after). Without `--state-dir`, under the default `state_dir` and under a non-default one: a run only at the configured location and a run only at `<git dir>/autoforge/state` are each found and judged by their `mode`, and one at each is refused with both named and neither touched; with `--state-dir`, only that directory is read. The stub and the `local:` error touch no file. A scheduler pass with a legacy LOCAL state at `config.state_dir` admits as before. The fallback (D9): a LOCAL run that `0.1.0a0` started under a non-default contract (`local.validation_commands`, `local.max_fix_rounds`, `local.exclude`) and pin (`sandbox.mode: preferred`), with its configuration file kept beside it, is accepted by `0.1.0a0`'s `status` and `resume` given D9's arguments from its checkout, and refused, with the moved fields named, without `--config`; the new release refuses it as any legacy run. That half runs `0.1.0a0`'s own code from its tag, so it is opt-in like the live Pi test and recorded in S2's PR. |
| S3. Delete | Every DELETE row, with its tests: the LOCAL engine paths, transitions, prompts and result models, `run_contract.py`, the rest of `local_workspace.py`, the LOCAL launch classes, the workspace record variant and the `local` certificate kind, `doctor`'s LOCAL subset. The protocol bump of D8, announced on #23. D11's ADR notes. | Protocol-12 REMOTE files captured from `main` in every phase load and relabel with the LOCAL fields dropped, and so does a pre-LOCAL file without them; a protocol-12 REMOTE file with a non-empty LOCAL field or missing a field protocol 12 always wrote, and an N + 1 file with any LOCAL field or a `mode` other than `"REMOTE"`, are refused unchanged; `0.1.0a0` refuses an N + 1 file. Every per-protocol load test of `test_state.py` keeps passing with only its LOCAL rows dropped and its expected label moved to N + 1, the refusals included (`test_protocol_1_is_migrated_only_without_a_replan_in_flight`, `test_protocol_2_is_refused_in_the_merge_phases`, `test_protocol_3_is_refused_in_the_merge_phases`, `test_protocol_3_is_refused_with_a_replan_in_flight`, `test_protocol_4_is_refused_with_a_replan_past_the_write`, and the planted- and missing-field refusals of protocols 5 to 12), and each of those refusals is run again on its file with the twelve LOCAL fields present at their empty values, so that dropping them can never let a refused file through. The S2 tests still pass. A structural test fails on `LOCAL_LEGAL_EDGES`, a LOCAL launch class or an import of a deleted module, and on `WorkflowMode.LOCAL` outside the detector. |
| S4. Measure and close | The instruction maps (`AGENTS.md`, `src/autoforge/AGENTS.md`, `tests/AGENTS.md`, `docs/README.md`) drop LOCAL; Appendix B's commands run again, and `tests/parallel_bench.py --config serial --repeats 3` runs on `4b03151` and on the removal's last commit, back to back on one host. | The after-numbers are recorded beside Appendix B's, with the commands, interpreters, host and load. A difference is reported as measured, and called a speedup only when the repeated runs put it beyond their spread: Appendix B's two pytest runs differ by 36 s between interpreters alone. |

S1 comes first because a coverage loss is invisible in a diff of
deletions. S2 before S3 keeps what an existing run meets small and
reviewable on its own, with LOCAL's code still present to compare against;
S3 is then a deletion whose safety the S2 tests already pin.

## 3. Options considered

**For an existing LOCAL run.** (a) Refuse it with a read-only summary and
a fallback, D3 to D9 (chosen). (b) Migrate it into a REMOTE run: there is
no issue, PR or commit for it to bind to, and D6 forbids carrying its
review across. (c) Keep a minimal LOCAL engine that can finish runs
already started: it keeps the run contract, the walk and the LOCAL
transitions, which are most of what the removal deletes. (d) Move or
delete its state directory on upgrade: #384 forbids it, and it would
destroy the only record of work still in the operator's tree.

**For the protocol.** N + 1 (chosen); label 12 with the LOCAL fields kept
as empty columns; label 12 with the fields dropped. D8 gives the reasons.

**For `preferred`.** Keep it as `required` (chosen); refuse it. D7 gives
the reasons.

**For the release.** Remove in the next prerelease (chosen, D10), or ship
a deprecation warning first. The second costs a release whose only
content is the warning, for a population of one pre-Alpha upload's users.

## 4. Risks

- **An operator with a LOCAL run upgrades and cannot continue it** with
  the new release. That is the design (D4); the mitigations are the
  explicit refusal at every entry point, the fallback, and a state
  directory nothing touches.
- **The fallback rots.** An agent CLI that changes its flags breaks
  `0.1.0a0`, and no fix will follow. The fallback is a way to finish a
  run, not a supported LOCAL.
- **A shared mechanism loses coverage** because its only test was a LOCAL
  one. S1 exists for this; Appendix A.2.3 is its checklist.
- **The S1 split of `local_workspace.py` changes REMOTE.** It moves code
  every REMOTE run executes. It must be a move with the REMOTE tests
  unchanged, not a rewrite.
- **The protocol bump meets another persisted change.** The numbering rule
  already settles it: whichever merges second takes the next number.
- **The inventory is a snapshot of `4b03151`.** A LOCAL reference added
  after it is not in Appendix A. Each step re-greps before it merges, and
  S3's structural test catches what is left.

## 5. Open questions and proposed edits elsewhere

#384 asks for open #155 and the launch EPIC #255 to be reviewed and for
precise edits and dependency changes to be proposed on them. Neither
issue could be read, so that review is not done and the item stays open.
What follows is drawn from the repository alone: drafts to check against
each issue's current text, and the questions that check must answer,
before anything is proposed on either issue.

- **#255 (Public Alpha).** README's maturity section lists LOCAL as
  "Supported now" and says the Public Alpha claims "these capabilities at
  exactly these levels (#255, conditions C1–C7)". If the removal ships
  first, the Public Alpha never claims LOCAL, and its security audit,
  compatibility matrix and README cover REMOTE only; if #255's Go comes
  first, the Public Alpha ships LOCAL frozen (D2) and the removal follows.
  The recommendation is the first. Draft edit to #255: a Go/No-Go item
  "LOCAL removed (EPIC #383 S2 shipped), or shipped frozen and documented
  as such", and S1 as a dependency of any #255 evidence gathered through
  LOCAL. Pi's recorded live run, the acceptance evidence of #135, is a
  LOCAL REVIEW (`docs/development.md`, README). To check on #255: whether
  C1–C7 name LOCAL or README's "Supported now" tier; which of its open
  children gather evidence through LOCAL (the live Pi run among them);
  and whether it already orders itself against EPIC #383.
- **#155 (provider parity).** ADR 0006 D17.2 reuses #155's parity method,
  and `tests/test_sandbox_parity.py` has a LOCAL validation case. Draft
  edit to #155: its parity cases cover the REMOTE launch classes only, and
  no new case adds a LOCAL one (D2). To check on #155: whether it is still
  open; whether its open scope names a LOCAL case, a LOCAL launch class or
  `preferred`'s fallback; and whether it blocks or is blocked by anything
  S1 to S3 touch.

Two choices of this record's own are open to the sign-off:

- **The exit status of the legacy `status` (D4).** 2 makes a legacy run
  read as an error to a script; 0 would make it read as healthy. This
  record chooses 2.
- **The two-match refusal (D3)** keeps a flag's cost for operators with a
  stale LOCAL directory, in exchange for never picking a run for them.

## Appendix A. Reference inventory

Taken at `4b03151` by reading every match of `WorkflowMode.LOCAL`,
`is_local`, `LOCAL`, `local_*`, `features/`, the run contract, the
workspace walk and fingerprint, the `local:` block, the LOCAL launch
classes and the `local` certificate across `src/`, `tests/`, the prompts,
the configuration example, CI, the release artifacts and `docs/`, then
checking each claimed REMOTE use with a grep for its callers. "local" in
the sense of a local branch, a local checkout, local git configuration or
"on this host" is not counted. Line numbers are `4b03151`'s; each step
re-greps before it merges (§4).

### A.1 Source

The deletion estimates are reviewers' counts over method and branch
boundaries, not a diff: about 4,500 of the 74,840 lines under
`src/autoforge/`. The largest are `engine.py` (about 1,450 to 1,500 of
16,820), `local_workspace.py` (about 960 of 1,253), `run_contract.py`
(all 408), the four LOCAL prompt templates (417), `cli.py` and `state.py`
(about 220 and 210 net of the legacy code they gain) and `result_parser.py`
(about 205).

**Entry points, configuration, doctor**

| Item | Where | Class | Note |
|---|---|---|---|
| `local` subparser and dispatch, `_local_engine_for`, `cmd_local_init`, `cmd_local_run`, `cmd_local_doctor` | `cli.py` :326–387, 564–571, 623–631, 1604–1676 | LEGACY (stub) + DELETE | D5 |
| `_bind_existing_run`'s two-location probe | `cli.py` :634–667 | LEGACY | D3. Runs a `git rev-parse` on every REMOTE `status`, `step`, `resume`, `unblock` without `--state-dir` |
| LOCAL arms of `status`, terminal and evidence rendering; `_render_local_status` | `cli.py` :683–684, 1821, 2038–2062, 2084–2086, 2170–2186, 2229–2254, 2353, 2415, 2435–2437, 2508, 2603, 2717–2770 | PRESERVE SHARED (arms DELETE) | `_render_local_status` gives way to D4's summary |
| `local` in `TOP_LEVEL_KEYS`, `LOCAL_KEYS`, `LocalConfig`, its parser and defaults | `config.py` :52–56, 621–679, 764, 808, 1512–1551 | LEGACY (key refusal) + DELETE | D5; REMOTE reads it through `engine.workspace()` (C1) |
| `refuses_unenforced(local)` and `preferred`'s fallback | `config.py` :696–705, 743–748 | PRESERVE SHARED (simplified) | D7 |
| `doctor`'s LOCAL subset: `check_feature_spec`, `check_validation_commands`, `run_local`, `_local_agent_checks`, `_workspace`, `check_local_state_dir`, `run_local_doctor` | `doctor.py` :54–57, 116–118, 803–848, 856–876, 1281–1325, 1541–1551 | DELETE | `run_all` never calls them. `check_local_state_dir`'s probe can create an empty `<git dir>/autoforge/state` |
| The `local` parameter of the sandbox rows | `doctor_sandbox.py` :128–206, 432–480 | PRESERVE SHARED (arms DELETE) | |
| `LOCAL_BASE_PROFILES`, `REVIEW_PROFILE_THRESHOLDS`, `local_required_profiles` | `profiles.py` :45–84 | DELETE | REMOTE uses `REQUIRED_PROFILES` |

**Workflow and engine**

| Item | Where | Class | Note |
|---|---|---|---|
| `WorkflowMode.LOCAL` | `transitions.py` :84–90 | LEGACY | The detector and the scheduler's skip (D3, D4) |
| `LOCAL_LEGAL_EDGES`, `stop_phases_for`, the LOCAL block of `decide_next_phase`, the `mode` parameters | `transitions.py` :225–275, 326–336 | DELETE (functions PRESERVE) | |
| `LOCAL_PHASES`, `LOCAL_WRITE_PHASES` | `transitions.py` :115–120, 238–249 | DELETE | The legacy summary validates no LOCAL topology |
| `Phase.DONE` | `transitions.py` :102 | PRESERVE SHARED | `UPDATE_EPIC → DONE` |
| The LOCAL step: `_local_step_once` and the 16 methods after it, the launch checkpoint and three-launch bound, fingerprint binding, validation convergence, `local` certificate issuance (:5460–5470) | `engine.py` :4965–5784 | DELETE | Entry :4803; outside call `_charge_local_launch` :13163 |
| `new_local_run`, `_check_baseline_clean`, `bind_local_state_dir` | `engine.py` :3049–3059, 3127–3201 | DELETE | |
| The LOCAL dry-run plan; the LOCAL prompt variables | `engine.py` :4341–4466; 3608–3643, 3668–3693 | DELETE | |
| `_local_facts`, `_say_fallback`, `_require_local_review_evidence`, `_validation_failure`, `_reused_failure`, `_local_budget_block_reason`, `MAX_LOCAL_PHASE_ATTEMPTS`, `LOCAL_PHASE_TEMPLATE`, `_invocation_contract`, `local_contract` | `engine.py` :595–634, 1020–1030, 1473–1547, 1825–1844, 2680–2684, 2779–2814, 6445–6500, 9460–9466 | DELETE | |
| The contract gate in `load()`; the LOCAL arm of `_configured_validation` | `engine.py` :3216–3219, 6508–6512 | DELETE after S2 | Legacy states no longer reach `load()` (D3) |
| `unblock`'s LOCAL refusal | `engine.py` :7820–7824 | DELETE after S2 | The detector refuses first (D4) |
| `local_state_paths()`, function and method | `engine.py` :1213–1250, 3061–3063 | LEGACY | The default location of D3 |
| `workspace()` | `engine.py` :1806–1823 | REPLACE | C1 |
| `preferred`'s fallback: `_sandbox_fallback`, `_fallback_facts`, `_fallback_notice` | `engine.py` :2646–2678 | DELETE | D7; REMOTE never reaches it (:2651) |
| LOCAL arms in shared methods: sandbox routing and denied roots (:2574–3028), `_execution_cwd`, the run-configuration record and check, `validate_config`, `template_for`, `run`, `_step_once`, the pre-launch charge in `_invoke_phase` (:13158–13167), the transient relaunch (:13358), `_log_metadata`, `_record_invocation`, `_moved_checkout_recovery`, the evidence renderers (:919–995, 14237) | `engine.py` | PRESERVE SHARED (arms DELETE) | 15 `mode == REMOTE` guards become always true |
| `_checkout_anchor`, `_invoke_phase_anchored` | `engine.py` :2889–2970 | PRESERVE SHARED | Already REMOTE's own code |
| LOCAL result models, `LOCAL_FIX_RESOLUTIONS`, `_validate_local_phase`, the `mode` parameter of `parse_control_result` | `result_parser.py` :106, 117–120, 330–340, 1994–2201, 2208–2219 | DELETE | `_opt_str_list` (:2003) is shared: it moves |
| `local_common.md`, `local_analyze_execute.md`, `local_review.md`, `local_fix.md`; their `TEMPLATE_FILES` entries and `LOCAL_COMMON_TEMPLATE` | `prompts/` | DELETE | REMOTE templates do not change, so `__prompt_version__` need not |
| `validation_evidence.md`, `correction.md` | `prompts/` | PRESERVE SHARED | |

**State and protocol**

| Item | Where | Class | Note |
|---|---|---|---|
| `mode` and its parse | `state.py` :457–461, 735–764 | LEGACY | D8 keeps writing `"REMOTE"` |
| The twelve LOCAL fields | `state.py` :568–618 | DELETE with D8's drop step | C2 |
| `_validate_local_pending`, the LOCAL topology and required-field checks, the contract parse, `local_contract()` | `state.py` :190–263, 727–732, 1105–1167 | DELETE | |
| The pre-contract LOCAL refusal | `state.py` :1045–1058 | DELETE after S2 | The detector refuses every LOCAL file first |
| LOCAL arms of the effect, acceptance, operator and recorded-configuration validators | `state.py` :1266, 1310–1317, 1712–1768, 1905–1917 | PRESERVE SHARED (arms DELETE) | |
| `StatePaths.anchor`; `canonical_state_dir` | `state.py` :2059–2106; 2108–2110 | PRESERVE SHARED; DELETE | `anchor` serves the scheduler namespaces (`run_namespace.py` :281) |
| `run_contract.py` | whole file | DELETE | |
| `__protocol_version__` | `__init__.py` :4 | PRESERVE SHARED | Bumped by D8 |

**Workspace, filesystem, evidence, sandbox, scheduler**

| Item | Where | Class | Note |
|---|---|---|---|
| `LocalWorkspace`'s git plumbing: `root`, `git_dirs`, `head_sha`, `branch`, `status_entries`, `dirty_paths`, `StatusEntry`, `uncommitted_paths`, `GIT_TIMEOUT_SECONDS` | `local_workspace.py` :124, 140, 329–341, 388–466, 479–588, 1001–1023 | REPLACE | REMOTE: `engine.py` :2282, 2376, 2891, 3807–3812, 13580; moves to a mode-neutral module (C1) |
| The walk, snapshot and fingerprint | `local_workspace.py` :131–327, 619–976 | DELETE | |
| The feature specification and `init_feature_file`; `local_state_dir`, `check_state_dir_location`; `DEFAULT_MAX_*`, `normalize_exclude_pattern` | `local_workspace.py` :135–169, 468–477, 590–617, 978–1253 | DELETE | |
| `safefs.py` | whole file | PRESERVE SHARED | `readlink_at` (:490–492) has only the walk as caller: DELETE |
| `KIND_LOCAL`, `LocalCandidate`, `METHOD_IN_PLACE`, the LOCAL record and marker keys, `check_evidence(local=)` | `evidence.py` :112–122, 313–321, 406–431, 594–699, 781–995, 1085–1121, 1538–1548 | DELETE | |
| `local_certificate`, `_local_binding_problem`, the LOCAL arm of `certificate_problem` | `certificate.py` :271–304, 361–378, 407–441 | DELETE | `certificate_problem` stays |
| `LaunchClass.LOCAL_AGENT`, `LOCAL_VALIDATION`, `LOCAL_SANDBOX_KEY`, their policy and `template_facts` | `sandbox_policy.py` :18–23, 85–87, 112–113, 289–317, 719–835 | DELETE | `<git dir>/autoforge/sandbox/local` stays on disk |
| `mode_strength` and `direction(local=)` | `sandbox_pin.py` :98–123 | PRESERVE SHARED (simplified) | D7 |
| `standalone_hold`'s LOCAL skip | `ownership.py` :227 | LEGACY | D4 |
| The namespace refusal of a non-REMOTE state | `scheduler_state.py` :1460–1461 | PRESERVE SHARED | |
| `LocalBinding` | `interaction.py` :690–703 | DELETE | No production caller; ADR 0007's LOCAL identity |

No LOCAL code, only prose at most: `runlog.py`, `locking.py`, the
scheduler modules, `github.py`, `git_transport.py`, `materialize.py`,
`premerge.py`, `agent_worktree.py`, the executors, `sandbox.py`,
`sandbox_bwrap.py`, `claims.py`, the effect modules, the provider and
protocol modules, `loop_guard.py`, `replan.py`, `replan_txn.py`,
`read_context.py`, `redaction.py`, `roadmap.py`, `validation.py`.

**Couplings.** Where removing LOCAL would change REMOTE, or a shared
mechanism depends on LOCAL:

- **C1.** `engine.workspace()` builds REMOTE's git reader from
  `config.local.*`, and `config_file_placement_problem` builds one too
  (`engine.py` :1452). The `local:` block cannot go before the reader is
  split out (S1).
- **C2.** Every REMOTE state file carries the twelve LOCAL fields, and an
  unknown field is refused (`state.py` :1086–1100). D8.
- **C3.** `status --json` of a REMOTE run prints those fields
  (`cli.py` :2038–2041 promises an additive shape). D8, D10.
- **C4.** `_bind_existing_run` probes LOCAL's location on every REMOTE
  `status`, `step`, `resume` and `unblock` without `--state-dir`, whatever
  `state_dir` is configured to, and can refuse two runs. D3 keeps it as
  the legacy probe.
- **C5.** `preferred` has a fallback only in LOCAL. D7.
- **C6.** The scheduler's reverse guard skips a LOCAL state; if it failed
  to load instead, every pass would warn. D4.
- **C7.** A LOCAL `status` passes the contract gate today and fails when
  the configuration drifted. D3's detector never reaches it.
- **C8.** REMOTE relaunches a transient provider failure; LOCAL does not
  (`engine.py` :13358). Deleting the arm changes nothing for REMOTE.

### A.2 Tests

Counts are of test functions, not collected items (Appendix B has
those): definitions matching `^def test_`, `^    def test_` or
`^async def test_` in the 84 `tests/test_*.py` modules. There are 4,254;
387 are in scope.

| Class | Functions | What it means |
|---|---|---|
| DELETE | 275 | Tests of LOCAL-only behaviour |
| REPLACE | 46 | Shared coverage that today runs only through LOCAL (A.2.3) |
| LEGACY | 12 | Tests that now assert a LOCAL artifact loads or is left alone, to be rewritten as refusals or read-only display; the `local` stub and `local:` refusals have no test yet |
| PRESERVE SHARED, edited | 54 | 48 drop a LOCAL row or parameter, 6 move out of a LOCAL-only module |
| PRESERVE SHARED, untouched | 3,867 | |

#### A.2.1 By module

| Module | Functions | In scope | Class |
|---|---|---|---|
| `test_local.py` | 116 | 116 | DELETE 106; REPLACE 8 (A.2.3 A, B, H); move 1 (`test_saved_state_without_a_mode_field_loads_as_remote`); LEGACY 1 (`test_unknown_mode_fails_loudly`) |
| `test_local_recovery_matrix.py` | 24 | 24 | DELETE (journal cases covered in `test_runlog.py` and REMOTE `test_engine.py`) |
| `test_local_red_team.py` | 21 | 21 | DELETE (the artifact-name case is covered in `test_runlog.py`; the state-directory replacement partly in `test_state.py`) |
| `test_local_state_fuzz.py` | 6 | 6 | DELETE 4; REPLACE 2 (A.2.3 B) |
| `test_local_workspace_matrix.py` | 9 | 9 | DELETE |
| `test_durable_run_contract.py` | 43 | 43 | DELETE 30; REPLACE 6 (A.2.3 C, D); move 5 (two AST guards on state access, the `link(2)` publication test, the REMOTE worktree test, the REMOTE half of a correction-fence test); LEGACY 2 |
| `test_live_pi.py`, `test_live_pi_support.py` | 1, 28 | 1, 15 | REPLACE 15 (A.2.3 G); 13 checks of the support module are mode-free |
| `test_doctor_sandbox.py` | 18 | 14 | REPLACE 12 (A.2.3 E); DELETE 1; PRESERVE 1 |
| `test_sandbox_routing.py` | 21 | 8 | DELETE 6; REPLACE 2 (A.2.3 E, F) |
| `test_sandbox_policy.py` | 54 | 3 | DELETE 2; REPLACE 1 (A.2.3 F) |
| `test_state.py` | 160 | 12 | DELETE 7; LEGACY 2; PRESERVE 3 (drop the `local` id) |
| `test_config.py` | 142 | 14 | DELETE 6; PRESERVE 6; LEGACY 2 (the `local` row of the unknown-key test; the non-mapping `local:` block) |
| `test_certificate.py`, `test_validation_evidence.py`, `test_validation_gates.py`, `test_review_certificate.py`, `test_merge_certificate.py` | 40, 37, 44, 20, 29 | 9, 9, 10, 4, 2 | DELETE (each has REMOTE sections) |
| `test_evidence.py`, `test_result_parser.py`, `test_transitions.py` | 77, 134, 19 | 10, 15, 5 | DELETE 5, 4, 2; PRESERVE the rest (drop LOCAL parametrizations) |
| `test_sandbox_gate.py`, `test_sandbox_pin.py`, `test_config_fingerprint.py` | 12, 15, 35 | 5, 4, 2 | DELETE 5, 2, 2; PRESERVE 2 in `test_sandbox_pin.py` |
| `test_cli.py`, `test_ownership.py`, `test_epic_commands.py`, `test_run_namespace.py` | 119, 16, 32, 45 | 3, 1, 1, 1 | LEGACY 2, 1, 1, 1; PRESERVE 1 in `test_cli.py` |
| `test_doctor.py`, `test_prompts.py`, `test_routing.py`, `test_sandbox_parity.py` | 122, 50, 5, 1 | 4, 11, 1, 1 | DELETE 2 in `test_doctor.py`; PRESERVE the rest (drop the LOCAL row, template or profile) |
| `test_sdk_policy.py`, `test_interaction.py`, `test_unblock.py` | 46, 73, 39 | 1 each | DELETE |
| `test_safefs.py` | 49 | 0 | PRESERVE. Introduced with LOCAL, uses none of it |
| `test_engine.py`, `test_lock_domains.py` | 546, 39 | 0 | PRESERVE; they import `StatusEntry` and `GIT_TIMEOUT_SECONDS` from `local_workspace` (C1) |
| The other 46 modules | 1,967 | 0 | PRESERVE |

#### A.2.2 Fixtures and helpers

- `tests/conftest.py` :2280–2420 is LOCAL-only: `ExplodingGitHub`,
  `FEATURE_MD`, `sample_contract`, `write_feature` and
  `make_local_engine`, which 14 modules use.
- **`test_local.py` is a helper library.** Nine modules import its
  helpers at module level (`local_repo`, `impl_result`, `review_result`,
  `fix_result`, `finding`, `scripted`, `touch_impl`, `IMPL_FILE`) and three
  more inside functions. Deleting it first breaks their collection: S3
  deletes its importers' LOCAL tests in the same change, or S1 moves what
  survivors import.
- `tests/live_pi.py`'s `run_smoke` is LOCAL (`local doctor`, a `local:`
  configuration, `new_local_run`); its credential scan, tree, process and
  session helpers are not. `tests/pi_fake.py` is shared.
- `tests/dist_check.py` runs a LOCAL dry run (`local init`, `local run
  --dry-run --full-prompt`) beside its REMOTE one and renders the LOCAL
  templates. It gates every release (`pypi-publish.yml` runs it with
  `--release-tag`) and CI's `dist` job. The REMOTE dry run already exists,
  so this is PRESERVE with the LOCAL half dropped.

#### A.2.3 What a naive deletion would lose (the S1 checklist)

- **A. The hardened git reads REMOTE uses** (5, `test_local.py`): no
  optional index lock, no fsmonitor and no credential; `uncommitted_paths`
  as one hardened read; status records with renames and copies; the
  refusal of truncated output. REMOTE has only end-to-end tests of the
  rendered note (`test_engine.py`). They move with the git plumbing (C1).
- **B. The state loader** (3): the unknown-field refusal
  (`test_local.py`), the only fuzz test of the loader and the non-object
  JSON roots (`test_local_state_fuzz.py`). They become REMOTE state tests.
- **C. The state root under a running engine** (5,
  `test_durable_run_contract.py`): a state directory replaced mid-run, a
  `state.json` replaced by a symbolic link or hard-linked elsewhere, the
  documented undetectable in-place copy, a link at the state directory or
  its parent at bootstrap. `test_state.py` covers only rename-aside and a
  symbolic link at `state_root()`.
- **D. Findings fencing** (1): finding text cannot break out of the
  findings block, which `_format_findings` renders for REMOTE `FIX` and
  `REVIEW` too.
- **E. `doctor`'s sandbox rows** (13): every row (capability, providers'
  `--version` clip, declarations, linked configuration directories,
  shebangs through links, hard links, sandbox directories, operator paths
  over denied roots, outside a repository) is tested with `local=True`;
  REMOTE has one test that checks two row names.
- **F. Launch classes** (2): the declaration-writability test runs on
  LOCAL facts (a REMOTE helper exists in the same file); the real-backend
  demonstration that a provider can rewrite a `--config` file in its
  writable directory runs on `LOCAL_AGENT`.
- **G. The live Pi smoke test** (15): the only live model test runs one
  LOCAL REVIEW, and its 14 offline rehearsals follow the same flow. No
  live REMOTE flow exists; offline REMOTE Pi coverage does
  (`test_a_pi_phase_runs_end_to_end_over_rpc`,
  `test_an_all_pi_run_gets_the_same_isolation_in_every_child`). A REMOTE
  form needs a GitHub stand-in, the in-memory fake or a scratch
  repository, which is S1's design question.
- **H. The credential preflight of a validation command** (2,
  `test_local.py` :3850, :3873): a credential helper written into the
  repository's configuration, and a login-shell startup file written to
  export a credential, by the agent after its own launch was checked, stop
  the validation command before it runs and leave the phase unverified.
  REMOTE candidate validation runs the same preflight before each command
  (`engine.py` :6181–6190), where a refusal is re-checked once and becomes
  `ValidationInconclusiveError`: `BLOCKED`, no record, nothing pushed or
  published (:6349–6354). No test plants either file before it
  (`test_validation_evidence.py` reaches that error only through a
  validator that cannot be launched). S1 adds a
  REMOTE test for each, planting the file between a producer's launch and
  its candidate validation and asserting with a probe command that the
  command did not run. The REMOTE command runs in an export with no git
  dir, so the helper case may instead show a command that runs where no
  repository configuration reaches it; which of the two the code does was
  not checked here, and the test settles it and asserts it. The REMOTE
  pre-merge verification's startup-file test (`test_engine.py` :13627) is
  another launch and does not count.

#### A.2.4 Test contracts to rewrite

`tests/AGENTS.md` (the LOCAL fixtures, the `test_local*.py` and
`test_durable_run_contract.py` rows, "REMOTE and LOCAL" in the sandbox-pin,
evidence, gate and certificate rows, the live Pi row) and
`docs/agent-guides/testing.md` (34 lines: the state, CONTROL_RESULT,
execution, outer sandbox and launch routing, credential, parity, live Pi,
SDK, interaction and epic requirements) name LOCAL coverage as required.
S2 adds the legacy requirement (a `mode: "LOCAL"` state, LOCAL's default
location, a `local:` block, `autoforge local` argv); S3 removes the rest.

### A.3 Documents, configuration, CI and release artifacts

DELETE goes when the removal ships; REWRITE edits shared text to REMOTE
only; LEGACY is text an operator with a `0.1.0a0` LOCAL run needs; HISTORY
stays as written. `CLAUDE.md`, `SECURITY.md`, `pyproject.toml`,
`MANIFEST.in`, `pypi-publish.yml`, `docs/releasing.md` and
`docs/non-code-rules.md` mention no LOCAL mode.

| Document | DELETE | REWRITE | LEGACY / HISTORY |
|---|---|---|---|
| `README.md` | "Local mode (no GitHub)" (:359–415) and the GitHub-free sentences (:61–64, 177–184); the "work without GitHub" row (:426) | state location (:47–49); key properties (:100–101, 123–124, 148–156); prerequisites (:200–224); installation (:285–287); maturity: LOCAL under "Supported now" (:442–451), LOCAL validation and `DONE` (:468–489), Pi's evidence (:524–526) | LEGACY: the section heading kept with a pointer (D10); a note that `0.1.0a0` ships LOCAL (:528–531) |
| `docs/local-mode.md` | the guide | | LEGACY: replaced by the legacy page (D10), from its "Resume and recovery" and "The run contract" facts |
| `docs/usage.md` | "LOCAL runs show the same block" (:1463–1464) | intro; `BLOCKED`, acceptance, standalone, watching and namespace passages; the outer sandbox (`preferred`, `local doctor`, :944–992); troubleshooting; recorded configuration and evidence | LEGACY: a troubleshooting entry for the legacy refusal; the leftover `.git/autoforge/sandbox/local` cache (:881) |
| `docs/configuration.md` | "Local mode configuration" (:1215–1235); the `local` row (:207) | `prompt_version` and the contract (:160–188); `local doctor`; the pin, validation commands, `preferred`, launch classes, LOCAL workspaces and caches (about fifteen passages) | |
| `docs/security.md`, `docs/compatibility.md`, `docs/development.md` | | LOCAL validation commands, the LOCAL root and pin order; "LOCAL needs no `gh`", `preferred` in LOCAL, the LOCAL state directory; `dist_check.py`'s step 6 and the live Pi test | |
| `docs/pi.md`, `docs/pi-policy.md`, `docs/sdk-policy.md` | "LOCAL mode" in `pi.md` (:312–325) | the live smoke test (`pi.md` :407–445); the tree-drift check and LOCAL REVIEW test (`pi-policy.md`); the LOCAL sandbox root and the fingerprint argument (`sdk-policy.md` :236–243) | |
| `docs/README.md` | the Local mode row (:16) | the Pi and configuration rows; the ADR 0001 and 0006 rows (:57, 62) | |
| `docs/agent-guides/architecture.md` | LOCAL templates (:972–974) | the resolver, launch routing, denied roots, refusal paths | |
| `docs/agent-guides/control-result-protocol.md` | the LOCAL REVIEW and FIX protocol (:104–105, 233–234, 360–387) | "REMOTE and LOCAL alike" (:136, 144, 225) | |
| `docs/agent-guides/workflow.md` | the LOCAL gate bullet and the three-launch bound (:276–285, 680–686) | the LOCAL topology, `REVIEW → DONE`, the unblock edge, certificate, pin, relaunch | |
| `docs/agent-guides/state-and-recovery.md` | | persistent state, validation evidence, locking, runtime artifacts, namespaces, error handling | LEGACY: how a LOCAL state is detected and refused (D3, D4), its directory left in place; HISTORY: the protocol history (:482) |
| `docs/agent-guides/github-safety.md`, `testing.md`, `replan-transaction.md`, `secrets-and-logging.md` | "LOCAL prompts" (`github-safety.md` :1265–1266); "LOCAL on Pi" parity (`testing.md` :1483–1486) | the rest (A.2.4 for `testing.md`) | |
| `AGENTS.md`, `src/autoforge/AGENTS.md`, `tests/AGENTS.md` | the `local-mode.md` map line; `run_contract.py`, `local_workspace.py`; the LOCAL fixtures and test rows | `preferred` in the isolation terms (:56–66); the ADR map line; the module map; the LOCAL production rule (:184–189), keeping its `safefs.py` pointer to ADR 0001 §5.4 | |
| `autoforge.example.yaml` | the `local:` block (:506–598) | `execution`, `scheduler` and `sandbox` comments | |
| `CHANGELOG.md` | | | HISTORY: the LOCAL lines of #395, #302, #280, #256 and the Pi entry; LEGACY: the removal entry (D10) |
| `.github/workflows/ci.yml` | | the `dist` job's comment (:109–121), which already omits the REMOTE dry run | |

### A.4 ADR decisions that are LOCAL's

| Record | LOCAL decisions | At removal (D11) |
|---|---|---|
| ADR 0001 | the whole record | superseded, except §5.4, the run-log and state-file bounds of §5.5, and §8.8 and §8.10, which describe `safefs.py` for REMOTE |
| ADR 0002 | none normative (§3, §4, §4c name LOCAL as an example) | no note |
| ADR 0003 | §2.4 (Pi corrections under the LOCAL launch charge), §3 | note |
| ADR 0004 | §2.1 (LOCAL prompts out of scope), K10's LOCAL refusal, §2.13 (a LOCAL state carrying effect state is corrupt) | note; §2.13's refusal is replaced by D3's |
| ADR 0005 | §1.3 and §2.3 to §2.6 (the LOCAL launch classes, the resource table's LOCAL column, the shared cache), §2.8, §2.9, D12.2 (`preferred`'s fallback), D12.3, D12.4, §2.13 to §2.15 | superseded in part; D12.2 by D7 |
| ADR 0006 | D1.2, D2.6 (`LocalCandidate`), D3.2, D3.5, D5.6, D5.7, D7.1 to D7.3 (the in-place convergence rule), the LOCAL record fields, D12.1, D12.5, D12.6, D14.1, D14.2 | superseded in part; D2.7's kind check stays |
| ADR 0007 | §1.1, §2.5 (the LOCAL interaction identity), §2.7, D8.2, D8.5 | unimplemented: dropped by the child that implements it |
| ADR 0008 | D17.5 (LOCAL excluded from scheduler runs by L0) and mentions in §1.1, §2.5, §2.12, §3, §4 | note; D4 keeps D17.5's effect |

### A.5 Found on the way, not decided here

- `safefs.py` is described as the boundary for "every controller write"
  (ADR 0001 §5.4, `docs/local-mode.md`), but controller writes in
  `git_transport.py`, `child_env.py`, `github.py`, `providers.py`,
  `materialize.py` and `locking.py` do not go through it. Whether the
  wording or the code should change is a separate question.
- REMOTE's sandbox `_denied_roots` (`engine.py` :2822–2836) may not deny a
  legacy LOCAL state under a linked worktree's own git dir. Not verified;
  S2 checks what an enforced REMOTE launch can see of a legacy state
  directory.

## Appendix B. Baseline before the removal

Measured on 2026-10-30 between 09:14 and 09:35 UTC at `4b03151`, a clean
worktree, before any of the removal. One run per interpreter, so these are
single observations with no spread: S4 measures again as D12 says.

### B.1 Host and conditions

Host B of `docs/development.md` ("The suite on several workers"): a Proxmox
container allowed 8 of the 12 hardware threads of an Intel i7-8700K, 15 GiB
of memory, Debian 13, Linux 7.0.14, Git 2.47.3, uv 0.13.6, pytest 9.1.1,
`TMPDIR` on a tmpfs `/tmp`. Python 3.14.10 is the interpreter
`.python-version` gives `make check`; 3.12.15 is one of CI's. The host was
shared: another AutoForge run and other agent sessions were active, and the
one-minute load average went from 1.8 before the first run to 8.2 at its
busiest and 2.1 after the last.

### B.2 Commands

```bash
uv run pytest --collect-only -q                     # items per module
uv run pytest --collect-only -q -m live
make lock-check; make lint; make fmt-check; make typecheck   # each twice: cold, then warm
PYTEST_ADDOPTS="--junitxml=junit-local.xml --durations=0 --durations-min=0.5" make check
uv run --isolated --locked --python 3.12 pytest --junitxml=junit-312.xml --durations=0 --durations-min=0.5
```

Each was timed with bash's `time` (wall seconds). Per-module times are sums
of each JUnit test case's `time`, which includes its setup and teardown.

### B.3 Results

| Measure | Python 3.14.10 (`make check`) | Python 3.12.15 (`pytest`) |
|---|---|---|
| Collected (default selection) | 11,723 items in 83 modules | same |
| Outcome | 11,642 passed, 81 skipped, 1 deselected | same |
| Wall time | `make check` 602.0 s, of which pytest 600.3 s | pytest 563.9 s (561.9 s reported by pytest) |
| Sum of per-test times | 589.1 s | 541.4 s |
| The six LOCAL-only modules | 568 items, 38.0 s (6.4%) | 568 items, 29.8 s (5.5%) |
| Items of 0.5 s or more | 253 items, 321.7 s | 210 items, 269.6 s |
| Items under 0.1 s | 10,520 items, 89.5 s | 10,536 items, 85.3 s |

- **`make check` is pytest.** Its other steps took, cold then warm:
  `lock-check` 0.009 s and 0.009 s, `lint` 0.075 s and 0.018 s,
  `fmt-check` 0.099 s and 0.018 s, `typecheck` 4.96 s and 0.16 s.
- **`-m live` selects one test**, `tests/test_live_pi.py`'s LOCAL REVIEW.
- **The 81 skips** are this host's: 60 real-backend sandbox tests (no
  Landlock ABI 6 on its kernel), 16 that need zsh, 5 opt-in probes. They are
  the 81 #340 recorded here.
- **Where the time goes.** `test_engine.py` (901 items) is 90 to 93 s, 15
  to 17% of the summed time; then `test_providers.py` (45 to 46 s), the two
  executor modules (32 to 36 s each), `test_epic_recovery.py` (27 to 32 s
  for 12 items), `test_replan.py` (26 s). `test_local.py` is next, 19 to
  26 s for 135 items; the other five LOCAL-only modules add 11 to 12 s
  together, of which `test_durable_run_contract.py` is 4.9 s.
- **The slow tail is test bodies, not fixtures.** Under `--durations-min=0.5`
  no setup or teardown phase appears in either run; the time is in calls,
  most of them waiting on a child process, a deadline or a grace period.
  The slowest items: `test_executor.py::test_bounded_buffer_memory_is_the_limit_plus_a_constant`
  (4.5 s on 3.14, 8.0 s on 3.12), then about 5.6 s each for
  `test_local.py::test_failing_validation_command_names_what_it_left_behind`,
  two `test_engine.py` verification-leftover tests and
  `test_pi_rpc.py::test_provider_a_pipe_holding_child_is_killed_and_reported`.
  That `test_local.py` test is the only LOCAL one among the 25 slowest.
- **Against #340.** #340 measured this host serially on 3.12 at `0255ca2`
  with a median of 509 s over three runs. This run's 563.9 s is one run, at
  a later commit with more tests, under more load; the two say nothing
  about a trend.

### B.4 Fixtures, subprocesses and files

A third run on 3.14.10, right after the other two, loaded the pytest plugin
below. It records each test's setup, call and teardown time, counts the
processes it starts through `subprocess.Popen` by executable name, and
counts the entries left in its `tmp_path` when its teardown begins. The
plugin's own cost makes this run no timing, yet it finished in 547.9 s
(pytest's 546.4 s), 54 s faster than the uninstrumented 3.14 run. That
much of B.3's wall time is the shared host's load (and perhaps the first
run's cold caches), not the suite.

```bash
PYTHONPATH=<plugin dir> uv run pytest -p af384_instrument
```

- **Fixtures are not where the time is.** Setup totals 17.8 s and teardown
  4.0 s of 537 s; the largest setup share is `test_cli.py`'s 3.1 s.
- **`git` is.** The suite starts 68,711 processes through `Popen`, 66,898
  of them `git` (then `python` 637, `bash` 295, `sh` 285, the fake `pi`
  264). `test_engine.py` starts 21,213 `git` processes, `test_replan.py`
  9,029, `test_merge_certificate.py` 4,956, `test_local.py` 3,921 and
  `test_cli.py` 3,632. The six LOCAL-only modules start 7,310 (10.9%).
  How much wall time that costs was not measured.
- **Files.** The 5,827 tests that take a `tmp_path` leave 231,591 entries in
  it: `test_engine.py` 71,898, `test_replan.py` 23,889, `test_local.py`
  8,828, the six LOCAL-only modules 19,263 (8.3%). One test,
  `test_runlog.py::test_opening_the_logger_never_lists_sibling_runs`,
  leaves 4,011 on purpose.
- **Not counted:** processes started other than through
  `subprocess.Popen`, files written outside `tmp_path`, and files a test
  created and removed before its teardown.

<details>
<summary>The plugin, <code>af384_instrument.py</code></summary>

```python
import collections, json, os, subprocess
import pytest

OUT = os.environ.get("AF384_INSTRUMENT_OUT", "instrument.json")
_current = [None]
_spawns = collections.defaultdict(collections.Counter)
_phases = collections.defaultdict(dict)
_entries = {}
_orig_init = subprocess.Popen.__init__

def _exe(args):
    try:
        first = args if isinstance(args, (str, bytes, os.PathLike)) else args[0]
        return os.path.basename(os.fsdecode(first)).split()[0] or "?"
    except Exception:
        return "?"

def _init(self, args, *a, **kw):
    if _current[0] is not None:
        _spawns[_current[0]][_exe(args)] += 1
    return _orig_init(self, args, *a, **kw)

subprocess.Popen.__init__ = _init

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    yield
    _current[0] = None

@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    tmp = item.funcargs.get("tmp_path") if hasattr(item, "funcargs") else None
    if tmp is not None:
        _entries[item.nodeid] = sum(
            len(d) + len(f) for _, d, f in os.walk(tmp, onerror=lambda e: None))
    yield

def pytest_runtest_logreport(report):
    _phases[report.nodeid][report.when] = report.duration

def pytest_sessionfinish(session, exitstatus):
    with open(OUT, "w") as fh:
        json.dump({"phases": _phases, "spawns": {k: dict(v) for k, v in _spawns.items()},
                   "tmp_entries": _entries, "exitstatus": int(exitstatus)}, fh)
```

</details>

### B.5 What the baseline does not say

It does not say how much faster the suite will be without LOCAL. The
LOCAL-only modules' 5.5 to 6.4% of test time, 10.9% of `git` processes and
8.3% of `tmp_path` entries bound what deleting them removes, but S1 adds
REMOTE tests in their place and S2 adds legacy ones, and B.4 shows the
host's load alone moving a run by 54 s. Whatever S4 measures is reported
against these numbers as measured, with its own spread.
