# ADR 0009. Removing LOCAL mode: no compatibility, strict refusal, the state protocol, sequencing and the baseline

- **Status:** policy accepted, implemented by #386 (S1a), #385 (S1b),
  #387 (S2) and #388 (S3, state protocol 13), and measured and audited by
  #389 (S4, Appendix C).
  - *Decision:* LOCAL mode is removed completely, as a breaking change with
    no backward compatibility. The maintainer approved this on 2026-10-30
    in the
    [maintainer decision on #384](https://github.com/iceboundrock/AutoForge/issues/384#issuecomment-6529791101).
    That comment first approved the compatibility design of this record's
    earlier revisions (`c648f07` to `7812757`: a legacy LOCAL run
    recognised and shown read-only, an `autoforge local` stub, `0.1.0a0` as
    a documented fallback). It was edited at 18:34 UTC the same day to
    supersede that approval, and the earlier text survives only in the
    comment's edit history. §1.4 lists what this revision drops. Whether
    it states the decision faithfully is for PR #406's review.
  - *This record's own, reviewed with it:* D1's classes, D2's freeze, D11,
    D12's steps and exit tests, and the proposals of §5.
  - *Not decided here:* the state protocol's number (#23, when #388's PR
    is ready to merge, D8) and the order of EPIC #383 against the Public
    Alpha's release candidate (§5.2). The maintainer decided that order on
    #255 on 2026-10-30, after §5.2 was written: option 1, the removal
    before the release candidate, with C7 reading "LOCAL mode: Removed.
    Not advertised or supported in Public Alpha." (§5.2's closing note,
    C.9).
  - *Implementation:* S1a (#386), S1b (#385), S2 (#387) and S3 (#388). S4
    (#389) recorded the measurement, the regression, the installed
    package's checks, the attestation of A.2.4 and the documentation audit
    in Appendix C (D12, §5.3).
  - *Release:* nothing is authorized. D10 says what the release that
    carries the removal must say; it is not a Go/No-Go, a tag or a
    publication.
  - The removal is complete on `main` with S3: ADR 0001 is marked
    superseded, and the LOCAL decisions of ADR 0003 to 0008 carry their
    notes (D11).
- **Decides:** #384 (RET-A1), for EPIC #383.
- **Planned against:** `main` `02ce244` (2026-10-30). Every line number in
  this record is at that commit. The inventory was taken at `4b03151`;
  #405, the one commit between the two, changed `engine.py`, eight prompt
  templates (`local_common.md` and `local_analyze_execute.md` among them),
  five test modules and six documents. Every line reference into them was
  moved to `02ce244` and checked against the symbol it names, and the
  counts were taken again. Appendix B's baseline stays at `4b03151`.
- **Amends:** ADR 0001, superseded except the parts REMOTE still relies
  on, and the LOCAL decisions of ADR 0003 to 0008 that Appendix A.4 lists,
  superseded in part. Both took effect when the removal was complete on
  `main` (#388, D11).
- **Related:** ADR 0001 (LOCAL mode), ADR 0005 (the LOCAL launch classes
  and `preferred`), ADR 0006 (LOCAL candidate identity and the `local`
  certificate), ADR 0008 (the scheduler and standalone state); #23 (state
  protocol numbering); #135 (the live Pi smoke test); #155 (SDK provider
  parity); #241 (the REMOTE validator's credential tests); #255 (the
  Public Alpha); #295 (the `0.1.0a0` registration prerelease).
- **Inputs:** the repository at `02ce244` and at `4b03151`; the bodies and
  comments of #383 to #389, #155, #255, #255's children #241, #259, #260
  and #262, and #23's numbering decision, read on 2026-10-30, with the edit
  history of the decision on #384; and the native sub-issue and dependency
  links of #383 to #389, #155 and #255, read through GitHub's REST API the
  same day, before and after the links of §5.3 were created.

## 1. Context

### 1.1 What removing LOCAL means

EPIC #383 removes LOCAL mode: `autoforge local` and the workflow behind it,
which drives agents over a `features/<slug>.md` specification and binds
reviews to a fingerprint of the working tree instead of a pull request. It
does not remove local *execution*. REMOTE's worktrees, git transport,
sandbox and process containment, validation, evidence, durable state,
recovery and EPIC scheduler all run on the operator's machine, and they
stay. Why LOCAL goes is the maintainer's decision, recorded on #384 and
#383. This record decides how: what is deleted and what stays, what an
existing LOCAL artifact meets afterwards, what the state protocol does, how
the removal is released, in which order the work lands, and what the suite
costs before any of it, so that the removal's effect is measured rather
than claimed.

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

The earlier revision estimated about 4,500 deleted lines under
`src/autoforge/`, net of the legacy code it added; this revision adds none,
so the deletion is somewhat larger. Neither figure is a diff (Appendix
A.1). 389 of the suite's 4,274 test functions touch LOCAL, and 46 of those
carry coverage REMOTE needs (Appendix A.2). At `4b03151` the six modules
that exist only for LOCAL were 568 of the 11,723 collected items and 6.4%
of the measured test time (Appendix B).

### 1.2 What is in the field

- `autoforge-cli` `0.1.0a0` was published to PyPI on 2026-10-28 (tag
  `v0.1.0a0`, commit `6e9495a`) and ships LOCAL mode. It is a pre-Alpha
  registration prerelease, not the Public Alpha (`CHANGELOG.md`,
  `docs/releasing.md`).
- It writes state protocol 12, as `main` does at `02ce244`.
- How many LOCAL runs exist is unknown. AutoForge has no telemetry.
- `SECURITY.md` supports only the latest commit of `main`. After the
  removal, a LOCAL run is unsupported as well: no release that contains the
  removal continues, shows, migrates or converts it (D3).

### 1.3 Constraints

The maintainer's decision on #384 sets these:

- **No LOCAL backward compatibility.** No legacy recognition or read-only
  display, no `autoforge local` stub, no LOCAL state-directory discovery,
  no fallback or recovery commands, no tests against historical LOCAL run
  contracts, no preserved LOCAL schema, and no legacy operator guide.
- **Generic fail-closed safety stays.**
  - Nothing deletes, rewrites, quarantines or converts state or
    working-tree content because an operator upgraded.
  - A `mode: "LOCAL"` state fails strict generic loading and is never read
    as REMOTE.
  - `run --force` never turns a foreign state into a REMOTE run or into
    trusted evidence.
  - No LOCAL evidence satisfies a REMOTE gate.
- **The CLI and the `local:` schema go entirely.** An unknown `local:`
  fails ordinary strict validation.
- **Shared REMOTE infrastructure stays.**
- **The sandbox loses its LOCAL branches.** `preferred` may stay as an
  alias for `required`, as REMOTE compatibility.
- **The protocol takes N + 1 under #23's rules.** Every historical REMOTE
  refusal stays, and the only migration is one that drops empty LOCAL
  fields from otherwise valid REMOTE files.
- **The release.** The removal ships in the next `0.1.0aN` with a
  changelog entry and no deprecation release. LOCAL leaves the feature
  matrix, the documents and the packaged resources. Historical ADRs stay,
  and no fallback release is promised.
- **Coverage.** Shared security regressions are preserved before LOCAL
  tests are deleted, the credential-helper and login-shell preflight
  tests included.
- **Related issues.** #155 and #255 are reconciled without reducing REMOTE
  coverage or any Public Alpha gate.

From the repository contract: a state file's protocol label, never its
shape, decides whether it loads, and a persisted change takes the next
number, announced on #23 (`state-and-recovery.md`, "Numbering a new
protocol"); configuration keys are validated strictly; REMOTE behaviour,
its verification and every safety gate stay as they are.

### 1.4 What this revision drops

The earlier revisions designed a compatibility layer. None of it is built:

| Earlier proposal | Now |
|---|---|
| A legacy LOCAL run recognised by its `mode`, by a detector run before the loader (earlier D3) | No detector. A `mode: "LOCAL"` file fails the ordinary load (D4) |
| A read-only `status` and `status --json` summary of a LOCAL run (earlier D4) | `status` fails as on any state that does not load |
| Refusal error types of their own for legacy runs (earlier D4) | The loader's existing `StateError` |
| The `autoforge local` stub, naming the fallback (earlier D5) | Deleted. argparse rejects `local` |
| The probe of LOCAL's default state directory on every command without `--state-dir`, with its two-run refusal (earlier D3) | Deleted, with no replacement (D3) |
| `autoforge-cli==0.1.0a0` as the documented, unsupported fallback: commands, a promise not to yank it, a source build (earlier D9) | Nothing documented or promised (D9) |
| A `local:` error telling the operator to keep the original file for the fallback (earlier D5) | The ordinary unknown-key error (D5) |
| Fixtures produced by the `0.1.0a0` release, a non-default run contract, and an opt-in test running `0.1.0a0`'s code (earlier S2, S3) | None. S2's refusal tests use small hand-built files |
| `docs/local-mode.md` kept as a legacy page, and the README's `#local-mode-no-github` anchor kept (earlier D10) | Both deleted (D5) |
| The LEGACY READ/REFUSE class: code that survives, reduced, to handle LOCAL artifacts (earlier D1) | Gone. GENERIC STRICT REFUSAL marks where a deletion leaves an existing refusal behind, and adds no code (D1) |
| The scheduler's LOCAL skip kept through the detector, and a `doctor` row warning about LOCAL's location (earlier D4) | Deleted. The generic rules for a state that does not load apply (D4) |

These carry over:

- the inventory and the REPLACE checklist, the credential-preflight cases
  included (Appendix A.2.3 H);
- the evidence boundary (D6) and `preferred` (D7);
- the protocol rule's order, minus the detector (D8);
- the freeze (D2), ADR status (D11) and the baseline (Appendix B).

The six findings of PR #406's first review were made against the
compatibility design. PR #406's description gives each one's disposition.

## 2. Decisions

### D1. What goes and what stays

**Delete what only LOCAL reaches; keep every shared mechanism, minus its
LOCAL branch; replace first what REMOTE needs from LOCAL.** Appendix A
classifies each reference with one of four labels:

- **DELETE:** LOCAL-only; nothing REMOTE or the scheduler uses depends on
  it.
- **PRESERVE SHARED:** REMOTE or the scheduler uses it. It stays; its LOCAL
  branch goes.
- **REPLACE:** a shared purpose that today is implemented or tested only
  through LOCAL. The replacement lands first (D12, S1a), or REMOTE breaks
  or loses coverage.
- **GENERIC STRICT REFUSAL:** a REMOTE validation path that already exists,
  and that refuses a LOCAL artifact once the LOCAL branch beside it is
  deleted. These paths are argparse's subcommand choices, the
  configuration's unknown-key check, the state loader's mode, protocol and
  field checks, and the evidence parsers' kind checks. The label adds no
  code and keeps nothing of LOCAL. It marks where a deletion leaves a
  refusal behind, so that a test shows the refusal happens and writes
  nothing (D4, D12).

Three findings of the inventory shape the work more than the rest:

- **REMOTE reads the working tree through LOCAL's reader.**
  `ControllerEngine.workspace()` (`engine.py` :1868–1885) builds a
  `LocalWorkspace` from `config.local.*` for every run. REMOTE uses its git
  plumbing (`root`, `git_dirs`, `head_sha`, `branch`, the status entries,
  `uncommitted_paths`) for:
  - the agent worktree root and worktree creation;
  - sandbox facts;
  - the candidate read and the checkout anchor;
  - the uncommitted-work note.

  That plumbing moves to a mode-neutral module that reads no `local:` key
  before `local_workspace.py` and the `local:` key can go. The walk, the
  fingerprint and the feature specification go.
- **`safefs.py` stays.** It is the write boundary of REMOTE's state, run
  logs and read context, as ADR 0001 §7 ("REMOTE mode") recorded when LOCAL
  introduced it, and the scheduler's namespaces and bookkeeping open their
  directories through it.
- **Every REMOTE state file carries LOCAL's twelve fields**, and the
  loader refuses a field it does not know (`state.py` :1086–1100). Deleting
  the fields without a drop step would make every existing REMOTE state
  unloadable. D8 is that step.

### D2. LOCAL is frozen until the removal ships

From this record's merge until the removal ships, LOCAL gets no new feature
and no new configuration key. A LOCAL defect is fixed only when it is a
security or data-loss defect, or when the fix is in shared code anyway. A
shared change need not extend LOCAL: it may refuse a LOCAL run explicitly,
naming this record, instead of implementing LOCAL's half.

Without the freeze, every shared change until the removal pays LOCAL's cost
twice: once to build the LOCAL half and once to delete it. Its cost is that
a LOCAL operator on `main` may meet a new refusal before the removal, in a
mode the removal ends anyway.

### D3. An existing LOCAL run is unsupported, and nothing looks for it

**No release that contains the removal continues, shows, migrates or
converts a LOCAL run.** An operator with LOCAL artifacts meets only paths
every other input already goes through:

| What the operator has | What happens after the removal | Mechanism |
|---|---|---|
| `autoforge local …` | `invalid choice: 'local'`, exit 2. No configuration read, no lock taken, no file touched | argparse's subcommand choices |
| A configuration file with a top-level `local:` block | Every command that loads it refuses, `doctor` and the `epic` commands included: `unknown key(s) under 'the top level': local`, exit 2 | the strict key check (`config.py` :1180) |
| A LOCAL state at LOCAL's default location, `<git dir>/autoforge/state` | Not read. Without `--state-dir` a command reads `config.state_dir` only and reports that there is no run there (`no state file at …`) | the two-location probe is deleted (`cli.py` :634–667) |
| A LOCAL state in a directory a command does read: `--state-dir`, or a `state_dir` the LOCAL run also used | `status`, `status --json`, `step`, `resume` and `unblock` refuse with `state file has unknown mode 'LOCAL'`, exit 2, and write nothing | the loader's mode check (D4) |
| The same, under `run` | Refused as a state that cannot be read, exit 2. With `--force`, it is moved aside as `state.json.corrupt-<timestamp>` and a fresh REMOTE run starts (D4) | `_existing_run_guard` (`cli.py` :1485–1551) |
| The same at `config.state_dir`, during an `epic` pass | Holds no issue, and is reported as a problem | `ownership.standalone_hold` (`ownership.py` :196–231) |
| LOCAL run logs, `features/*.md`, `<git dir>/autoforge/sandbox/local`, uncommitted work in the working tree | Never read, moved or deleted | none |

**The probe goes, and nothing replaces it.** Today a command without
`--state-dir` also looks at `<git dir>/autoforge/state`, because a LOCAL
run kept its state there (`_bind_existing_run`). Once LOCAL is gone, that
location holds no run any release can act on. Keeping a probe only to say
so is the LOCAL state-directory discovery the decision excludes. The cost:
an operator who upgrades and types `autoforge status` hears that there is
no run at `.autoforge/state.json`, not that a LOCAL run sits elsewhere. The
changelog says where LOCAL runs lived (D10). Deleting the probe also drops
a `git rev-parse` from every REMOTE `status`, `step`, `resume` and
`unblock` without `--state-dir` (Appendix A.1, C4).

The one LOCAL state a command still reads is one that shares a directory
with REMOTE commands. D4 makes that a refusal that writes nothing.

### D4. Strict refusal through the existing state paths

**The loader accepts `mode` absent or `"REMOTE"`, and refuses every other
value, `"LOCAL"` included, with today's error,
`state file has unknown mode …`.** The mode is read before the protocol
label (`state.py` :757–764), so the refusal holds under every label, an
unknown one included. An absent `mode` keeps meaning a REMOTE file from
before LOCAL existed, as today. No error type, message, exit code or
command branch is added for LOCAL: the loader's `StateError` exits 2
(`cli.py` :575–577) like every load refusal. Between S2 and S3 the
`WorkflowMode.LOCAL` member still exists for code nothing reaches, so S2
refuses it explicitly with that same message. S3 deletes the member, and
the refusal becomes the enum lookup that refuses any other unknown mode.

- **A failed load writes nothing.** `load_state` only reads, through the
  state root's capability. `status`, `step`, `resume` and `unblock` load
  the state before they save, pin, record a configuration or open a run
  log, so a refusal leaves the state directory and its logs byte for byte
  as they were. S2 tests this with a hash of the tree, taken twice in a
  row.
- **`run --force`, audited.** `_existing_run_guard` treats every
  `StateError` from `existing_run()` as a state that cannot be read.
  Without `--force` it refuses, exit 2, before anything is written. With
  `--force`:
  - `quarantine_state_file` (`state.py` :2269) reserves
    `state.json.corrupt-<UTC timestamp>` with `link(2)`, or with an
    exclusive copy where the filesystem makes no hard links, so it never
    replaces an existing file. Only then does it unlink `state.json`. The
    bytes survive under the new name;
  - `new_run` builds the new run from the command's own `--epic` and
    `--issue`. Nothing of the moved file is read into it: no phase,
    counter, evidence, pin or identity;
  - the run logs and every other file in the directory stay;
  - the move is printed (`autoforge: moved unreadable state file aside: …`).

  So `--force` neither converts nor overwrites a foreign state, and it
  promotes nothing from it into REMOTE evidence. It does move the file.
  Moving it is what the operator asked for, and the flag's documented
  meaning (`cli.py` :199–205). An upgrade alone never does it. The removal
  changes none of this: a LOCAL file now takes the path every other file
  that does not load takes. Refusing `--force` over a LOCAL file in
  particular was rejected. Telling a LOCAL file apart from other content
  that does not load is the detector the decision excludes, and the
  generic path already keeps the bytes. The `.corrupt-` suffix is generic,
  not a claim that the file is damaged; the changelog says so (D10).
- **The scheduler.** A standalone state at `config.state_dir` that does not
  load holds no issue and is reported (ADR 0008 D17.2; `ownership.py`
  :196–231). Today a LOCAL state there holds nothing too, through an
  explicit skip (:227) that goes. Admission does not change, because a
  LOCAL run never held an issue; the difference is a reported problem on
  each pass. The scheduler only ever creates REMOTE namespace states. A
  namespace state with any other mode is classified as one that cannot be
  read (`run_namespace.py` :632–644), not as a mismatch at the binding
  check (`scheduler_state.py` :1460). Both are uncertain classifications
  that hold the issue for the operator (`scheduler_state.py` :1527–1551).
  Run selection, claims and ownership do not change.
- **No LOCAL evidence reaches a REMOTE gate.** A REMOTE state carries no
  LOCAL record or certificate. Once S3 deletes them, the evidence parsers
  refuse the workspace variant and the `local` kind as unknown (D6).

### D5. The CLI, the configuration and the documents go entirely

- **`autoforge local` is deleted**: `local init`, `local run`,
  `local doctor`, their flags and their dispatch. There is no stub;
  argparse's `invalid choice: 'local'` is the refusal.
- **`local` leaves the known top-level keys.** A file that still has the
  block is refused by the ordinary unknown-key check. No removed-key
  message is added for it (the mechanism exists for section keys,
  `config.py` :1208). Naming LOCAL's removal at the point of use is the
  legacy guidance the decision excludes; the changelog explains it. The
  controller copies no LOCAL setting into a REMOTE one. An operator who
  wants REMOTE runs validated by `local.validation_commands` adds the
  commands to `validation.commands`. REMOTE runs each one in a byte-exact
  export of the candidate commit, not in the working tree (ADR 0006).
- **The current documents lose LOCAL** (Appendix A.3):
  - README's "Local mode (no GitHub)" section, with its
    `#local-mode-no-github` anchor, and every other LOCAL passage;
  - `docs/local-mode.md`, deleted rather than kept as a legacy page;
  - the LOCAL passages of the operator guides, `docs/configuration.md` and
    the security, compatibility, development and Pi documents;
  - the `local:` block of `autoforge.example.yaml`.

  `0.1.0a0`'s PyPI description links the anchor absolutely, so that link
  will land at the top of the README. That is accepted.
- **The packaged resources lose LOCAL.** `local init`'s feature-file
  template goes with its command (S2). The four LOCAL prompt templates go
  with the code that renders them (S3). From S3 on, the wheel and the sdist
  carry no LOCAL template, which S3 and S4 check.
- **The test tooling that runs LOCAL changes.** `tests/dist_check.py`
  drops its LOCAL dry run and keeps its REMOTE one. The live Pi smoke test
  gets a REMOTE flow before its LOCAL flow goes (Appendix A.2.3 G).

### D6. No LOCAL evidence becomes REMOTE evidence

A LOCAL run's fingerprint, run contract, `ValidationRecord`s, `local`
certificate and findings are never converted into a PR review binding, a
REMOTE `ValidationRecord`, a REMOTE certificate or anything a merge gate
reads, and no command adopts a LOCAL run into a REMOTE one.

The two identities do not denote the same bytes. A LOCAL certificate binds
a walk of the working tree under the run's workspace policy: ignored files
hashed, excluded entries named, mode bits, symbolic-link text, no commit. A
REMOTE certificate binds a commit's tree, exported byte-exact and proven to
hash back (ADR 0006 D6). The tree an operator later commits differs in
exactly the entries the two policies treat differently, so a converted
certificate would claim a review nobody did. An operator who wants a LOCAL
change merged commits it and runs REMOTE on an issue; the LOCAL review does
not carry over.

### D7. `sandbox.mode: preferred` keeps its name and means `required`

`preferred` falls back to logical isolation only in LOCAL; in REMOTE it
already is `required` (ADR 0005 D12.2; `sandbox_pin.mode_strength`,
`config.py` :743–748). With LOCAL gone it is `required` everywhere. It stays
an accepted value with that meaning, documented as such. The fallback code
goes: `engine.py` :2708–2746, and the `local=` arguments of the pin, the
policy and `doctor_sandbox.py`.

This is REMOTE compatibility. REMOTE configurations name `preferred`, and
REMOTE runs are pinned to it. Refusing the value would make each of them
fail to load, for no gain in safety: no REMOTE launch changes, a REMOTE pin
of `preferred` compares exactly as before, and REMOTE's validation policy
keeps the configured mode as it records it today
(`ValidationPolicy.requires_enforcement` already reads REMOTE's `preferred`
as `required`). The cost is a value that is now only an alias; the
documents say so.

### D8. The state protocol takes the next number, and every older refusal stays

**Removing LOCAL's fields from the state schema is a persisted change of its
own and takes protocol N + 1.** N is `__protocol_version__` on `main` when
the removal's PR (#388) is ready to merge. It is 12 at `02ce244`, but #126
or any other persisted change may take the next number first, so this
record reserves none. The PR follows #23's rule, from its
[2026-10-23 decision](https://github.com/iceboundrock/AutoForge/issues/23#issuecomment-6377402842)
and `state-and-recovery.md` ("Numbering a new protocol"):

- before the merge, one comment on #23 naming #388, the PR, N → N + 1, the
  twelve fields removed and the rule below, confirmed there;
- if `main` moves past N, a rebase onto the new N + 1 and a correction on
  #23;
- after the merge, the squash commit recorded on #23.

The bump carries no other persisted change.

**The rule composes today's loader with one more step. It is not a new
migration path, and it migrates no LOCAL run.** A file reaches the LOCAL
fields' step only after everything that decides today whether it loads has
accepted it, in today's order and with today's messages:

1. **The mode.** A file whose `mode` is anything but absent or `"REMOTE"`
   is refused as an unknown mode (D4), whatever its label, N + 1 included.
   A LOCAL file never reaches the steps below.
2. **Every per-protocol rule of the loader**, unchanged, its migrations and
   its refusals alike (`AutoForgeState.from_dict`, `state.py` :746–1084).
   These refusals stay:
   - a protocol-1 to 4 file whose replan journal is in flight at a stage
     its label could not bind (`replan_txn.legacy_journal_refusal`);
   - a protocol-1 to 3 file parked in `READY_FOR_MERGE` or `MERGE`, whose
     clean review lacks the PR, base or merge base the merge gate needs
     (`_legacy_review_binding_refusal`);
   - an older file carrying state its label never wrote: the planted
     effect, acceptance, configuration, validation, operator-action and
     pin refusals;
   - a protocol-7 to 12 or unlabelled file missing a field its label always
     wrote;
   - label 6, a label no controller wrote, a label that is not a string, an
     unknown phase, and a file that is not a JSON object.

   These files stay refused because the binding they lack cannot be
   reconstructed now any more than before. The removal changes none of
   these rules.
3. **Then the LOCAL fields.** For a file labelled N or older, each of the
   twelve is dropped when it is absent or holds the empty value every
   REMOTE save wrote (`""`, `0`, `{}`, `[]`). The fields arrived under
   protocol 1, on 2026-09-10 and 11, so a protocol-1 file carries none,
   some or all of them; every later save carries all twelve. A non-empty
   one is refused and the file left unchanged. Only LOCAL code ever wrote
   one (`engine.py` :5045–5864, and `new_local_run`, :3189–3263), so in a
   file that passed step 1 it is a hand edit or a foreign file, and
   dropping it would discard what the label cannot explain. That is
   stricter than today for eight of the twelve. Today a REMOTE file is
   refused for a non-empty `local_run_contract` or `local_pending_*`
   (`state.py` :1158–1167, :190–263); the other eight are only
   type-checked. A file labelled N + 1 never carries a LOCAL field, so one
   that does, even at its empty value, is refused. So is an unlabelled file
   that carries one, since an unlabelled file is read as the current
   protocol: N + 1 after the bump, as it is read as 12 today. Every save
   since the first controller has written a label, so an unlabelled file
   is a hand edit.
4. **Then today's unknown-field check and the checks after it,** minus the
   LOCAL ones the removal deletes.

A file that loads is relabelled on its next save, as every older label is;
a load writes nothing. A refused file is left byte for byte as it was.

| State file | Today (protocol 12) | After N + 1 |
|---|---|---|
| `mode: "LOCAL"`, any label or none | A LOCAL run, held to its contract | Refused as an unknown mode; nothing written (D4) |
| Any other `mode` but `"REMOTE"`, or one that is not a string | Refused | Refused, unchanged |
| REMOTE, a label that loads today, every LOCAL field empty or absent | Loads | Loads with them dropped; relabelled on its next save |
| REMOTE, a label that loads today, a LOCAL field non-empty | Refused for `local_run_contract` or `local_pending_*`; the other eight load | Refused, unchanged |
| REMOTE, protocol 1 to 3, in `READY_FOR_MERGE` or `MERGE` | Refused | Refused, unchanged |
| REMOTE, protocol 1 to 4, a replan in flight at a stage its label could not bind | Refused | Refused, unchanged |
| REMOTE, older, with state its label never wrote, or 7 to 12 missing a field its label always wrote | Refused | Refused, unchanged |
| Label 6, an unknown or non-string label | Refused | Refused, unchanged; N + 1 becomes known and N + 2 joins the unknown |
| REMOTE, N + 1, no LOCAL field | — | Loads |
| REMOTE, N + 1, any LOCAL field | — | Refused, unchanged |
| Unlabelled REMOTE | Read as 12 | Read as N + 1, so a LOCAL field is refused |
| Not JSON, not an object, oversized | Refused as corrupt | As today |

- **What N + 1 writes:** no LOCAL field. It keeps `mode`, always
  `"REMOTE"`. `status --json` consumers read it, and step 1 stays the same
  across labels. Whether `WorkflowMode` stays a one-member enum or becomes
  a constant is S3's choice; the persisted value is not.
- **What the evidence parsers refuse:** a workspace-variant
  `ValidationRecord` or a `local` certificate in any REMOTE file is
  corruption, as ADR 0006 D2.7's kind check already makes it. The records'
  and certificates' own `schema` stays 1: the REMOTE variants do not change
  shape, and the parser only narrows.
- **What never happens:** no LOCAL file is migrated, relabelled or turned
  into a REMOTE run, and no file the loader refuses today loads after the
  bump. S1b's and S3's exit tests (D12) hold the removal to the table
  above, the refusals included.

Two alternatives were rejected. Keeping label 12 with the LOCAL fields as
permanently empty columns leaves LOCAL's vocabulary in every future state
file and breaks the numbering rule. Dropping the fields under label 12
breaks the rule too, and lets an older controller load and continue a
REMOTE run a newer one saved, running older code over state it no longer
describes.

`status --json` of a REMOTE run loses the twelve empty LOCAL keys. That is
a change to its document and goes into the changelog (D10).

### D9. No fallback, and no promise about `0.1.0a0`

- **No document names a release, command or procedure** for continuing,
  inspecting, exporting or converting a LOCAL run after the removal.
- **`autoforge-cli` `0.1.0a0` stays on PyPI as it was published.** This
  record neither yanks nor deletes it, and promises nothing about it: not
  that it stays installable, not that it gets fixes (the version scheme
  has no `.postN`, `docs/releasing.md`), not that it still drives current
  agent CLIs. A security problem in it is a separate decision.
- **The configuration a LOCAL run used is the operator's** to keep or
  delete. Nothing tells them to keep it for a fallback.

A fallback is a support promise for a mode the project is removing. The
earlier revision showed what keeping it honest costs. The original
configuration, the canonical state directory, the checkout, the YAML extra
and the sandbox caveats all had to be documented and tested against the
published release. The maintainer judged that cost too high for a pre-Alpha
registration prerelease whose LOCAL use nobody can count (§1.2).

### D10. Release communication

- **The removal ships in the next `0.1.0aN` prerelease**, with no
  deprecation release before it. Nothing before 1.0 promises compatibility
  (`docs/releasing.md` sets no deprecation rule), and the only release with
  LOCAL is a pre-Alpha registration upload. Whether that prerelease is the
  Public Alpha is not decided here (§5.2). This record authorizes no tag,
  publication or release, and is not a Go/No-Go.
- **`CHANGELOG.md`** gets a `LOCAL mode is removed` entry under
  `## Unreleased`, marked **Behaviour change:**. The PR that closes the
  entry points (S2) writes it with:
  - what is removed: `autoforge local`, the `local:` key, LOCAL runs and
    `docs/local-mode.md`;
  - what an operator with LOCAL artifacts meets, with each error's text
    (D3, D4), including that `run --force` moves a state that does not load
    aside under a `.corrupt-` name;
  - that no release from this one on continues, shows, migrates or
    converts a LOCAL run;
  - the manual clean-up below;
  - that `preferred` means `required` everywhere.

  S3 adds the protocol bump with its migration rule, and the twelve keys
  `status --json` loses. The release notes are that section
  (`docs/releasing.md`).
- **The manual clean-up** is documented in that entry and automated
  nowhere. A LOCAL run may have left:
  - its state directory, with its run logs under it. That is
    `<git dir>/autoforge/state` by default (`.git/autoforge/state` from a
    main checkout, `.git/worktrees/<name>/autoforge/state` from a linked
    one), or the `--state-dir` or non-default `state_dir` it was started
    with;
  - the sandbox cache, `<git dir>/autoforge/sandbox/local`, if a run used
    the outer sandbox;
  - `features/<slug>.md` and whatever else the run left in the working
    tree, which is the operator's own work;
  - the `local:` block in the configuration file.

  An operator who wants a record archives a directory first (for example
  `tar -czf local-run.tar.gz <dir>`), then removes it (`rm -r <dir>`).
  Those are the only paths to remove: `<git common dir>/autoforge` also
  holds REMOTE's worktrees, lock, scheduler state and sandbox directories.
  AutoForge reads none of the LOCAL paths, so leaving them costs only disk
  space. Nothing in the controller moves, archives or deletes them.
- **Feature matrix and onboarding.** S2 removes LOCAL from README's
  "Current maturity" and "Supported now" and from every operator guide.
  The Quick start shows only REMOTE already. What #262's feature matrix
  says follows C7's decision on #255 (§5.2).
- **History stays:** `CHANGELOG.md`'s past entries and the ADRs (D11).

### D11. ADR status follows the code

Nothing is marked before the removal is complete on `main`, as ADR status
lines in this repository follow `main`. The PR that completes it (S3) adds
to ADR 0001's header: superseded by this record, except the parts that
keep describing `safefs.py` for REMOTE:

- §5.4, one capability boundary for every controller write;
- the parts of §5.5 that bound the run log and the state file;
- the hard-link limitations of §8.8 and §8.10.

Each LOCAL decision of ADR 0003 to 0008 that Appendix A.4 lists gets a
one-line note naming this record. Accepted records are not rewritten: what
they decided for LOCAL stays readable as history. ADR 0007's LOCAL
obligations (its LOCAL interaction identity, D8.2, D8.5) are unimplemented,
and the child issue that implements ADR 0007 can drop them without
waiting.

### D12. Sequencing and exit tests

The removal is five pieces of work after this record, in four steps. Each
leaves `main` green and every REMOTE behaviour unchanged, and each has exit
tests that must exist and pass before it merges. §5.3 maps the steps onto
EPIC #383's issues.

| Step | Issue | Lands | Needs first |
|---|---|---|---|
| S1a. Replace first | #386 (RET-A3) | Every REPLACE row of Appendix A: REMOTE's git reader out of `local_workspace.py` and free of `config.local` (C1); a REMOTE or mode-neutral test for each row of A.2.3, among them the live Pi flow's REMOTE form (G) and the candidate-validation preflight tests (H); the helpers other modules import from `test_local.py`, moved to a shared home. LOCAL still works | This record |
| S1b. Pin the state | #385 (RET-A2) | Tests written against today's code that S2 and S3 must keep passing: every REMOTE refusal D8 keeps, the protocol-1 merge-phase refusal no test pins yet, and the `run --force` and scheduler audits of D4. No behaviour change. LOCAL still works | This record |
| S2. Close every way in | #387 (RET-A4) | D3 to D5: the `local` subcommand; the `local:` key (the internal `LocalConfig` type stays until S3, read by nothing reachable); the loader's mode refusal; the probe; the scheduler's LOCAL skip; `dist_check.py`'s LOCAL half; the example's `local:` block; the live Pi flow's LOCAL form; the current documents; the changelog entry. The tests those closures break or empty, each deleted, rewritten to assert its refusal or cut to its REMOTE rows (A.2.1's S2 column), and the test contracts' requirements they leave without a test (A.2.5). LOCAL becomes unreachable, but its code is still there, and so are the tests that call it directly | S1a: C1 before the `local:` key goes; G before the key and the subcommand go; B and C before the mode refusal. S1b |
| S3. Delete | #388 (RET-A5) | Every DELETE row of A.1, with the tests that still call it after S2: the LOCAL engine paths, transitions, prompts and result models, `run_contract.py`, the rest of `local_workspace.py`, the LOCAL launch classes, the workspace record variant and the `local` certificate kind, `doctor`'s LOCAL subset, `LocalConfig`. D8's protocol bump. The instruction maps, the agent guides and the rest of the test contracts. D11's ADR notes | S2. The protocol number confirmed on #23 |
| S4. Measure and close | #389 (RET-A6) | Appendix B's commands, and `tests/parallel_bench.py --config serial --repeats 3`, run again on `4b03151` and on S3's last commit, back to back on one host; the full check set; the installed-package checks; the documentation audit; release readiness without a release | S2 and S3 |

Why this order:

- **S1a and S1b come before anything destructive.** A coverage loss is
  invisible in a diff of deletions. And a pin written in the same PR as the
  change it guards is written against the new code, so it cannot show that
  the change kept the old behaviour. S1a and S1b are independent of each
  other.
- **S2 carries the mode refusal together with the closed commands.** The
  refusal cannot land earlier: `local run` would then write a state it
  could not read back while LOCAL is still supported. It cannot land later
  either. Once S2 removes the `local:` key, no configuration exists to hold
  a LOCAL run to, and a LOCAL state that still loaded would reach LOCAL
  engine code with nothing to check it against. So the refusal lands in the
  PR that removes the last way to start or configure a LOCAL run.
- **S3 comes after S2.** Deleting code that is still reachable would break
  LOCAL while it is supported. Once S2 has made the code unreachable, S3 is
  a deletion whose safety S1b's and S2's tests already pin.
- **Each step retires the tests of what it removes, and leaves the full
  suite passing.** A test that drives a behaviour a step removes is deleted
  in that step, rewritten to assert the refusal that replaces it, or cut to
  its REMOTE rows. None is skipped, deselected or marked expected to fail to
  bridge two steps. S2 shuts every way into LOCAL, so it retires every test
  that reaches LOCAL through one: the `local` subcommand, a `local:` block
  read from a file, a LOCAL state loaded from disk, the probe, README's
  LOCAL text, the live Pi flow's LOCAL form. That is 160 functions,
  measured, not estimated (A.2). The other LOCAL tests call LOCAL code
  directly, through `make_local_engine`'s state in memory or as units of a
  module, and pass after S2. S3 deletes them with that code.
- **Every merge leaves a coherent interface.**
  - After S1a and S1b, LOCAL works and REMOTE is unchanged.
  - After S2, nothing starts, configures, continues or shows a LOCAL run,
    and REMOTE is unchanged.
  - After S3, the code is gone and the protocol is N + 1.

**S1a exit tests (#386).**

- Each REPLACE row names its new test, and the test passes with LOCAL still
  present. No REMOTE test is weakened.
- H: the REMOTE tests of A.2.3 H pass, and each fails when its guard is
  removed, in a mutation run recorded in the PR. The refusal cases fail
  when the preflight call in `_run_remote_validator` (`engine.py` :6266)
  is removed. The reachability case fails when the export is created
  inside the repository. Without them S3 may not delete `test_local.py`'s
  two preflight tests.
- A test fails if `engine.workspace()` (or its successor) reads
  `config.local`.

**S1b exit tests (#385).** They are written against today's code. S2 and
S3 then keep them unchanged, apart from the expected protocol label and any
row that names LOCAL.

- *Every refusal D8 keeps.* Each test below keeps passing, and runs on its
  file twice: once with the twelve LOCAL fields present at their empty
  values and once with them absent.
  - `test_state.py`:
    - the refusal half of
      `test_protocol_1_is_migrated_only_without_a_replan_in_flight`;
    - `test_protocol_2_is_refused_in_the_merge_phases` and
      `test_protocol_3_is_refused_in_the_merge_phases`;
    - `test_protocol_3_is_refused_with_a_replan_in_flight` and
      `test_protocol_4_is_refused_with_a_replan_past_the_write`;
    - `test_a_legacy_file_carrying_effect_state_is_refused_and_left_unchanged`;
    - `test_a_protocol_7_or_8_file_missing_an_effect_field_is_refused_and_left_unchanged`
      and `test_an_unlabelled_file_missing_an_effect_field_is_refused`;
    - `test_a_protocol_7_plan_carrying_an_acceptance_comment_is_refused`;
    - `test_a_protocol_9_file_missing_its_recorded_configuration_is_refused`;
    - `test_an_older_file_carrying_validation_evidence_is_refused` and
      `test_a_protocol_10_file_missing_a_validation_field_is_refused`;
    - `test_a_protocol_11_file_missing_its_operator_outcomes_is_refused`;
    - `test_a_protocol_12_file_missing_its_sandbox_pin_is_refused`;
    - the corrupt-file tests
      (`test_load_corrupted_json_raises_and_does_not_clobber` and the
      others of that group);
    - `test_protocol_6_and_other_unknown_labels_are_refused_and_left_unchanged`,
      whose list of unknown labels includes `"13"` today and so moves past
      N + 1 in S3.
  - `test_replan.py`:
    - `test_r7f1_an_in_flight_protocol_1_journal_is_refused_at_the_state_boundary`;
    - `test_an_in_flight_protocol_2_journal_is_refused_at_the_state_boundary`;
    - `test_an_in_flight_protocol_3_journal_is_refused_at_the_state_boundary`;
    - `test_an_in_flight_protocol_4_journal_past_the_write_is_refused_at_the_state_boundary`;
    - `test_an_unreadable_stage_in_a_protocol_4_journal_is_still_a_refusal`;
    - `test_r7f1_an_unreadable_stage_in_a_protocol_1_journal_is_still_a_refusal`.
  - `test_cli.py`:
    `test_status_and_resume_refuse_an_in_flight_protocol_1_replan`.
- *A refusal no test pins yet.* A protocol-1 file parked in
  `READY_FOR_MERGE` or `MERGE` is refused today
  (`_REVIEW_BINDING_GAPS["1"]`), but only protocols 2 and 3 have that test.
- *A state that does not load.* The files are an unknown mode, an unknown
  label and invalid JSON.
  - `status`, `step`, `resume` and `unblock` exit 2 on each. The state
    directory and its logs are byte-identical afterwards, by a hash of the
    tree, twice in a row.
  - `run` without `--force` refuses with the same tree unchanged.
  - With `--force`, the moved file is byte-identical under
    `state.json.corrupt-<timestamp>`, and no existing file is replaced. The
    new state carries none of its fields, and the move is printed.
- *The scheduler.*
  - A pass with such a file at `config.state_dir` admits as before,
    reports the problem and writes nothing.
  - A namespace state with a mode other than REMOTE is held as uncertain.
  - `test_legacy_standalone_state_is_never_read_moved_or_modified` keeps
    passing: namespace work leaves a directory at
    `<git dir>/autoforge/state` untouched.

**S2 exit tests (#387).**

- `autoforge local`, alone and with each former subcommand, exits 2 from
  argparse, creates or changes no file and takes no lock. `--help` lists no
  `local`.
- A `local:` key is refused with the unknown-key error by every command
  that loads configuration, `doctor` and the `epic` commands included.
  Nothing is written.
- A `mode: "LOCAL"` file is tested under each label from 1 to 12, under
  `13` and with no label, both with an otherwise valid REMOTE shape and
  with garbage beside the mode.
  - `status`, `status --json`, `step`, `resume` and `unblock` exit 2
    naming the mode, and the directory is byte-identical.
  - `run` refuses, and `run --force` behaves as in S1b's test.
  - The files are written by hand in the test. None comes from a release.
- With a LOCAL state only at `<git dir>/autoforge/state`, `status` without
  `--state-dir` reports no run at the configured location, and the file is
  byte-identical.
- S1b's scheduler pass, with a `mode: "LOCAL"` state.
- `dist_check.py` passes on its REMOTE half, and REMOTE's CLI tests pass
  unchanged.
- S1a's and S1b's tests pass.
- *The full suite passes at S2's last commit,* CI's real-backend sandbox
  job included, with no test skipped, deselected or marked expected to fail
  for LOCAL.
  - Every function of A.2.1's S2 column is gone, rewritten to assert the
    refusal that replaces it, or cut to its REMOTE rows. A REPLACE original
    among them (B, C, G) goes only once its S1a replacement passes on
    `main`, so no shared regression is unprotected at any commit.
  - A test that S2 breaks and the column does not list, one added since
    `02ce244` (§4), is S2's as well. S2 may retire more of the LOCAL tests
    early, never fewer.
  - The test contracts drop what S2 retires and require the refusals above
    (A.2.5).

**S3 exit tests (#388).** These hold the removal to D8's table. A refused
file must be left byte-identical, with no `.corrupt-*` file and no relabel.

- *Positive.*
  - Protocol-12 REMOTE files captured from `main` in every phase load, and
    are relabelled N + 1 with the LOCAL fields dropped on their next save.
  - So does a file under each older label that loads today.
  - So do a pre-LOCAL protocol-1 file without the fields and a protocol-1
    file with some of them.
- *Every refusal of today, kept.* S1b's tests pass with only the expected
  label moved to N + 1.
- *The new refusals.*
  - An older REMOTE file with any one of the twelve fields non-empty is
    refused, one case per field.
  - An N + 1 file carrying any LOCAL field, even an empty one, is refused,
    and so is an unlabelled file carrying one.
  - A `mode: "LOCAL"` file under any label, N + 1 included, is refused as
    an unknown mode.
  - A workspace-variant record or a `local` certificate in a REMOTE file is
    refused.
- *Structural.*
  - A test fails on `LOCAL_LEGAL_EDGES`, a LOCAL launch class,
    `WorkflowMode.LOCAL` or an import of a deleted module.
  - The wheel built from the commit contains no `local_*.md`.
- S1a's, S1b's and S2's tests pass.

**S4 exit tests (#389).** The after-numbers are recorded beside Appendix
B's, with the commands, interpreters, host and load. A difference is
reported as measured, and called a speedup only when the repeated runs put
it beyond their spread: Appendix B's two pytest runs differ by 36 s between
interpreters alone.

## 3. Options considered

**For an existing LOCAL run.**

- (a) **Strict generic refusal, with nothing of LOCAL kept (chosen,
  D3, D4).**
- (b) Recognise it, show it read-only, keep an `autoforge local` stub and
  document `0.1.0a0` as the fallback. This was the earlier revision's
  design. The maintainer approved it, then superseded the approval for the
  cost of keeping it honest (D9).
- (c) Migrate it into a REMOTE run. There is no issue, PR or commit for it
  to bind to, and D6 forbids carrying its review across.
- (d) Keep a minimal LOCAL engine that can finish runs already started. It
  keeps the run contract, the walk and the LOCAL transitions, which are
  most of what the removal deletes.
- (e) Move or delete its state directory on upgrade. #383 and the decision
  forbid it, and it would destroy the only record of work still in the
  operator's tree.

**For LOCAL's default location.** Delete the probe (chosen, D3), or keep it
to tell the operator a LOCAL run exists there. The second is the LOCAL
state-directory discovery the decision excludes.

**For `run --force` over a LOCAL file.** Today's generic move aside
(chosen, D4), or a refusal specific to LOCAL. The second needs a detector,
and the move already keeps the bytes.

**For the protocol.** N + 1 (chosen); label 12 with the LOCAL fields kept as
empty columns; label 12 with the fields dropped. D8 gives the reasons.

**For `preferred`.** Keep it as `required` (chosen); refuse it. D7 gives the
reasons.

**For the release.** Remove LOCAL in the next prerelease (chosen, D10), or
ship a deprecation warning first. The second costs a release whose only
content is the warning, for the users of one pre-Alpha upload.

## 4. Risks

- **An operator with a LOCAL run upgrades and cannot continue it, or even
  see it.** That is the decision. The mitigations are refusals that write
  nothing, files nothing touches, and a changelog entry that says where the
  files are and how to archive or remove them.
- **`autoforge status` says there is no run while a LOCAL run sits at
  LOCAL's default location.** That is the accepted cost of deleting the
  probe (D3). The changelog names the location.
- **`run --force` moves a LOCAL state aside under a `.corrupt-` name.**
  Only on the operator's explicit flag, with the bytes kept and the move
  printed (D4).
- **A shared mechanism loses coverage** because its only test was a LOCAL
  one. S1a exists for this; Appendix A.2.3 is its checklist.
- **The S1a split of `local_workspace.py` changes REMOTE.** It moves code
  every REMOTE run executes. It must be a move with the REMOTE tests
  unchanged, not a rewrite.
- **The drop step lets a refused file through.** If it ran before a
  per-protocol refusal, or a refusal were rewritten along with the LOCAL
  rows, an older file the loader refuses today could load. D8 orders the
  step after every refusal, and S1b pins each refusal on today's code, with
  the LOCAL fields present and absent.
- **Unreachable LOCAL code between S2 and S3.** It still compiles and is
  type-checked, and the LOCAL tests that call it directly still run (A.2).
  S2's tests show that no entry point reaches it, and S3 deletes it with
  those tests. S2 and S3 should land close together.
- **The protocol bump meets another persisted change.** The numbering rule
  already settles it: whichever merges second takes the next number.
- **The order against the Public Alpha is undecided** (§5.2). Until it is
  decided, whichever lands first must leave #255's C7 wording true. (The
  maintainer decided it on 2026-10-30: option 1, §5.2's closing note.)
- **The inventory is a snapshot of `02ce244`.** A LOCAL reference added
  after it is not in Appendix A. Each step re-greps before it merges, and
  S3's structural test catches what is left.

## 5. Related issues and the EPIC's order, read on 2026-10-30

#384 asks for #155 and #255 to be reviewed, for the LOCAL-only edits to be
applied to #155, and for a change to C7 to be proposed on #255 without
changing any gate. This section is that review, from each issue's body,
comments and native links as read on 2026-10-30, and the text of the edits
and the proposal.

**What this record does not evidence is that they were made.** Neither its
text nor PR #406's description shows what #155 and #255 carry. The
evidence is one comment on each issue that the controller reads back and
verifies for PR #406: on #155, a note carrying §5.1's edits; on #255, the
proposal comment already posted there (§5.2), bound as the evidence rather
than posted a second time. Until both are verified, this part of #384 is
unverified, not done.

### 5.1 #155: SDK provider parity

#155 is open, under EPIC #147. It is natively blocked by #126 and #151 to
#154 and blocks #156. It has no dependency link to EPIC #383, natively or
in either body; #383 names it as interacting work. It proves SDK providers
at the engine boundary "across REMOTE/LOCAL workflows". Its LOCAL
requirements are:

- the title;
- ordinary parity case 5;
- "LOCAL launch count" among the budgets its interaction section tracks;
- helper fixture assets kept "outside LOCAL reviewed trees".

Each exists only for LOCAL parity. Each has a REMOTE counterpart, already
in #155 or in the REMOTE workflow, and that counterpart stays required:

| #155 requirement | After EPIC #383 |
|---|---|
| "LOCAL zero gh calls (ExplodingGitHub)" | Superseded. REMOTE uses `gh` by design, and case 4 already fails any agent claim FakeGitHub does not back |
| "contract cwd/spec/HEAD/branch frozen" | REMOTE form: each SDK launch runs in the issue worktree (case 1), and the controller's own checkout keeps its HEAD and branch around it, or the run is `BLOCKED` |
| "validation and tree fingerprints unchanged; REVIEW edits refused, never rebound" | REMOTE form: candidate validation binds the commit and tree (ADR 0006). A review round during which the PR head, base or merge base moved is a stale round, consumed and never bound to the new revision (`workflow.md`) |
| "Restarted write-capable continuations charged to durable LOCAL launch cap; inline decisions not charged. Prelaunch errors no charge" | REMOTE form: a restarted write-capable continuation is charged once, to REMOTE's bounds (phase attempt, workflow step, review round, correction attempts). An inline decision and a pre-launch error charge nothing |
| "LOCAL launch count" among the tracked budgets | Superseded. Every other budget in that list stays |
| helper fixture assets "outside LOCAL reviewed trees" | Outside the issue worktree and any candidate tree |

Everything else in #155 stays as written:

- REMOTE parity on every profile;
- the real adapters and helpers, driven at their boundary;
- the controller's evidence and GitHub checks;
- interaction and crash recovery;
- budget accounting and process containment;
- sandbox and credential isolation;
- offline, deterministic CI.

These are the edits #384 asks to apply to #155. They go to #155 as an
amendment note that lists each LOCAL line beside its REMOTE form, so that
the original text stays readable; that note, verified on #155, is their
evidence (§5). No dependency edge is added in either direction. D2 lets an
SDK change refuse a LOCAL run instead of implementing it, so #155 need not
wait for #383, and #383 need not wait for #155.

One line of #155 predates protocols 7 to 12: "Protocol migration 5→next
version and all older replan compatibility refusals retained". Read with
D8, it means every older protocol's migration and refusal, the LOCAL
fields' step included. #383 does not cause that correction, so it is noted
here and left unchanged.

### 5.2 #255: Public Alpha

On 2026-10-23 the maintainer decided #255's conditional blockers
([C1 to C7](https://github.com/iceboundrock/AutoForge/issues/255#issuecomment-6377402004)).
C7 makes LOCAL mode "an advanced path, documented but outside the
first-release Quick start". Under it, `docs/local-mode.md` stays, #262's
feature matrix sets LOCAL's status from its evidence, and #259's
clean-room run needs no LOCAL first run. EPIC #383 removes that path.
#255 and #383 have no dependency link, native or in either body, and the
maintainer's decision on #384 leaves their order open. It also says that a
change to C7 must be proposed explicitly and approved separately.

Where LOCAL appears in #255 and its children:

- **C7**, above. README's "Supported now" tier lists "LOCAL mode against a
  feature Markdown file", which PR #296 found consistent with C7.
- **C1** (Pi, experimental). Its only live evidence, #135's run, is a LOCAL
  REVIEW. After the removal that evidence describes a removed mode. S1a's G
  gives Pi a REMOTE flow to gather new evidence through, but C1's status
  does not change by itself.
- **§C**, the 2026-10-21 assessment, which lists LOCAL mode as implemented
  and verified. It is a dated baseline.
- **#259** (LAUNCH-A). Its item 7 applies only if C7 promotes LOCAL, which
  C7 does not. #255's §I lists LOCAL among #259's offline commands.
- **#260** (LAUNCH-F). Its sources include `docs/local-mode.md`'s "Resume
  and recovery", and its exit-code table includes `local …`.
- **#262** (LAUNCH-C). The feature matrix has a LOCAL row, and
  `tests/test_local.py` pins a README sentence (#262's comment) that moves
  with D5's README edits.
- **#241** (Gates 4 and 5). Its item 3 asks for the REMOTE validator's
  credential tests, which are A.2.3 H's tests.

**Proposed C7, for the release that contains the removal:** "LOCAL mode:
removed (EPIC #383). Not advertised, documented or packaged; no feature-matrix
row. The release notes record the removal as a breaking change." With it:

- #262's matrix drops the LOCAL row, and Gate 6's checklist checks that no
  current document advertises LOCAL;
- #259's offline commands drop LOCAL;
- #260 drops `docs/local-mode.md` as a source and `local …` from its
  exit-code table, and covers the generic refusals of D3 instead;
- C1 stays Experimental. Its evidence note says that #135's run was in a
  removed mode, and new evidence comes through a REMOTE flow.

No gate is added, waived or satisfied by this. #241's item 3 stays a Gate 4
and 5 blocker until its tests are on `main`, whether #241 or #386 lands
them; the second to land links the first. Gate 5's rule, no Critical or
High finding open, applies to whatever the release candidate carries.

**The order is the maintainer's decision.** These are the options:

| Option | What happens | Impact |
|---|---|---|
| 1. The removal before the release candidate | #255's RC waits for S2, and for S3 if the Alpha is to carry the protocol change | A new hard blocker on #255. The Alpha never advertises LOCAL |
| 2. The Alpha first, C7 as decided | LOCAL ships frozen (D2) as an advanced path, and the removal follows in a later `0.1.0aN` | #255 is unchanged. Strangers can start LOCAL runs that the next prerelease refuses, with no supported path to finish them |
| 3. No ordering edge; C7's wording follows `main` at the RC (recommended) | If S2 is on `main` at the RC, C7 reads as proposed above. If not, it reads "advanced path, frozen and scheduled for removal (EPIC #383); a LOCAL run will not continue in the release that removes it" | No new blocker and no gate changed. Gate 6 checks C7's wording against `main`, as it checks every row. It changes C7's advertised wording, which needs the maintainer's approval |

Option 3 is recommended. It keeps the launch independent of #383, and in
either case it tells a stranger the truth about LOCAL, including that a
LOCAL run does not survive the removal. Without a fallback, option 2's
cost is higher than it was under the earlier design. Option 3 is a
proposal for #255, not a decision. Until the maintainer decides, neither
EPIC gains a dependency on the other, and C7 stays as recorded. The
proposal for that decision is
[a comment on #255](https://github.com/iceboundrock/AutoForge/issues/255#issuecomment-6531809299).
It counts as made once the controller has verified that comment on #255
(§5), and it is not posted again.

**Decided on 2026-10-30, after this section was written.** The maintainer
chose [option 1](https://github.com/iceboundrock/AutoForge/issues/255#issuecomment-6533695132),
the removal before the release candidate, not the option recommended
above, and decided
[C7](https://github.com/iceboundrock/AutoForge/issues/255#issuecomment-6533713412)
as "LOCAL mode: Removed. Not advertised or supported in Public Alpha.",
which #255's C7 row now records. That wording, not the proposed one
above, is C7. The text above stays as what was proposed; C.9 says what S4
found on `main` against the decision.

### 5.3 EPIC #383: steps, workstreams and dependencies

Until 2026-10-30 GitHub had no native sub-issue, parent or `blocked_by`
link among #383 to #389; the edges were stated in issue bodies only. With
this revision the maintainer had them created natively, and they were read
back through the sub-issue and dependency APIs the same day:

- #384 to #389 are sub-issues of #383;
- #385 and #386 are blocked by #384;
- #387 by #385 and #386;
- #388 by #385, #386 and #387;
- #389 by #387 and #388.

The edges to #23, #155, #241 and #255 below are not native, and none is
proposed.

| Step | Issue | Change to the issue's scope |
|---|---|---|
| S1a | #386 (RET-A3) | Names the reader split (C1) and A.2.3's eight rows, H's two cases among them, each with its deletion prerequisite |
| S1b | #385 (RET-A2) | Narrowed from a legacy-compatibility contract to REMOTE state pins and the generic-refusal audits. The mode refusal moves to #387 and the migration rule to #388 |
| S2 | #387 (RET-A4) | Removes the public surface completely, with no stub. It gains the mode refusal, the probe's deletion and the scheduler skip's, and the retirement of every LOCAL test those closures break, with the test contracts' requirements for them (A.2.1, A.2.5), so that the suite passes at its last commit. The LOCAL prompt templates, ADR 0001's note and the instruction maps move to #388 |
| S3 | #388 (RET-A5) | Deletes the unreachable runtime and evidence code with the unit tests that still call it, carries D8's bump and the #23 announcement, and keeps no legacy discriminator |
| S4 | #389 (RET-A6) | Measures, verifies, audits the documents and installed package, reads back the native links, and prepares the release without making it |

**#385 is narrowed, not merged into #388.** Its remaining work is pinning
REMOTE's state refusals and auditing `run --force` and the scheduler. That
has to land before the destructive PRs, for the reason S1a does: a pin
written in #388 would be written against the code it is meant to check.
The migration rule itself belongs in #388, the PR that removes the fields,
and it moves there.

| Edge | In the bodies before | This record | Native | Why |
|---|---|---|---|---|
| #385 ← #384, #386 ← #384 | Yes | Yes | Yes | |
| #385 ↔ #386 | No | None | No | Independent |
| #387 ← #385 | Yes | Yes | Yes | The pins exist before the mode refusal lands |
| #387 ← #386 | "Coordinate" | Hard | Yes | C1 before the `local:` key goes; G before the key and the subcommand go; B's and C's replacements before S2 retires their LOCAL originals (A.2) |
| #388 ← #385, #386, #387 | Yes | Yes | Yes | Nothing reachable is deleted, and no LOCAL-only security regression is deleted before its REMOTE replacement passes (A.2.3 H) |
| #388's protocol bump ← #23 | No | Confirmed on #23 before the merge (D8) | No | A process gate, not an issue edge |
| #389 ← #387, #388 | Yes | Yes | Yes | |
| #241's item 3 and #386's H | No | One set of tests | No | Whichever lands first; the other links it |
| #255 and #383 | No; #383 names #255 as interacting work | Undecided (§5.2) | No | No edge until the maintainer decides |
| #155 and #383 | No; #383 names #155 as interacting work | None (§5.1) | No | |

GitHub keeps each issue body as the authoritative copy of that step's
scope, and the native links above are the ones the EPIC scheduler reads.
So a body that disagrees with this table is brought into line before its
step starts, and S4 reads the native links back.

## Appendix A. Reference inventory

Taken at `4b03151`, by reading every match of `WorkflowMode.LOCAL`,
`is_local`, `LOCAL`, `local_*`, `features/`, the run contract, the
workspace walk and fingerprint, the `local:` block, the LOCAL launch
classes and the `local` certificate. The search covered `src/`, `tests/`,
the prompts, the configuration example, CI, the release artifacts and
`docs/`, and each claimed REMOTE use was checked with a grep for its
callers. The references were then moved to `02ce244` (see the header). "local" in the sense of
a local branch, a local checkout, local git configuration or "on this host"
is not counted. Each step re-greps before it merges (§4).

### A.1 Source

The deletion estimate is a reviewer's count over method and branch
boundaries, not a diff: about 4,500 lines under `src/autoforge/` (75,129
lines of Python at `02ce244`, by `wc -l`), net of the legacy code the
earlier revision added to `cli.py` and `state.py`. The largest pieces are
`engine.py` (about 1,450 to 1,500 of 16,913), `local_workspace.py` (about
960 of 1,253), `run_contract.py` (all 408), the four LOCAL prompt templates
(427), `result_parser.py` (about 205), and `cli.py` and `state.py`.

**Entry points, configuration, doctor**

| Item | Where | Class | Note |
|---|---|---|---|
| `local` subparser and dispatch, `_local_engine_for`, `cmd_local_init`, `cmd_local_run`, `cmd_local_doctor` | `cli.py` :326–387, 564–571, 623–631, 1604–1676 | DELETE (S2); GENERIC STRICT REFUSAL (argparse) | D5 |
| `_bind_existing_run`'s two-location probe | `cli.py` :634–667 | DELETE (S2) | D3; no replacement. It runs a `git rev-parse` on every REMOTE `status`, `step`, `resume`, `unblock` without `--state-dir` (C4) |
| `_existing_run_guard` and `run --force`'s move aside | `cli.py` :1485–1551, 1580–1592 | PRESERVE SHARED | D4's audit; unchanged |
| LOCAL arms of `status`, terminal and evidence rendering; `_render_local_status` | `cli.py` :683–684, 1821, 2038–2062, 2084–2086, 2170–2186, 2229–2254, 2353, 2415, 2435–2437, 2508, 2603, 2717–2770 | PRESERVE SHARED (arms and `_render_local_status` DELETE, S3) | Unreachable after S2 |
| `local` in `TOP_LEVEL_KEYS` | `config.py` :52–56 | DELETE (S2); GENERIC STRICT REFUSAL (unknown key, :1180) | D5 |
| `LOCAL_KEYS`, `LocalConfig`, its parser and defaults | `config.py` :621–679, 764, 808, 1512–1551 | DELETE (the parser in S2, the type in S3) | REMOTE reads it through `engine.workspace()` until S1a (C1) |
| `refuses_unenforced(local)` and `preferred`'s fallback | `config.py` :696–705, 743–748 | PRESERVE SHARED (simplified) | D7 |
| `doctor`'s LOCAL subset: `check_feature_spec`, `check_validation_commands`, `run_local`, `_local_agent_checks`, `_workspace`, `check_local_state_dir`, `run_local_doctor` | `doctor.py` :54–57, 116–118, 803–848, 856–876, 1281–1325, 1541–1551 | DELETE (S3) | `run_all` never calls them; `local doctor` is their only entry, gone in S2 |
| The `local` parameter of the sandbox rows | `doctor_sandbox.py` :128–206, 432–480 | PRESERVE SHARED (arms DELETE) | |
| `LOCAL_BASE_PROFILES`, `REVIEW_PROFILE_THRESHOLDS`, `local_required_profiles` | `profiles.py` :45–84 | DELETE | REMOTE uses `REQUIRED_PROFILES` |

**Workflow and engine**

| Item | Where | Class | Note |
|---|---|---|---|
| `WorkflowMode.LOCAL` | `transitions.py` :84–90 | DELETE (S3); GENERIC STRICT REFUSAL (the loader's unknown mode) | D4. S2 refuses it explicitly while the member exists |
| `LOCAL_LEGAL_EDGES`, `stop_phases_for`, the LOCAL block of `decide_next_phase`, the `mode` parameters | `transitions.py` :225–275, 326–336 | DELETE (functions PRESERVE) | |
| `LOCAL_PHASES`, `LOCAL_WRITE_PHASES` | `transitions.py` :115–120, 238–249 | DELETE | |
| `Phase.DONE` | `transitions.py` :102 | PRESERVE SHARED | `UPDATE_EPIC → DONE` |
| The LOCAL step: `_local_step_once` and the 16 methods after it, the launch checkpoint and three-launch bound, fingerprint binding, validation convergence, `local` certificate issuance (:5540–5550) | `engine.py` :5045–5864 | DELETE | Entry :4883; outside call `_charge_local_launch` :13252 |
| `new_local_run`, `_check_baseline_clean`, `bind_local_state_dir` | `engine.py` :3111–3121, 3189–3263 | DELETE | |
| The LOCAL dry-run plan; the LOCAL prompt variables | `engine.py` :4421–4546; 3670–3705, 3730–3755 | DELETE | |
| `_local_facts`, `_say_fallback`, `_require_local_review_evidence`, `_validation_failure`, `_reused_failure`, `_local_budget_block_reason`, `MAX_LOCAL_PHASE_ATTEMPTS`, `LOCAL_PHASE_TEMPLATE`, `_invocation_contract`, `local_contract` | `engine.py` :595–634, 1082–1092, 1535–1609, 1887–1906, 2742–2746, 2841–2876, 6525–6580, 9540–9546 | DELETE | |
| The contract gate in `load()`; the LOCAL arm of `_configured_validation` | `engine.py` :3278–3281, 6588–6592 | DELETE (S3) | Unreachable after S2: the loader refuses a LOCAL state first |
| `unblock`'s LOCAL refusal | `engine.py` :7900–7904 | DELETE (S3) | The same |
| `local_state_paths()`, function and method | `engine.py` :1275–1312, 3123–3125 | DELETE | D3. Its only REMOTE caller, the probe, goes in S2 |
| `workspace()` | `engine.py` :1868–1885 | REPLACE | C1 |
| `preferred`'s fallback: `_sandbox_fallback`, `_fallback_facts`, `_fallback_notice` | `engine.py` :2708–2740 | DELETE | D7; REMOTE never reaches it (:2713) |
| LOCAL arms in shared methods: sandbox routing and denied roots (:2636–3090), `_execution_cwd`, the run-configuration record and check, `validate_config`, `template_for`, `run`, `_step_once`, the pre-launch charge in `_invoke_phase` (:13247–13256), the transient relaunch (:13450), `_log_metadata`, `_record_invocation`, `_moved_checkout_recovery`, the evidence renderers (:919–995, 14330) | `engine.py` | PRESERVE SHARED (arms DELETE) | 15 `mode == REMOTE` guards become always true |
| `_checkout_anchor`, `_invoke_phase_anchored` | `engine.py` :2951–3032 | PRESERVE SHARED | Already REMOTE's own code |
| LOCAL result models, `LOCAL_FIX_RESOLUTIONS`, `_validate_local_phase`, the `mode` parameter of `parse_control_result` | `result_parser.py` :106, 117–120, 330–340, 1994–2201, 2208–2219 | DELETE | `_opt_str_list` (:2003) is shared: it moves |
| `local_common.md`, `local_analyze_execute.md`, `local_review.md`, `local_fix.md`; their `TEMPLATE_FILES` entries and `LOCAL_COMMON_TEMPLATE` | `prompts/` | DELETE (S3) | REMOTE templates do not change, so `__prompt_version__` need not |
| `validation_evidence.md`, `correction.md` | `prompts/` | PRESERVE SHARED | |

**State and protocol**

| Item | Where | Class | Note |
|---|---|---|---|
| `mode` and its parse | `state.py` :457–461, 735–764 | GENERIC STRICT REFUSAL | D4: absent or `"REMOTE"` only. N + 1 keeps writing `"REMOTE"` (D8) |
| The twelve LOCAL fields | `state.py` :568–618 | DELETE with D8's drop step (S3) | C2 |
| `_validate_local_pending`, the LOCAL topology and required-field checks, the contract parse, `local_contract()` | `state.py` :190–263, 727–732, 1105–1167 | DELETE | The REMOTE refusal of a non-empty `local_run_contract` or `local_pending_*` becomes D8's step 3 |
| The pre-contract LOCAL refusal | `state.py` :1045–1058 | DELETE (S3) | The mode check refuses every LOCAL file first |
| LOCAL arms of the effect, acceptance, operator and recorded-configuration validators | `state.py` :1266, 1310–1317, 1712–1768, 1905–1917 | PRESERVE SHARED (arms DELETE) | |
| `StatePaths.anchor`; `canonical_state_dir` | `state.py` :2059–2106; 2108–2110 | PRESERVE SHARED; DELETE | `anchor` serves the scheduler namespaces (`run_namespace.py` :281) |
| `quarantine_state_file` | `state.py` :2269 | PRESERVE SHARED | D4's audit |
| `run_contract.py` | whole file | DELETE | |
| `__protocol_version__` | `__init__.py` :4 | PRESERVE SHARED | Bumped by D8 |

**Workspace, filesystem, evidence, sandbox, scheduler**

| Item | Where | Class | Note |
|---|---|---|---|
| `LocalWorkspace`'s git plumbing: `root`, `git_dirs`, `head_sha`, `branch`, `status_entries`, `dirty_paths`, `StatusEntry`, `uncommitted_paths`, `GIT_TIMEOUT_SECONDS` | `local_workspace.py` :124, 140, 329–341, 388–466, 479–588, 1001–1023 | REPLACE | REMOTE: `engine.py` :2344, 2438, 2953, 3869–3874, 13673; moves to a mode-neutral module (C1) |
| The walk, snapshot and fingerprint | `local_workspace.py` :131–327, 619–976 | DELETE | |
| The feature specification and `init_feature_file`; `local_state_dir`, `check_state_dir_location`; `DEFAULT_MAX_*`, `normalize_exclude_pattern` | `local_workspace.py` :135–169, 468–477, 590–617, 978–1253 | DELETE | `init_feature_file` with `local init` (S2) |
| `safefs.py` | whole file | PRESERVE SHARED | `readlink_at` (:490–492) has only the walk as caller: DELETE |
| `KIND_LOCAL`, `LocalCandidate`, `METHOD_IN_PLACE`, the LOCAL record and marker keys, `check_evidence(local=)` | `evidence.py` :112–122, 313–321, 406–431, 594–699, 781–995, 1085–1121, 1538–1548 | DELETE; GENERIC STRICT REFUSAL (the parser's kind check) | D6 |
| `local_certificate`, `_local_binding_problem`, the LOCAL arm of `certificate_problem` | `certificate.py` :271–304, 361–378, 407–441 | DELETE; GENERIC STRICT REFUSAL (the kind check) | `certificate_problem` stays |
| `LaunchClass.LOCAL_AGENT`, `LOCAL_VALIDATION`, `LOCAL_SANDBOX_KEY`, their policy and `template_facts` | `sandbox_policy.py` :18–23, 85–87, 112–113, 289–317, 719–835 | DELETE | `<git dir>/autoforge/sandbox/local` stays on disk for the operator (D10) |
| `mode_strength` and `direction(local=)` | `sandbox_pin.py` :98–123 | PRESERVE SHARED (simplified) | D7 |
| `standalone_hold`'s LOCAL skip | `ownership.py` :227 | DELETE (S2) | D4: a standalone state that does not load holds nothing and is reported (:196–231) |
| The namespace refusal of a non-REMOTE state | `scheduler_state.py` :1460–1461 | PRESERVE SHARED | Unreachable for LOCAL once the loader refuses it: `run_namespace.py` :632–644 classifies the file as one that cannot be read, and both cases are uncertain (`scheduler_state.py` :1527–1551) |
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
  (`engine.py` :1514). The `local:` key cannot go before the reader is
  split out (S1a).
- **C2.** Every REMOTE state file carries the twelve LOCAL fields, and an
  unknown field is refused (`state.py` :1086–1100). D8.
- **C3.** `status --json` of a REMOTE run prints those fields
  (`cli.py` :2038–2041 promises an additive shape). D8, D10.
- **C4.** `_bind_existing_run` probes LOCAL's location on every REMOTE
  `status`, `step`, `resume` and `unblock` without `--state-dir`, whatever
  `state_dir` is configured to, and can refuse two runs. D3 deletes it.
- **C5.** `preferred` has a fallback only in LOCAL. D7.
- **C6.** The scheduler's reverse guard skips a LOCAL state. After S2 the
  file does not load, and the guard's generic branch holds nothing and
  reports it. D4.
- **C7.** A LOCAL `status` passes the contract gate today, and fails when
  the configuration drifted. After S2 the loader refuses the file first.
- **C8.** REMOTE relaunches a transient provider failure; LOCAL does not
  (`engine.py` :13450). Deleting the arm changes nothing for REMOTE.

### A.2 Tests

Counts are of test functions, not collected items (Appendix B has those):
definitions matching `^def test_`, `^    def test_` or `^async def test_`
in the 84 `tests/test_*.py` modules. At `02ce244` there are 4,274; 389 are
in scope. #405 added two of them since `4b03151`, one DELETE
(`test_local.py`) and one PRESERVE edited (`test_prompts.py`).

| Class | Functions | What it means |
|---|---|---|
| DELETE | 276 | Tests of LOCAL-only behaviour |
| REPLACE | 46 | Shared coverage that today runs only through LOCAL (A.2.3) |
| GENERIC STRICT REFUSAL | 12 | The earlier revision's LEGACY tests. Each asserted that a LOCAL artifact loads, is shown or is left alone. Each is rewritten in S2 to assert D3's and D4's generic refusal and an untouched file, or deleted where an S1b or S2 test covers the same case |
| PRESERVE SHARED, edited | 55 | 49 drop a LOCAL row or parameter, 6 move out of a LOCAL-only module |
| PRESERVE SHARED, untouched | 3,885 | |

**Which step retires each.** S2 retires 160 of the 389, and S3 all the
rest but two: `test_unknown_mode_fails_loudly`, which S2 extends with a
`"LOCAL"` case, and the `test_run_namespace.py` test S1b keeps. Of S2's
160, 122 are DELETE, 19 REPLACE (B 2, C 2, G 15), 10 GENERIC and 8
PRESERVE edited; one, the correction-fence test, is split, its REMOTE half
moving. A.2.1's last column counts them by module, and the list below names
them.

S2's share was measured, not read off the classes. A prototype of S2's
closures was applied to an export of `02ce244`, deleting no LOCAL code: the
`local` subparser and its dispatch removed; `local` out of the top-level
keys and its block no longer parsed; the loader refusing `mode: "LOCAL"`
with today's unknown-mode message; the probe returning at once; the
scheduler's LOCAL skip removed; README's "Local mode (no GitHub)" section,
`docs/local-mode.md` and the example's `local:` block deleted. The full
suite ran on the export with and without it, on Python 3.14.10 with
`pytest -n 8`, on Appendix B's host, on 2026-10-30.

- **147 functions (211 items) pass without the prototype and fail with
  it**: 114 on the loader's refusal, the rest on the subcommand, the key,
  the probe and README's text. A 148th, the live Pi api-key rehearsal,
  fails on both, because the export is not a git checkout; with the
  prototype it fails earlier, at `local doctor`.
- **2 pass with it only because every LOCAL file is now refused**: the
  fuzz module's illegal-state and random-corruption tests. With the refusal
  changed to raise an exception no test expects, a second run of 25
  modules (every module above with LOCAL state tests, and the replan and
  scheduler-state modules) fails exactly these two. After S2 they assert
  nothing about the loader.
- **2 were skipped**, because that host cannot run the real-backend sandbox
  tests: the two `test_sandbox_routing.py` tests that start a `required`
  LOCAL run through `local run`. argparse refuses them at S2.
- **8 go with the live Pi flow's LOCAL form**: the live test, the six
  rehearsals that stop before the flow reaches LOCAL, and the check that
  names the live test.

The other 229 pass with the prototype. They call LOCAL code directly,
through `make_local_engine`'s state in memory or as units of a module,
never through an entry point S2 closes; `step` works on the state it holds
and does not read it back. A's, E's, F's and H's originals are among them,
so their deletion prerequisites stay with S3 (A.2.3). Two limits: the
prototype is not S2's code, and a test added after `02ce244` is not in the
list. S2's exit tests cover both by requiring the full suite to pass (D12).

<details>
<summary>The 160 functions S2 retires, by module</summary>

Untagged entries are DELETE. `test_state.py`'s nine untagged entries are
seven DELETE and two GENERIC, as A.2.1 counts them.

- `test_cli.py` (3):
  - `test_a_configured_scheduler_state_dir_is_refused_before_the_run_is_looked_for` (GENERIC)
  - `test_an_enforced_run_refuses_a_config_file_in_a_providers_writable_directory` (PRESERVE)
  - `test_local_run_refuses_a_state_dir_inside_the_scheduler_root` (GENERIC)
- `test_config.py` (9):
  - `test_a_non_mapping_local_block_is_refused` (GENERIC)
  - `test_known_key_tables_name_real_fields` (PRESERVE)
  - `test_local_block_rejects_unusable_values`
  - `test_local_exclude_is_normalized_deduplicated_and_sorted`
  - `test_local_validation_commands_have_the_same_bounds`
  - `test_local_validation_commands_stay_argv_arrays`
  - `test_non_mapping_sections_are_rejected_everywhere` (PRESERVE)
  - `test_typos_from_the_issue_are_rejected` (PRESERVE)
  - `test_unknown_keys_are_rejected_in_every_section` (GENERIC)
- `test_doctor.py` (1):
  - `test_local_doctor_checks_a_pi_reviewer`
- `test_durable_run_contract.py` (22):
  - `test_a_changed_snapshot_algorithm_refuses_the_resume`
  - `test_a_configuration_changed_between_invocations_refuses_the_resume`
  - `test_a_contract_rewritten_in_state_json_cannot_launder_a_policy_change`
  - `test_a_correction_diagnostic_cannot_break_out_of_its_block` (split: REMOTE half moves)
  - `test_a_legacy_local_state_without_a_contract_is_refused_not_filled_in` (GENERIC)
  - `test_a_relocated_checkout_refuses_the_resume_on_both_roots`
  - `test_a_state_file_copied_to_another_state_directory_refuses_the_resume`
  - `test_an_exclusion_containing_a_comma_resumes_under_itself_and_refuses_its_split`
  - `test_an_exclusion_that_matches_nothing_is_still_drift`
  - `test_bootstrap_does_not_follow_a_link_at_the_state_directory_or_its_parent` (REPLACE C)
  - `test_control_the_root_verifier_is_what_passes_from_a_subdirectory_too`
  - `test_documented_limit_an_in_place_replacement_with_copied_contents_is_not_detectable` (REPLACE C)
  - `test_every_command_that_binds_a_run_is_refused_by_the_gate` (GENERIC)
  - `test_removing_an_exclusion_is_drift_too`
  - `test_reproduction_a_resume_from_a_subdirectory_still_runs_the_root_verifier`
  - `test_reproduction_an_exclusion_added_after_the_implementation_is_refused`
  - `test_the_cwd_a_run_is_created_from_is_not_recorded_because_nothing_runs_from_it`
  - `test_the_dry_run_plan_names_the_frozen_execution_directory`
  - `test_the_fix_budget_is_the_recorded_one_not_the_current_one`
  - `test_the_policy_change_is_refused_between_review_and_fix_as_well`
  - `test_the_step_budget_is_the_recorded_one_not_the_current_one`
  - `test_the_unchanged_invocation_resumes_and_finishes`
- `test_epic_commands.py` (1):
  - `test_local_mode_is_unaffected_by_scheduler_namespaces` (GENERIC)
- `test_interaction.py` (1):
  - `test_a_local_interaction_failure_keeps_the_launch_charge`
- `test_live_pi.py` (1):
  - `test_one_local_review_runs_on_the_operators_real_pi` (REPLACE G)
- `test_live_pi_support.py` (15):
  - `test_a_credential_of_the_other_type_fails_its_route` (REPLACE G)
  - `test_a_forward_naming_an_unset_key_fails_step_2` (REPLACE G)
  - `test_a_key_forwarded_on_purpose_is_named_reaches_the_launch_and_is_scanned_for` (REPLACE G)
  - `test_a_missing_pi_fails_the_preconditions_and_launches_nothing` (REPLACE G)
  - `test_a_model_error_on_the_api_key_route_is_the_providers_refusal` (REPLACE G)
  - `test_a_model_pi_does_not_resolve_fails_the_preconditions` (REPLACE G)
  - `test_a_new_session_entry_fails_the_side_effect_step` (REPLACE G)
  - `test_a_provider_key_fails_step_2_is_not_unset_and_stops_before_any_launch` (REPLACE G)
  - `test_a_provider_refusal_is_named_and_the_later_checks_still_run` (REPLACE G)
  - `test_a_review_answer_without_a_valid_control_result_fails_step_4` (REPLACE G)
  - `test_a_review_that_writes_into_the_tree_fails_twice_over` (REPLACE G)
  - `test_an_auth_check_that_does_not_allow_the_api_key_route_stops_before_any_launch` (REPLACE G)
  - `test_the_live_test_is_deselected_by_default_and_skips_without_its_variable` (PRESERVE)
  - `test_the_smoke_flow_passes_on_the_api_key_route_against_a_fake_pi` (REPLACE G)
  - `test_the_smoke_flow_passes_on_the_optional_oauth_route_against_a_fake_pi` (REPLACE G)
- `test_local.py` (41):
  - `test_a_checkpoint_from_another_phase_is_not_closed_by_a_clean_review`
  - `test_a_correction_refused_at_its_template_is_not_charged_as_a_launch`
  - `test_a_crash_between_corrections_resumes_under_the_remaining_bound`
  - `test_a_failed_fix_validation_does_not_charge_a_fix_round`
  - `test_a_failed_phase_is_re_invoked_against_its_original_baseline`
  - `test_a_journal_refused_before_the_launch_is_not_charged_as_a_launch`
  - `test_a_local_agent_failure_names_its_log_and_resume_command_but_no_worktree`
  - `test_a_local_agent_message_is_bounded_before_it_reaches_state`
  - `test_a_local_state_cannot_hold_a_github_only_phase`
  - `test_a_local_transient_failure_is_not_relaunched`
  - `test_a_malformed_result_is_not_corrected_while_a_process_its_launch_started_may_run`
  - `test_a_planted_step_name_is_refused_before_the_launch_not_after_the_agent`
  - `test_a_pre_launch_refusal_on_a_resumed_entry_keeps_the_earlier_charge`
  - `test_a_profile_refused_before_the_launch_is_not_charged_as_a_launch`
  - `test_a_resume_cannot_hide_the_implementation_behind_a_new_exclusion`
  - `test_a_resume_that_widens_the_cost_bounds_is_refused_too`
  - `test_a_reviewer_that_switches_branches_is_not_corrected`
  - `test_a_run_directory_refused_before_the_launch_is_not_charged_as_a_launch`
  - `test_a_run_directory_that_cannot_be_published_into_is_refused_before_the_launch`
  - `test_a_state_file_with_a_malformed_pending_checkpoint_is_refused`
  - `test_a_state_file_with_an_unknown_field_is_corruption` (REPLACE B)
  - `test_a_template_refused_before_the_launch_is_not_charged_as_a_launch`
  - `test_an_anchor_a_launch_moved_blocks_before_its_correction`
  - `test_an_oversized_unresolved_rationale_never_reaches_the_block_reason`
  - `test_cli_local_init_and_status`
  - `test_cli_local_init_refuses_while_another_controller_holds_the_lock`
  - `test_cli_local_run_dry_run_writes_nothing`
  - `test_cli_local_run_refuses_a_dirty_tree`
  - `test_cli_local_status_describes_a_journal_the_state_carries`
  - `test_cli_local_status_hides_github_fields`
  - `test_cli_local_status_redacts_the_block_reason`
  - `test_correction_retries_are_charged_against_the_same_durable_bound`
  - `test_fifty_unresolved_rationales_at_the_bound_produce_a_bounded_block_reason`
  - `test_local_doctor_checks_only_the_reachable_providers`
  - `test_local_doctor_requires_only_the_reachable_reviewer_profiles`
  - `test_local_run_is_resumable_from_persisted_state`
  - `test_pi_correction_retries_are_charged_as_scripted_ones_are`
  - `test_the_existing_run_guard_names_the_subcommand_that_was_typed`
  - `test_the_new_run_an_anchor_refusal_prints_continues_with_the_flags_given`
  - `test_the_readme_no_github_promise_is_the_controllers_not_the_runs`
  - `test_the_recorded_policy_is_canonical_not_the_operators_spelling`
- `test_local_recovery_matrix.py` (22):
  - `test_a_crash_after_the_agents_side_effect_does_not_demand_the_work_twice`
  - `test_a_crash_after_the_transition_is_persisted_does_not_repeat_the_phase`
  - `test_a_crash_after_validation_but_before_the_transition_is_persisted`
  - `test_a_crash_at_validation_keeps_the_phase_unverified`
  - `test_a_crash_before_a_clean_review_is_persisted_re_reviews`
  - `test_a_crash_before_a_findings_review_is_persisted_does_not_charge_a_round`
  - `test_a_crash_before_the_agent_ran_leaves_the_phase_to_be_done`
  - `test_a_crash_on_the_launch_charge_is_a_launch_that_never_happened`
  - `test_a_crash_window_used_to_commit_blocks_rather_than_resuming`
  - `test_a_fix_round_is_not_charged_twice_by_a_crash`
  - `test_a_foreign_file_the_agent_renamed_over_the_journal_receives_nothing`
  - `test_a_journal_enlarged_by_the_agent_refuses_the_post_run_append_bounded`
  - `test_a_reviewer_that_edits_the_tree_and_crashes_cannot_have_its_edit_reviewed`
  - `test_a_reviewer_that_edits_the_tree_and_returns_is_refused_and_stays_refused`
  - `test_a_timed_out_agent_that_enlarged_the_journal_keeps_its_checkpoint`
  - `test_an_operator_edit_between_a_verified_phase_and_review_blocks_too`
  - `test_an_oversized_event_journal_left_by_an_agent_is_refused_not_materialised`
  - `test_every_crash_point_leaves_state_that_loads`
  - `test_the_dry_run_plan_for_review_reports_an_unverified_tree`
  - `test_the_implementation_file_the_dead_agent_wrote_is_never_reverted`
  - `test_the_tree_moving_while_the_controller_was_dead_blocks_the_review`
  - `test_validation_command_output_is_part_of_the_verified_tree`
- `test_local_state_fuzz.py` (4):
  - `test_a_round_trip_through_disk_preserves_every_field`
  - `test_an_illegal_local_state_never_loads`
  - `test_random_corruption_either_fails_or_is_legal` (REPLACE B)
  - `test_the_baseline_state_loads`
- `test_merge_certificate.py` (2):
  - `test_local_a_certificate_names_only_the_workspace_its_round_reviewed`
  - `test_local_fingerprint_drift_before_review_certifies_nothing`
- `test_ownership.py` (1):
  - `test_what_holds_nothing` (GENERIC)
- `test_review_certificate.py` (4):
  - `test_local_a_crash_before_the_done_write_leaves_no_certificate`
  - `test_local_a_fix_round_certifies_the_new_workspace_only`
  - `test_local_done_carries_a_certificate_of_the_reviewed_workspace`
  - `test_local_done_is_never_entered_without_an_applicable_certificate`
- `test_sandbox_pin.py` (1):
  - `test_a_local_run_refuses_a_weaker_mode_unless_accepted`
- `test_sandbox_routing.py` (2):
  - `test_a_required_local_run_with_its_config_file_in_claudes_directory_launches_nothing`
  - `test_a_required_local_run_with_its_config_file_in_the_root_launches_nothing`
- `test_state.py` (12):
  - `test_a_legacy_local_state_gets_no_launch_label`
  - `test_a_local_run_carries_no_acceptance_state`
  - `test_a_local_run_carries_no_operator_state`
  - `test_a_local_run_carrying_a_recorded_configuration_is_refused`
  - `test_a_local_run_carrying_any_effect_state_is_refused`
  - `test_a_local_run_refuses_a_remote_record`
  - `test_a_local_run_with_empty_effect_fields_loads`
  - `test_a_malformed_sandbox_pin_is_refused_never_read_as_unpinned` (PRESERVE)
  - `test_a_protocol_10_local_file_loads_and_is_relabelled`
  - `test_a_protocol_11_file_loads_unpinned_and_is_relabelled` (PRESERVE)
  - `test_a_protocol_7_local_file_missing_an_effect_field_is_refused`
  - `test_a_sandbox_pin_round_trips_under_the_current_protocol` (PRESERVE)
- `test_validation_evidence.py` (8):
  - `test_a_changed_local_validation_command_is_refused_before_anything_runs`
  - `test_a_failing_local_validation_is_a_fail_record_and_the_message_is_unchanged`
  - `test_a_failing_local_validation_whose_tree_cannot_be_walked_keeps_its_message`
  - `test_a_local_contract_beyond_the_bounds_blocks_before_the_agent`
  - `test_a_local_validation_leaves_records_with_start_and_end_fingerprints`
  - `test_a_marker_is_recorded_before_a_plan_that_refuses_the_working_tree`
  - `test_an_interrupted_local_validation_is_recorded_on_resume_and_rerun`
  - `test_the_local_review_binding_passes_with_validation_records`
- `test_validation_gates.py` (10):
  - `test_local_a_changed_contract_at_review_entry_is_refused_before_anything_runs`
  - `test_local_a_failure_is_final_only_for_the_tree_it_ran_on`
  - `test_local_a_fix_binds_the_next_review_to_new_evidence`
  - `test_local_a_legacy_review_with_no_evidence_blocks_without_validating`
  - `test_local_a_non_idempotent_validator_is_not_verified_then_blocks`
  - `test_local_a_validator_writing_an_excluded_path_converges_without_a_rerun`
  - `test_local_an_idempotent_generator_converges_with_one_confirming_run`
  - `test_local_an_unchanged_tree_with_a_failure_is_not_validated_again`
  - `test_local_review_needs_evidence_for_the_bound_fingerprint_itself`
  - `test_local_runs_make_no_github_call`

</details>

#### A.2.1 By module

| Module | Functions | In scope | Class | Retired in S2 |
|---|---|---|---|---|
| `test_local.py` | 117 | 117 | DELETE 107; REPLACE 8 (A.2.3 A, B, H); move 1 (`test_saved_state_without_a_mode_field_loads_as_remote`); GENERIC 1 (`test_unknown_mode_fails_loudly`, which gains a `"LOCAL"` case) | 41: DELETE 40, REPLACE 1 (B). The GENERIC test gains its case |
| `test_local_recovery_matrix.py` | 24 | 24 | DELETE (journal cases covered in `test_runlog.py` and REMOTE `test_engine.py`) | 22 |
| `test_local_red_team.py` | 21 | 21 | DELETE (the artifact-name case is covered in `test_runlog.py`; the state-directory replacement partly in `test_state.py`, the rest in A.2.3 C) | None |
| `test_local_state_fuzz.py` | 6 | 6 | DELETE 4; REPLACE 2 (A.2.3 B) | 4: DELETE 3, REPLACE 1 (B's random-corruption test) |
| `test_local_workspace_matrix.py` | 9 | 9 | DELETE | None |
| `test_durable_run_contract.py` | 43 | 43 | DELETE 30; REPLACE 6 (A.2.3 C, D); move 5 (two AST guards on state access, the `link(2)` publication test, the REMOTE worktree test, the REMOTE half of a correction-fence test); GENERIC 2 | 22: DELETE 17, REPLACE 2 (C), GENERIC 2, and the correction-fence test, split |
| `test_live_pi.py`, `test_live_pi_support.py` | 1, 28 | 1, 15 | REPLACE 15 (A.2.3 G); 13 checks of the support module are mode-free | All 16: the LOCAL flow goes in S2 |
| `test_doctor_sandbox.py` | 18 | 14 | REPLACE 12 (A.2.3 E); DELETE 1; PRESERVE 1 | None |
| `test_sandbox_routing.py` | 21 | 8 | DELETE 6; REPLACE 2 (A.2.3 E, F) | 2: DELETE (the two started through `local run`) |
| `test_sandbox_policy.py` | 54 | 3 | DELETE 2; REPLACE 1 (A.2.3 F) | None |
| `test_state.py` | 160 | 12 | DELETE 7; GENERIC 2; PRESERVE 3 (drop the `local` id) | All 12 |
| `test_config.py` | 142 | 14 | DELETE 6; PRESERVE 6; GENERIC 2 (the `local` row of the unknown-key test, now expecting a refusal; the non-mapping `local:` block) | 9: DELETE 4, PRESERVE 3, GENERIC 2 |
| `test_certificate.py`, `test_validation_evidence.py`, `test_validation_gates.py`, `test_review_certificate.py`, `test_merge_certificate.py` | 40, 37, 44, 20, 29 | 9, 9, 10, 4, 2 | DELETE (each has REMOTE sections) | 0, 8, 10, 4, 2 |
| `test_evidence.py`, `test_result_parser.py`, `test_transitions.py` | 77, 134, 19 | 10, 15, 5 | DELETE 5, 4, 2; PRESERVE the rest (drop LOCAL parametrizations) | None |
| `test_sandbox_gate.py`, `test_sandbox_pin.py`, `test_config_fingerprint.py` | 12, 15, 35 | 5, 4, 2 | DELETE 5, 2, 2; PRESERVE 2 in `test_sandbox_pin.py` | 1 DELETE in `test_sandbox_pin.py` |
| `test_cli.py`, `test_ownership.py`, `test_epic_commands.py`, `test_run_namespace.py` | 119, 16, 32, 45 | 3, 1, 1, 1 | GENERIC 2, 1, 1, 1 (`test_run_namespace.py`'s is `test_legacy_standalone_state_is_never_read_moved_or_modified`, which S1b keeps); PRESERVE 1 in `test_cli.py` | All but `test_run_namespace.py`'s: 3, 1, 1 |
| `test_doctor.py`, `test_prompts.py`, `test_routing.py`, `test_sandbox_parity.py` | 122, 53, 5, 1 | 4, 12, 1, 1 | DELETE 2 in `test_doctor.py`; PRESERVE the rest (drop the LOCAL row, template or profile) | 1 DELETE in `test_doctor.py` |
| `test_sdk_policy.py`, `test_interaction.py`, `test_unblock.py` | 46, 73, 39 | 1 each | DELETE | `test_interaction.py`'s |
| `test_safefs.py` | 49 | 0 | PRESERVE. Introduced with LOCAL, uses none of it | |
| `test_engine.py`, `test_lock_domains.py` | 557, 39 | 0 | PRESERVE; they import `StatusEntry` and `GIT_TIMEOUT_SECONDS` from `local_workspace` (C1) | |
| The other 46 modules | 1,972 | 0 | PRESERVE | |

#### A.2.2 Fixtures and helpers

- `tests/conftest.py` :2280–2420 is LOCAL-only: `ExplodingGitHub`,
  `FEATURE_MD`, `sample_contract`, `write_feature` and `make_local_engine`,
  which 14 modules use.
- **`test_local.py` is a helper library.** Nine modules import its helpers
  at module level (`local_repo`, `impl_result`, `review_result`,
  `fix_result`, `finding`, `scripted`, `touch_impl`, `IMPL_FILE`) and three
  more inside functions. Deleting it first breaks their collection, so S1a
  moves what the survivors import.
- `tests/live_pi.py`'s `run_smoke` is LOCAL (`local doctor`, a `local:`
  configuration at :466, `new_local_run` at :673). Its credential scan,
  tree, process and session helpers are not. `tests/pi_fake.py` is shared.
- `tests/dist_check.py` runs a LOCAL dry run (`local init`, `local run
  --dry-run --full-prompt`, :461–483) beside its REMOTE one, and renders
  the LOCAL templates. It gates every release (`pypi-publish.yml` runs it
  with `--release-tag`) and CI's `dist` job. The REMOTE dry run already
  exists, so this is PRESERVE with the LOCAL half dropped in S2.

#### A.2.3 What a naive deletion would lose (the S1a checklist)

- **A. The hardened git reads REMOTE uses** (5, `test_local.py`): no
  optional index lock, no fsmonitor and no credential; `uncommitted_paths`
  as one hardened read; status records with renames and copies; the
  refusal of truncated output. REMOTE has only end-to-end tests of the
  rendered note (`test_engine.py`). They move with the git plumbing (C1).
- **B. The state loader** (3): the unknown-field refusal (`test_local.py`),
  the only fuzz test of the loader and the non-object JSON roots
  (`test_local_state_fuzz.py`). They become REMOTE state tests. S2 retires
  the unknown-field and random-corruption originals (A.2), so these
  replacements pass on `main` before S2.
- **C. The state root under a running engine** (5,
  `test_durable_run_contract.py`): a state directory replaced mid-run, a
  `state.json` replaced by a symbolic link or hard-linked elsewhere, the
  documented undetectable in-place copy, a link at the state directory or
  its parent at bootstrap. `test_state.py` covers only rename-aside and a
  symbolic link at `state_root()`. S2 retires the bootstrap and in-place
  copy originals, which load a LOCAL state from disk (A.2), so these
  replacements pass on `main` before S2.
- **D. Findings fencing** (1): finding text cannot break out of the
  findings block, which `_format_findings` renders for REMOTE `FIX` and
  `REVIEW` too.
- **E. `doctor`'s sandbox rows** (13): every row is tested with
  `local=True`: capability, providers' `--version` clip, declarations,
  linked configuration directories, shebangs through links, hard links,
  sandbox directories, operator paths over denied roots, outside a
  repository. REMOTE has one test that checks two row names.
- **F. Launch classes** (2): the declaration-writability test runs on LOCAL
  facts (a REMOTE helper exists in the same file); the real-backend
  demonstration that a provider can rewrite a `--config` file in its
  writable directory runs on `LOCAL_AGENT`.
- **G. The live Pi smoke test** (15): the only live model test runs one
  LOCAL REVIEW, and its 14 offline rehearsals follow the same flow. No live
  REMOTE flow exists; offline REMOTE Pi coverage does
  (`test_a_pi_phase_runs_end_to_end_over_rpc`,
  `test_an_all_pi_run_gets_the_same_isolation_in_every_child`). A REMOTE
  form needs a GitHub stand-in, the in-memory fake or a scratch repository,
  which is S1a's design question. The flow writes a `local:` block
  (`tests/live_pi.py` :466) and runs `autoforge local doctor` (:584–600),
  and the 14 rehearsals run in CI. So S2's removal of the key and the
  subcommand cannot land before G.
- **H. The credential preflight of a validation command** (2). These are
  the only tests that plant a credential source after an agent's launch
  was checked and before a validation command runs. The REMOTE pre-merge
  verification's tests (`test_engine.py` :14007, :14026) cover another
  launch and do not count. Each case below keeps its LOCAL test until its
  REMOTE test is on `main` and passing.

  | | H-A. A credential helper | H-B. A login-shell startup file |
  |---|---|---|
  | LOCAL test | `test_a_credential_helper_planted_before_validation_leaves_the_phase_unverified` (`tests/test_local.py` :3884) | `test_a_startup_file_planted_before_validation_leaves_the_phase_unverified` (`tests/test_local.py` :3907) |
  | What the agent plants, during its own launch | `git config --local credential.helper store` in the working tree | A `~/.profile` that exports `GITHUB_TOKEN` |
  | What LOCAL does | The command's preflight, run in the working tree, reads repository scope and refuses: `VerificationError` "validation commands not run … credential helper". The phase stays `ANALYZE_EXECUTE`, and the command's marker file is never created | The preflight's login-shell probe reports `GITHUB_TOKEN` changed and refuses in the same way, with the same result |
  | Shared invariant | No validation command is launched where the git configuration it reads would hand it a credential. When the preflight finds one, nothing is launched, no record is written and the candidate stays unverified | No validation command is launched when a login shell it may start would change its isolation variables. Same consequences |
  | What REMOTE does at `02ce244` | Each command runs in a fresh `tempfile.mkdtemp` export with no git dir (`validate_remote_candidate`, `engine.py` :6043; `materialize.py` :696–706). Its preflight reads the configuration git reads there (`child_env.preflight`, `read_git_config(cwd)`). A helper planted in the issue worktree's repository configuration is therefore not seen by that preflight and not read by the command's git, and the controller's push reads no repository configuration (`git_transport.py`, ADR 0004 D7.1). The next agent launch in the worktree refuses the helper (`test_a_credential_helper_in_the_repository_refuses_the_launch_and_leaves_state`, `test_engine.py` :13875). A git credential source the export does reach, a GitHub entry in `~/.netrc`, is refused by the same preflight | The login-shell probe reads `HOME`, not the working directory, so the same preflight before each REMOTE command (`engine.py` :6261–6270) refuses. A refusal is re-checked once and becomes `ValidationInconclusiveError`: `BLOCKED`, no record, "Nothing was pushed or published" (:6429–6434) |
  | REMOTE test to add | Two tests, in `tests/test_validation_evidence.py` through `validate_remote_candidate` and in `tests/test_engine.py` beside the pre-merge preflight tests. (i) A helper planted in the common dir's `config` and in the worktree's `config.worktree`, between a producer's launch and its candidate validation, never reaches the command. (ii) A `~/.netrc` GitHub entry planted the same way is refused | One test, in the same two files: a `~/.profile` that exports `GITHUB_TOKEN`, planted between a producer's launch and its candidate validation, is refused |
  | Required negative assertion | (i) A probe command records no `credential.helper` from any scope git reads in its directory and no credential variable, and the run stops at the next agent launch's preflight, before that launch starts. (ii) The probe never runs (its record file does not exist), no `ValidationRecord` is written, the state is `BLOCKED` with a reason that names `~/.netrc` but no value, and FakeGitHub records no push and no PR write | The probe never runs, no `ValidationRecord` is written, the state is `BLOCKED` with a reason that names the startup file and the variable but no value, and FakeGitHub records no push and no PR write |
  | Owner | #386 (RET-A3), in S1a. #241's item 3 asks for the same tests; the second to land links the first | Same |
  | Deletion prerequisite for #388 (RET-A5) | Both REMOTE tests on `main` and passing, each failing when its guard is removed (D12, S1a) | The REMOTE test on `main` and passing, failing when the preflight call is removed |

  H-A does not carry over as a refusal. REMOTE prevents the unsafe launch
  by where it runs the command, not by refusing it, so its test asserts the
  helper's absence where LOCAL's asserts a refusal. REMOTE could also
  refuse a candidate whose worktree configuration gained a helper, though
  the export cannot read it. That would be a new refusal and a behaviour
  change, for #386 or #241 to propose with its own review. This record does
  not make it, and H-A's deletion prerequisite does not depend on it.

#### A.2.4 Shared safety and its preservation owner

Each shared invariant a LOCAL test protects today has an owner, who
protects it another way before the step that retires that test (S2 or S3,
A.2).

| Invariant | Protected only through LOCAL today | Owner and prerequisite |
|---|---|---|
| A credential helper never reaches a validation command | H-A | #386 (S1a), the mutation-checked REMOTE test |
| A login-shell startup file never changes a validation command's isolation | H-B | #386 (S1a), the same |
| Git reads take no optional lock, no fsmonitor and no credential | A | #386: the tests move with the reader (C1) |
| The state loader refuses unknown fields and non-object roots, and survives fuzzing | B | #386: REMOTE state tests, passing before S2 |
| The state root holds under a running engine (replaced directory, links, bootstrap) | C | #386: REMOTE or neutral tests, passing before S2 |
| Finding text cannot break out of its block | D | #386 |
| `doctor`'s sandbox rows are right | E | #386: REMOTE rows |
| Launch-class declarations stay read-only, and a provider cannot rewrite its `--config` | F | #386, on a REMOTE class; the real-backend test runs where a host can enforce |
| A live model run through the real CLI | G | #386: the REMOTE flow and its offline rehearsals, passing before S2 |
| Sandbox pinning and enforcement | `test_sandbox_pin.py`'s and `test_sandbox_gate.py`'s LOCAL rows | PRESERVE: their REMOTE rows stay. Apart from E and F above, no REMOTE sandbox test depends on LOCAL |
| Candidate validation, evidence and certificate binding | None: each module has REMOTE sections | PRESERVE. S3 adds the refusal of LOCAL kinds in a REMOTE file |
| Crash recovery | `test_local_recovery_matrix.py`'s journal cases | PRESERVE: `test_runlog.py` and REMOTE `test_engine.py` cover them; #386 confirms each case before S2, which retires 22 of the 24 |
| Provider interaction and launch accounting | LOCAL's three-launch bound and `LocalBinding` | Nothing shared: REMOTE's bounds have their own tests, and #155's REMOTE forms (§5.1) |
| Scheduler state ownership | The LOCAL skip and the LOCAL namespace mismatch | #385 (S1b): the generic tests of D4 |
| REMOTE state refusals under the drop step | None, but the drop step could weaken them | #385 (S1b) pins them; #388 (S3) keeps them |
| The release gate's packaging check | `dist_check.py`'s LOCAL half | #387 (S2) keeps the REMOTE half. #388 and #389 check that the packages carry no LOCAL template |

#### A.2.5 Test contracts to rewrite

`tests/AGENTS.md` and `docs/agent-guides/testing.md` name LOCAL coverage
as required. In `tests/AGENTS.md` that is the LOCAL fixtures, the
`test_local*.py` and `test_durable_run_contract.py` rows, "REMOTE and
LOCAL" in the sandbox-pin, evidence, gate and certificate rows, and the
live Pi row. In `testing.md` it is the state, CONTROL_RESULT, execution,
outer sandbox and launch routing, credential, parity, live Pi, SDK,
interaction and epic requirements.

A requirement leaves the contracts in the step that retires its last test,
so that they never require coverage the suite no longer has. One that S2
empties only in part is narrowed in S2 to what is left.

- **S2** removes what its retirements empty (A.2). In `tests/AGENTS.md`:
  the gate row's "LOCAL convergence", the certificate row's "LOCAL `DONE`
  writes", "LOCAL" among the epic commands' cases, and the live Pi row's
  LOCAL flow. In `testing.md`: the effect, acceptance, operator-action and
  recorded-configuration requirements on a LOCAL state; LOCAL validation
  records and `local.validation_commands`; the gates' LOCAL convergence;
  LOCAL `DONE`'s certificate and LOCAL fingerprint drift at the merge gate;
  `local run`'s refusal of a state directory inside the scheduler root and
  "LOCAL unaffected" among the standalone guards, both now D4's refusal;
  the interaction requirement's LOCAL launch charge; `local doctor`; and
  the live Pi flow. S2 adds D4's and D5's generic refusals as required
  coverage, in the PR that brings them: a `mode: "LOCAL"` state refused
  with nothing written, a `local:` key refused, `autoforge local` rejected,
  and no probe of LOCAL's location.
- **S3** removes the rest as it deletes the code those tests call: the
  LOCAL fixtures, the `test_local*.py` and `test_durable_run_contract.py`
  rows, and "REMOTE and LOCAL" in the sandbox-pin and evidence rows of
  `tests/AGENTS.md`; in `testing.md`, the CONTROL_RESULT requirements (the
  LOCAL refusals of `acceptance_action` and `operator_acceptance` among
  them), execution, outer sandbox and launch routing, credential (H), Pi
  parity, SDK, and the certificate's LOCAL fingerprint and contract fields.

### A.3 Documents, configuration, CI and release artifacts

DELETE goes in the step named; REWRITE edits shared text to REMOTE only;
HISTORY stays as written. `CLAUDE.md`, `SECURITY.md`, `pyproject.toml`,
`MANIFEST.in`, `pypi-publish.yml`, `docs/releasing.md` and
`docs/non-code-rules.md` mention no LOCAL mode. Line numbers are given only
for documents #405 did not change.

| Document | DELETE | REWRITE | HISTORY |
|---|---|---|---|
| `README.md` (S2) | "Local mode (no GitHub)" (:359–415), with its anchor; the GitHub-free sentences (:61–64, 177–184) and the absolute anchor link (:204); the "work without GitHub" row (:426) | state location (:47–49); key properties (:100–101, 123–124, 148–156); prerequisites (:200–224); installation (:285–287); maturity: LOCAL under "Supported now" (:442–451), LOCAL validation and `DONE` (:468–489), Pi's evidence (:524–526), the `0.1.0a0` note (:528–531) | |
| `docs/local-mode.md` (S2) | the whole guide | | |
| `docs/usage.md` (S2) | "LOCAL runs show the same block"; the LOCAL sandbox cache line | intro; the `BLOCKED`, acceptance, standalone, watching and namespace passages; the outer sandbox (`preferred`, `local doctor`); troubleshooting; recorded configuration and evidence | |
| `docs/configuration.md` (S2) | "Local mode configuration" (:1215–1235); the `local` row (:207) | `prompt_version` and the contract (:160–188); `local doctor`; the pin, validation commands, `preferred`, launch classes, LOCAL workspaces and caches (about fifteen passages) | |
| `docs/security.md`, `docs/compatibility.md`, `docs/development.md` (S2) | | LOCAL validation commands, the LOCAL root and pin order; "LOCAL needs no `gh`", `preferred` in LOCAL, the LOCAL state directory; `dist_check.py`'s step 6 and the live Pi test | |
| `docs/pi.md`, `docs/pi-policy.md`, `docs/sdk-policy.md` (S2) | "LOCAL mode" in `pi.md` (:312–325) | the live smoke test (`pi.md` :407–445); the tree-drift check and LOCAL REVIEW test (`pi-policy.md`); the LOCAL sandbox root and the fingerprint argument (`sdk-policy.md` :236–243) | |
| `docs/README.md` (S2, S3) | the Local mode row (:16) | the Pi and configuration rows; the ADR 0001 and 0006 rows (:57, 62), when D11's notes land | |
| `docs/agent-guides/architecture.md` (S3) | LOCAL templates (:972–974) | the resolver, launch routing, denied roots, refusal paths | |
| `docs/agent-guides/control-result-protocol.md` (S3) | the LOCAL REVIEW and FIX protocol | "REMOTE and LOCAL alike" and the other LOCAL asides | |
| `docs/agent-guides/workflow.md` (S3) | the LOCAL gate bullet and the three-launch bound | the LOCAL topology, `REVIEW → DONE`, the unblock edge, certificate, pin, relaunch | |
| `docs/agent-guides/state-and-recovery.md` (S3) | | persistent state, validation evidence, locking, runtime artifacts, namespaces, error handling; N + 1's migration rule added | the protocol history (:482) |
| `docs/agent-guides/github-safety.md`, `testing.md`, `replan-transaction.md`, `secrets-and-logging.md` (S3; `testing.md` in S2 and S3, A.2.5) | "LOCAL prompts" (`github-safety.md` :1265–1266); "LOCAL on Pi" parity (`testing.md`) | the rest (A.2.5 for `testing.md`) | |
| `AGENTS.md`, `src/autoforge/AGENTS.md`, `tests/AGENTS.md` (S3; `tests/AGENTS.md` in S2 and S3, A.2.5) | the `local-mode.md` map line; `run_contract.py`, `local_workspace.py`; the LOCAL fixtures and test rows; this record's LOCAL routing rule | `preferred` in the isolation terms (:56–66); the ADR map line; the module map; the LOCAL production rule (:184–189), keeping its `safefs.py` pointer to ADR 0001 §5.4 | |
| `autoforge.example.yaml` (S2) | the `local:` block (:506–598) | `execution`, `scheduler` and `sandbox` comments | |
| `CHANGELOG.md` (S2, S3) | | | the LOCAL lines of #395, #302, #280, #256 and the Pi entry; the removal entry (D10) is new |
| `.github/workflows/ci.yml` (S2) | | the `dist` job's comment (:109–121), which already omits the REMOTE dry run | |

### A.4 ADR decisions that are LOCAL's

| Record | LOCAL decisions | At removal (D11) |
|---|---|---|
| ADR 0001 | the whole record | superseded, except §5.4, the run-log and state-file bounds of §5.5, and §8.8 and §8.10, which describe `safefs.py` for REMOTE |
| ADR 0002 | none normative (§3, §4, §4c name LOCAL as an example) | no note |
| ADR 0003 | §2.4 (Pi corrections under the LOCAL launch charge), §3 | note |
| ADR 0004 | §2.1 (LOCAL prompts out of scope), K10's LOCAL refusal, §2.13 (a LOCAL state carrying effect state is corrupt) | note; §2.13's refusal is replaced by D4's mode refusal |
| ADR 0005 | §1.3 and §2.3 to §2.6 (the LOCAL launch classes, the resource table's LOCAL column, the shared cache), §2.8, §2.9, D12.2 (`preferred`'s fallback), D12.3, D12.4, §2.13 to §2.15 | superseded in part; D12.2 by D7 |
| ADR 0006 | D1.2, D2.6 (`LocalCandidate`), D3.2, D3.5, D5.6, D5.7, D7.1 to D7.3 (the in-place convergence rule), the LOCAL record fields, D12.1, D12.5, D12.6, D14.1, D14.2 | superseded in part; D2.7's kind check stays |
| ADR 0007 | §1.1, §2.5 (the LOCAL interaction identity), §2.7, D8.2, D8.5 | unimplemented: dropped by the child that implements it |
| ADR 0008 | D17.5 (LOCAL excluded from scheduler runs by L0) and mentions in §1.1, §2.5, §2.12, §3, §4 | note; D4's generic rule for a standalone state that does not load keeps D17.5's effect |

### A.5 Found on the way, not decided here

- `safefs.py` is described as the boundary for "every controller write"
  (ADR 0001 §5.4, `docs/local-mode.md`), but controller writes in
  `git_transport.py`, `child_env.py`, `github.py`, `providers.py`,
  `materialize.py` and `locking.py` do not go through it. Whether the
  wording or the code should change is a separate question.
- An enforced REMOTE launch cannot see a LOCAL run's default state
  directory, by reading, not by test. `<git common dir>/autoforge` is among
  REMOTE's denied roots (`engine.py` :2884–2898). The policy masks
  `<common>/autoforge` and `<common>/worktrees`, which hold every LOCAL
  default state directory (`sandbox_policy.py` :277). Under
  `sandbox.mode: disabled` there is no such boundary for any file. After
  the removal the directory is inert data.

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
8.3% of `tmp_path` entries bound what deleting them removes, but S1a, S1b
and S2 add REMOTE and refusal tests in their place, and B.4 shows the
host's load alone moving a run by 54 s. Whatever S4 measures is reported
against these numbers as measured, with its own spread.

## Appendix C. After the removal (S4, #389)

S4's record, on 2026-10-31, with its regression run again on 2026-11-01
after PR #425's first review: the suite measured again at `4b03151`
(Appendix B's commit, before any of the removal) and at `3eb68ac`, S3's
last commit and `main` when S4 began, back to back on one host (C.1 to
C.4); the regression on the final candidate (C.5); the tests that hold
each invariant of A.2.4 (C.6); the installed wheel and sdist (C.7); and the
documentation audit (C.8). C.9 says what this appendix does not show. It
authorizes no release and is not a Go/No-Go.

### C.1 Host and conditions

A laptop, not Appendix B's host, so no absolute number below compares with
B's: an Intel i5-4300U (2 cores, 4 threads at 1.9 GHz, the `schedutil`
governor), 7.6 GiB of memory, Fedora Linux 44 Server, Linux 7.2.13, Git
2.56.0, uv 0.13.6, bubblewrap 0.12.0, zsh 5.9. Python 3.14.8 is the
interpreter `.python-version` gives; for CI's 3.12 and 3.11, uv's own
builds of 3.12.13 and 3.11.17 (and, in C.5, of 3.12.16). pytest 9.1.1,
pytest-xdist 3.8.0. `TMPDIR` was a directory of the tmpfs `/tmp`,
as in B.1, for every run of both commits. Each commit ran from a clone of
its own outside the issue worktree, with a venv `uv sync --locked` made.
Besides the agent session that drove the runs and the controller that
started it, the host ran an idle second agent session, an idle OpenCode
server and a tunnel daemon, about 3% of one thread together. The driving
session edited documents throughout, and during the first baseline run it
also ran under `nice` about half a minute of the tests and checks this
step adds; the load average around the runs is in C.3. The CPU ran at 2.6
to 2.9 GHz under load, at 64 °C, with no throttling seen.

### C.2 Commands

B.2's commands, on both commits, with these differences. `make check`'s
pytest is measured by the benchmark instead, which runs the same pytest
with the repository's options, `-p no:cacheprovider` and its own JUnit
report; its other steps are timed apart. The instrumented run of B.4 ran
on four pytest-xdist workers, with the plugin writing one file per worker
(its counts do not depend on the order of the tests; its run is no
timing). `UV_OFFLINE=1` kept uv off the network after the benchmark,
but for the download of C.5's CPython 3.12.16.

```bash
# alternating: before, after, before, after, before, after (R = 1, 2, 3)
uv run python -m tests.parallel_bench --config serial --repeats R --out <dir of the commit>
uv run pytest --collect-only -q
uv run pytest --collect-only -q -m live
make lock-check; make lint; make fmt-check; make typecheck   # each twice: cold, then warm
uv run --isolated --locked --python 3.12 pytest --junitxml=junit-312.xml --durations=0 --durations-min=0.5
PYTHONPATH=<plugin dir> uv run pytest -p af384_instrument -n 4 --dist=load
```

The benchmark resumes a session on the same `--out`, so `--repeats 2`
keeps run 0 and adds run 1: the three rounds alternate the two commits,
and a drift in the host's load lands on both.

### C.3 Results

`make check`'s pytest on Python 3.14.8, as the benchmark ran it, three
times for each commit. A per-test number is that test's median over its
three runs.

| Measure | `4b03151` (before) | `3eb68ac` (after) |
|---|---|---|
| Collected (default selection) | 11,723 items in 83 modules | 12,215 items in 83 modules |
| Outcome, every run | 11,718 passed, 5 skipped, 1 deselected | 12,210 passed, 5 skipped, 1 deselected |
| Wall time of the three runs | 904.1, 899.6, 896.0 s | 836.5, 833.1, 839.4 s |
| Median (range) | 899.6 s (896.0 to 904.1) | 836.5 s (833.1 to 839.4) |
| Time pytest reported | 901.9, 897.5, 893.8 s | 834.2, 830.8, 837.2 s |
| Sum of per-test times | 887.1, 882.8, 879.0 s | 819.5, 816.2, 822.0 s |
| The six LOCAL-only modules | 568 items, 49.7 s (5.6%) | none |
| Items of 0.5 s or more | 360 items, 461.4 s | 315 items, 465.8 s |
| Items under 0.1 s | 9,943 items, 79.6 s | 10,604 items, 83.2 s |
| Load average at each run's start and end | 0.45 to 1.60 | 1.05 to 1.60 |

- **The suite is faster at `3eb68ac`, beyond the spread.** Its median is
  63.1 s (7.0%) under `4b03151`'s, and its slowest run (839.4 s) is
  56.6 s under `4b03151`'s fastest (896.0 s), about seven times the wider
  of the two spreads (8.1 s). Not the removal's alone: the eleven commits'
  (C.9).
- **Not by running fewer tests.** `3eb68ac` runs 492 more. Split by test
  id:

  | Test ids | Items | Before | After |
  |---|---|---|---|
  | In both commits | 10,653 | 790.7 s | 676.2 s |
  | Only before | 1,070 | 90.5 s | |
  | of them in the six LOCAL-only modules | 568 | 49.7 s | |
  | of them in 26 shared modules | 502 | 40.8 s | |
  | Only after | 1,562 | | 142.3 s |
  | of them `test_fast_targets.py` (#342, ADR 0010) | 42 | | 58.4 s |
  | of them the removal's five new modules | 264 | | 6.7 s |
  | of them in shared modules | 1,256 | | 77.2 s |

  The same 10,653 tests take 114.5 s (14.5%) less, more than the whole
  gain: the tests that left took 90.5 s and those that came take 142.3 s.
  The same tests gained most in `test_engine.py` (32.0 s), `test_replan.py`
  (13.0 s), `test_merge_certificate.py` (10.5 s), `test_cli.py` (6.0 s)
  and `test_validation_gates.py` (5.5 s), the modules that start the most
  `git` processes (C.4). These runs cannot say which commit made them
  faster: #341 changed how the executor waits for a child, the tests'
  disk flushes and the fake GitHub's ref reads, and S1a to S3 changed the
  shared fixtures. A hint only: on uv's CPython 3.12.13, which has no
  `os.pidfd_open`, so that #341's pidfd wait falls back to polling (C.5),
  the same tests gained 40.9 s (774.0 to 733.1 s, one run each, with the
  failures C.5 describes), not 114.5 s.
- **What the removal took out and put in.** The six LOCAL-only modules:
  `test_local.py` 135 items and 30.6 s, `test_durable_run_contract.py` 77
  and 8.2 s, `test_local_recovery_matrix.py` 24 and 6.4 s,
  `test_local_workspace_matrix.py` 53 and 2.2 s, `test_local_red_team.py`
  28 and 1.6 s, `test_local_state_fuzz.py` 251 and 0.8 s. Of the 502 ids
  that left shared modules, 208 name LOCAL (13.1 s); the rest are tests
  renamed or reparametrized for REMOTE, whose new ids are among those that
  came (`test_transitions.py`'s `REMOTE-…` cases, `test_doctor_sandbox.py`'s
  `…_in_remote` rows, `test_live_pi_support.py`'s REMOTE flow). The new
  modules: `test_state_fuzz.py` 224 items, in place of
  `test_local_state_fuzz.py`'s 251, `test_state_root.py` 18,
  `test_git_workspace.py` 9 and `test_findings_fence.py` 3 (all S1a), and
  `test_local_removal.py` 10 (S3). Which test now holds each invariant a
  LOCAL test used to hold is C.6, which a test keeps true.
- **The slowest tests** are the same at both but for three:
  `test_local.py::test_failing_validation_command_names_what_it_left_behind`
  (5.75 s) left, and
  `test_fast_targets.py::test_the_selection_is_the_union_of_its_paths`
  (6.77 s) and
  `test_fast_targets.py::test_run_hands_pytest_exactly_the_selection_and_returns_its_status`
  (4.39 s) came. The ten slowest sum to 52.4 s before and 52.2 s after.
- **`-m live` selects one test at both**: `tests/test_live_pi.py`'s LOCAL
  review at `4b03151`, its REMOTE review at `3eb68ac` (S1a).
- **The five skips are the same at both**, all opt-in probes:
  `test_pi_rpc.py`'s run of a real Pi (`AUTOFORGE_PI_E2E_BIN`) and four of
  `test_sdk_policy.py`'s Claude and OpenCode SDK probes. Unlike B.1's host,
  this one can enforce the outer sandbox and has zsh, so the real-backend
  and zsh tests B.3 skipped ran here, and passed, at both commits.
- **The rest of `make check`**, one run each, cold then warm:

  | Step | Before | After |
  |---|---|---|
  | `lock-check` | 0.014 s, 0.014 s | 0.015 s, 0.014 s |
  | `lint` | 0.367 s, 0.026 s | 0.351 s, 0.026 s |
  | `fmt-check` (176 and 173 files) | 0.274 s, 0.027 s | 0.269 s, 0.026 s |
  | `typecheck` (58 and 57 source files) | 7.61 s, 0.22 s | 7.36 s, 0.22 s |

  Under 9 s together at both; one run each says nothing about their
  difference. `make check` is its pytest, as in B.3.
- **Against Appendix B.** B's host is another, faster one: its single
  `make check` at `4b03151` took 602.0 s, against about 900 s here. Only
  the two commits measured on one host compare.
- **On 3.12.** B.2's 3.12 command ran at both commits on uv's CPython
  3.12.13 and took 903.8 s and 918.1 s, but 102 and 98 of its tests failed
  or errored for that interpreter (C.5), so neither number measures its
  commit. The sum of per-test times was 856.6 s and 871.4 s, the LOCAL-only
  modules 52.0 s of the first.

### C.4 Fixtures, subprocesses and files

B.4's plugin at both commits, on four pytest-xdist workers (C.2): its
counts, not its phase times, which four workers sharing four threads make
no timing (the runs took 423.7 s and 526.6 s, the second at a load average
over 4). It counts what B.4's does, and leaves out what B.4 says it leaves
out.

| Measure | `4b03151` (before) | `3eb68ac` (after) |
|---|---|---|
| Processes started through `Popen` | 69,814 | 59,274 |
| of them `git` | 67,555 | 57,052 |
| `git` from the six LOCAL-only modules | 7,310 (10.8%) | none |
| Tests that take a `tmp_path` | 5,903 | 6,326 |
| Entries left in their `tmp_path` | 231,723 | 228,386 |
| Entries from the six LOCAL-only modules | 18,988 (8.2%) | none |

- **`git` processes: 10,503 fewer (15.5%).** The six LOCAL-only modules
  started 7,310 (`test_local.py` 3,921, `test_durable_run_contract.py`
  1,322, `test_local_recovery_matrix.py` 1,201,
  `test_local_workspace_matrix.py` 514, `test_local_red_team.py` 352).
  Shared modules started fewer too: `test_replan.py` 9,029 to 6,590,
  `test_merge_certificate.py` 4,956 to 3,879, `test_validation_gates.py`
  3,079 to 2,357, `test_integration.py` 2,626 to 1,979; others more:
  `test_live_pi_support.py` 384 to 1,519 (its REMOTE rehearsals, S1a),
  `test_cli.py` 3,632 to 4,527, `test_state_root.py` 217 new.
  `test_engine.py` started 21,213 and 21,344.
- **`tmp_path` entries: 3,337 fewer (1.4%) from 423 more tests.** The six
  LOCAL-only modules left 18,988; the new and changed REMOTE tests leave
  most of it again: `test_engine.py` 70,690 to 75,499,
  `test_live_pi_support.py` 1,418 to 5,832, `test_cli.py` 8,736 to 12,026,
  `test_epic_commands.py` 4,649 to 6,050, the four new S1a modules 2,859
  together (`test_state_root.py` 1,191, `test_state_fuzz.py` 833,
  `test_git_workspace.py` 540, `test_findings_fence.py` 295). The test that leaves the most is still
  `test_runlog.py::test_opening_the_logger_never_lists_sibling_runs`, 4,011
  on purpose.
- **Against B.4**, at the same `4b03151`: 67,555 `git` processes here and
  66,898 on B's host, 231,723 entries from 5,903 tests here and 231,591
  from 5,827 there. The hosts differ in the tests they skip (C.3) and in
  Git (2.56.0 here, 2.47.3 there), which leaves its own files in a
  repository it makes; the LOCAL-only modules' 7,310 `git` processes are
  the same number on both.

### C.5 The regression on the final candidate

On `ccd1910`, this step's code, tests and documents after the first
review of PR #425 (R1-F2 to R1-F4), with C.5, C.7 and C.9 still to be
written. It ran on 2026-11-01 between 00:34 and 01:03 UTC, from a clone of
its own on C.1's host, with `UV_OFFLINE=1`. The commit that lands differs
from it in this record's text only;
`tests/test_removal_attestation.py`, which reads that text,
`tests/test_packaging.py` and the linters ran again on it. The three
pytest runs ran at the same time, two serial suites to a core, so their
times measure nothing (C.3 is the measurement).

| Check | Interpreter | Outcome |
|---|---|---|
| `make check` | 3.14.8 | Passed in 1,540 s. `lock-check`, `lint`, `fmt-check` (174 files) and `typecheck` (57 source files) clean; pytest: 12,327 passed, 5 skipped, 1 deselected, in 1,515 s |
| `make test-py3.11` (`check-matrix`) | 3.11.17 | Passed: 12,327 passed, 5 skipped, 1 deselected, in 1,606 s |
| `make test-py3.12` (`check-matrix`) | uv's 3.12.13 | Passed: 12,224 passed, 108 skipped, 1 deselected, in 1,588 s |
| `make test-sandbox-measured` | 3.14.8 | Passed: the host can enforce (its Python has `os.memfd_create`; bubblewrap 0.12.0 with `--overlay`; unprivileged user, PID and IPC namespaces and a new procfs granted; Landlock ABI 10), so the real-backend tests were required, and all 54 passed |
| `test-sandbox-measured`'s recipe, under `--python 3.12` | uv's 3.12.13 | Passed: the measurement says the host cannot enforce for this Python (`os.memfd_create no`), and the 54 real-backend tests skipped with the probe's reason and the measurement's, none failed. Under `AUTOFORGE_REQUIRE_SANDBOX_BACKEND=1` they fail, as that setting means |
| `make dist-check DIST_PYTHON=3.12` | 3.12.13 | Passed (C.7) |
| `make dist-check DIST_PYTHON=3.11` | 3.11.17 | Passed (C.7) |

- **The candidate adds 117 tests to `3eb68ac`'s.** 84 are in
  `tests/test_removal_attestation.py` and 12 in `tests/test_packaging.py`,
  from before the review. The other 21 come from the review: 13 in
  `tests/test_packaging.py` (R1-F3, R1-F4), and 8 for R1-F2 in
  `tests/test_sandbox.py`, `tests/test_sandbox_gate.py`,
  `tests/test_doctor_sandbox.py` and `tests/test_sandbox_host.py`.
- **Skips.** On 3.14.8 and 3.11.17 each run skipped the five opt-in
  probes of C.3 and nothing else, so no real-backend or zsh test skipped.
  On uv's 3.12.13 the 108 skips are those five and 103 more, each with
  its reason:
  - the 54 real-backend tests, with the probe's typed reason;
  - 45 of `tests/test_sandbox.py`'s tests against a fake `bwrap`, which
    need `os.memfd_create` to hand it the options;
  - four of `tests/test_executor.py`'s pidfd wait, as where the
    interpreter cannot open a pidfd.
- **The REMOTE matrix against fakes** is in every pytest run above: the
  scripted end-to-end loops of `tests/test_integration.py`, the REMOTE
  flows of `tests/test_engine.py` and the EPIC modules against the
  in-memory GitHub, and `tests/test_live_pi_support.py`'s rehearsals with
  the fake `pi`. None reaches a network or a model.
- **uv's CPython 3.12.13 now refuses to enforce, where it raised.** The
  build uv installed on this host in June 2026 has neither
  `os.memfd_create` nor `os.pidfd_open`; 3.11.17, 3.13.16 and 3.14.8 here
  have both, and so did uv's 3.12.16 build, used once before the review.
  `sandbox_bwrap` hands a launch's options to `bwrap` through a memfd, so
  on that build every real launch raised `AttributeError`, and `doctor`,
  whose capability probe is one, exited without its report.
  - **Before the review.** The first run of this table, on `ae0761c`,
    failed `make test-py3.12` there: 12,204 passed, 44 failed and 54
    errors. B.2's 3.12 command had failed on the same call at `4b03151`
    (42 failed, 60 errors) and at `3eb68ac` (44 failed, 54 errors), so
    this predates the removal. The 98 were the real-backend modules,
    which errored in their fixture's probe, 34 of `tests/test_sandbox.py`
    and ten fake-Pi rehearsals of `tests/test_live_pi_support.py`, whose
    `doctor` step failed. `test-py3.12`'s recipe passed on uv's 3.12.16
    with the real backend required (12,306 passed, 5 skipped, 1
    deselected), but that pass says nothing of 3.12.13 itself.
  - **With R1-F2.** Such a Python cannot enforce, and says so.
    `sandbox_bwrap.prepare` refuses it before anything is spawned, with a
    refusal naming the interpreter, and the probe types that as a
    capability of its own (`interpreter`, "a Python with
    os.memfd_create"). So `doctor` reports it, and under `required` or
    `preferred` the engine's gate refuses a step that launches, with
    state unchanged, as on any host that cannot enforce. The default
    `disabled` is unaffected. `tests/sandbox_host.py` measures the Python
    as well, so under `AUTOFORGE_REQUIRE_SANDBOX_BACKEND=auto` the
    real-backend tests it runs skip instead of failing on an otherwise
    capable host (the table above).
  - **The regression tests remove the call**, so they hold on every
    interpreter, this one included: the launch and the probe in
    `tests/test_sandbox.py`, the engine's gate with the real probe in
    `tests/test_sandbox_gate.py`, `doctor`'s rows in
    `tests/test_doctor_sandbox.py`, and the measurement and `auto`'s skip
    in `tests/test_sandbox_host.py`. The rehearsals pass on 3.12.13 now,
    since their `doctor` reports instead of raising.
  - **Where it is documented.** `docs/compatibility.md` names the
    requirement and the build, and the changelog has an entry for it.

### C.6 A.2.4's invariants and the tests that hold them on `main`

Each invariant of A.2.4, in its words, with the tests that hold it at
`3eb68ac` and the three this step adds to `tests/test_packaging.py`. Every
test named here ran and passed in C.5's `make check`, but the live one,
which is opt-in; on this host that includes those a line says skip where
the outer sandbox cannot be enforced.
`tests/test_removal_attestation.py` fails when this list and A.2.4's table
disagree on the invariants, or when a test named here no longer exists.

- **A credential helper never reaches a validation command** (H-A, #386):
  `tests/test_validation_evidence.py::test_a_helper_planted_by_the_producer_never_reaches_its_validation_command`,
  `tests/test_validation_evidence.py::test_a_netrc_entry_planted_by_the_producer_refuses_its_validation_and_blocks`,
  `tests/test_engine.py::test_a_credential_helper_planted_before_candidate_validation_never_reaches_the_command`
  and
  `tests/test_engine.py::test_a_netrc_entry_planted_before_candidate_validation_blocks_the_producer`.
  #386 recorded their mutation run: each fails without the preflight call
  in `_run_remote_validator`, or with the export inside the repository.
- **A login-shell startup file never changes a validation command's isolation** (H-B, #386):
  `tests/test_validation_evidence.py::test_a_startup_file_planted_by_the_producer_refuses_its_validation_and_blocks`
  and
  `tests/test_engine.py::test_a_startup_file_planted_before_candidate_validation_blocks_the_producer`.
- **Git reads take no optional lock, no fsmonitor and no credential** (A, #386):
  `tests/test_git_workspace.py::test_no_read_takes_the_optional_index_lock_or_refreshes_the_index`,
  `tests/test_git_workspace.py::test_reads_run_no_fsmonitor_hold_no_credential_and_ignore_the_operators_git_dir`,
  `tests/test_git_workspace.py::test_uncommitted_paths_is_one_hardened_status_read_and_never_guesses`,
  `tests/test_git_workspace.py::test_status_entries_keep_each_code_and_read_a_rename_or_a_copy_as_one_record`,
  `tests/test_git_workspace.py::test_a_truncated_or_timed_out_read_is_refused_never_parsed`
  and, for C1,
  `tests/test_git_workspace.py::test_engine_workspace_reads_no_configuration`.
- **The state loader refuses unknown fields and non-object roots, and survives fuzzing** (B, #386):
  `tests/test_state_fuzz.py::test_a_remote_state_file_with_an_unknown_field_is_corruption`,
  `tests/test_state_fuzz.py::test_a_remote_state_file_that_is_not_a_json_object_is_corruption_not_a_fresh_run`,
  `tests/test_state_fuzz.py::test_random_corruption_of_a_remote_state_either_fails_or_is_legal`,
  `tests/test_state_fuzz.py::test_the_remote_baseline_state_loads_and_round_trips`
  and
  `tests/test_state_fuzz.py::test_a_remote_state_carrying_a_well_formed_local_checkpoint_is_refused`.
- **The state root holds under a running engine (replaced directory, links, bootstrap)** (C, #386):
  `tests/test_state_root.py::test_a_remote_state_directory_replaced_mid_run_is_refused_and_reaches_nothing`,
  `tests/test_state_root.py::test_a_remote_state_json_replaced_by_a_symlink_is_replaced_back_not_written_through`,
  `tests/test_state_root.py::test_a_remote_state_json_hard_linked_elsewhere_keeps_the_old_bytes_at_the_other_link`,
  `tests/test_state_root.py::test_bootstrap_does_not_follow_a_link_at_the_remote_state_directory`,
  `tests/test_state_root.py::test_bootstrap_of_an_anchored_remote_state_directory_does_not_follow_a_link_below_the_anchor`
  and the documented limit,
  `tests/test_state_root.py::test_documented_limit_a_remote_state_directory_replaced_in_place_by_a_copy_is_not_detectable`.
- **Finding text cannot break out of its block** (D, #386):
  `tests/test_findings_fence.py::test_finding_text_cannot_break_out_of_the_findings_block_of_a_remote_prompt`
  and
  `tests/test_findings_fence.py::test_a_correction_diagnostic_cannot_break_out_of_its_block`.
- **`doctor`'s sandbox rows are right** (E, #386): the twelve REMOTE rows of
  `tests/test_doctor_sandbox.py`,
  `tests/test_doctor_sandbox.py::test_remote_doctor_shows_every_sandbox_row_and_never_calls_github`,
  `tests/test_doctor_sandbox.py::test_outside_a_repository_the_remote_layout_rows_are_skipped`,
  `tests/test_doctor_sandbox.py::test_a_token_across_the_version_clip_leaves_no_fragment_in_remote`,
  `tests/test_doctor_sandbox.py::test_an_unsupported_capability_is_named_for_a_remote_run`,
  `tests/test_doctor_sandbox.py::test_a_dir_rw_login_the_policy_refuses_is_named_with_the_alternatives_in_remote`,
  `tests/test_doctor_sandbox.py::test_an_operator_opencode_config_dir_is_replaced_as_at_every_remote_launch`,
  `tests/test_doctor_sandbox.py::test_a_linked_claude_config_dir_is_spelled_where_it_leads_inside_in_remote`,
  `tests/test_doctor_sandbox.py::test_a_shebang_through_a_link_is_reached_as_written_in_remote`,
  `tests/test_doctor_sandbox.py::test_hard_links_in_the_remote_issue_worktrees_are_listed_by_path`,
  `tests/test_doctor_sandbox.py::test_the_remote_sandbox_directories_and_their_sizes_are_listed`,
  `tests/test_doctor_sandbox.py::test_remote_operator_paths_over_a_denied_root_are_named`
  and
  `tests/test_doctor_sandbox.py::test_a_provider_declaration_over_a_remote_denied_root_is_refused_before_its_launch`;
  and each provider's `--version` through the real backend,
  `tests/test_sandbox_routing.py::test_remote_doctor_launches_each_providers_version_as_a_launch_would`,
  which skips on a host that cannot enforce (C.5).
- **Launch-class declarations stay read-only, and a provider cannot rewrite its `--config`** (F, #386):
  `tests/test_sandbox_policy.py::test_a_remote_agent_declaration_makes_its_state_and_dir_rw_items_writable_only`
  on every host, and through the real backend
  `tests/test_sandbox_routing.py::test_a_remote_launch_rewrites_a_config_file_in_its_providers_directory_so_it_is_refused`,
  which skips on a host that cannot enforce (C.5).
- **A live model run through the real CLI** (G, #386): the offline
  rehearsals of the REMOTE flow,
  `tests/test_live_pi_support.py::test_the_remote_flow_passes_beside_the_checkout_git_dir_names_and_runs_no_planted_hook`,
  `tests/test_live_pi_support.py::test_the_remote_flow_seeds_its_own_origin_whatever_the_operators_gitconfig_rewrites`,
  `tests/test_live_pi_support.py::test_the_origin_is_seeded_and_published_to_alone_whatever_rewrites_the_url`,
  `tests/test_live_pi_support.py::test_the_remote_flows_own_git_never_writes_to_a_repository_the_environment_selects`
  and
  `tests/test_live_pi_support.py::test_the_remote_flows_own_git_runs_no_hook_monitor_or_signer_the_operator_configured`.
  The live run itself,
  `tests/test_live_pi.py::test_one_remote_review_runs_on_the_operators_real_pi`,
  is opt-in and makes a model call, so S4 did not run it; no live REMOTE
  run of it is recorded yet (README, "Current maturity").
- **Sandbox pinning and enforcement** (PRESERVE): the REMOTE rows of
  `tests/test_sandbox_pin.py` and `tests/test_sandbox_gate.py`, among them
  `tests/test_sandbox_pin.py::test_strength_is_what_a_launch_of_the_run_gets`,
  `tests/test_sandbox_pin.py::test_a_remote_run_refuses_disabled_with_nothing_written`,
  `tests/test_sandbox_pin.py::test_a_remote_run_moves_between_required_and_preferred_freely`,
  `tests/test_sandbox_gate.py::test_a_remote_step_on_an_incapable_host_is_refused_with_state_unchanged`
  and
  `tests/test_sandbox_gate.py::test_a_setup_failure_after_the_gate_never_runs_unenforced`;
  and `preferred` as `required` (D7),
  `tests/test_config.py::test_preferred_refuses_unenforced_exactly_as_required_does`.
- **Candidate validation, evidence and certificate binding** (PRESERVE, and
  S3's refusal of the LOCAL kinds): the REMOTE sections of
  `tests/test_evidence.py`, `tests/test_certificate.py` and
  `tests/test_validation_evidence.py`, and
  `tests/test_evidence.py::test_a_local_candidate_is_refused_as_an_unknown_kind`,
  `tests/test_evidence.py::test_a_workspace_variant_record_is_refused`,
  `tests/test_evidence.py::test_a_workspace_variant_marker_is_refused`,
  `tests/test_evidence.py::test_evidence_naming_a_workspace_record_is_refused_whole`,
  `tests/test_evidence.py::test_a_local_certificate_is_refused_as_an_unknown_kind`,
  `tests/test_evidence.py::test_a_binding_naming_a_local_candidate_is_refused`,
  `tests/test_certificate.py::test_issuance_is_refused_for_a_binding_of_a_local_candidate`
  and
  `tests/test_certificate.py::test_a_local_certificate_is_never_applicable_in_a_run`.
- **Crash recovery** (PRESERVE; the four journal cases #386 confirmed):
  `tests/test_runlog.py::test_a_foreign_file_renamed_over_the_journal_during_a_remote_launch_receives_nothing`,
  `tests/test_engine.py::test_a_journal_refused_before_the_launch_charges_no_attempt`
  and
  `tests/test_engine.py::test_post_agent_journal_refusal_names_the_outcome_and_keeps_the_attempt`,
  with the rest of `tests/test_runlog.py` and REMOTE `tests/test_engine.py`.
- **Provider interaction and launch accounting** (nothing shared; REMOTE's
  own bounds):
  `tests/test_engine.py::test_a_journal_refused_before_the_launch_charges_no_attempt`,
  `tests/test_engine.py::test_correction_is_bounded`,
  `tests/test_engine.py::test_k8_the_attempt_bound_exhausted_blocks_naming_the_effect`
  and
  `tests/test_engine.py::test_review_round_cap_blocks_with_findings_and_never_starts_the_last_fix`.
- **Scheduler state ownership** (#385, D4):
  `tests/test_epic_commands.py::test_an_unloadable_standalone_state_holds_nothing_is_reported_and_is_left_alone`,
  `tests/test_epic_commands.py::test_a_namespace_state_whose_mode_is_not_remote_is_held_as_uncertain`,
  `tests/test_epic_commands.py::test_namespace_work_leaves_the_git_dirs_autoforge_state_alone`
  and
  `tests/test_ownership.py::test_an_unreadable_standalone_state_is_reported_and_holds_nothing`.
- **REMOTE state refusals under the drop step** (#385 pinned them, #388
  kept them): the 31 tests that take `tests/conftest.py`'s `local_fields`
  fixture, each run on its file once with LOCAL's twelve fields at their
  empty values and once without, among them
  `tests/test_state.py::test_protocol_1_is_refused_in_the_merge_phases`,
  `tests/test_state.py::test_protocol_3_is_refused_with_a_replan_in_flight`,
  `tests/test_state.py::test_a_mode_other_than_remote_is_refused_under_every_label_and_left_unchanged`,
  `tests/test_state.py::test_protocol_6_and_other_unknown_labels_are_refused_and_left_unchanged`
  and
  `tests/test_replan.py::test_an_in_flight_protocol_4_journal_past_the_write_is_refused_at_the_state_boundary`;
  and the drop step's own refusals,
  `tests/test_state.py::test_an_older_file_with_a_local_field_holding_a_value_is_refused_and_left_unchanged`
  and
  `tests/test_state.py::test_a_protocol_13_or_unlabelled_file_carrying_a_local_field_is_refused`.
- **The release gate's packaging check** (#387, #388, #389):
  `tests/test_packaging.py::test_no_artifact_carries_a_file_of_local_mode`,
  `tests/test_prompts.py::test_no_local_template_is_shipped`, and the
  installed command's refusals that `make dist-check` now runs (C.7),
  checked offline by
  `tests/test_packaging.py::test_the_local_refusal_check_holds_every_way_in_to_its_refusal`
  and
  `tests/test_packaging.py::test_the_local_refusal_check_covers_each_way_in_adr_0009_names`.

### C.7 The installed wheel and sdist

`make dist-check` on the candidate, on CI's two interpreters, offline: uv's
cache held the build backend `build-constraints.txt` pins (setuptools
84.2.1). Each run built the sdist and the wheel of `autoforge-cli
0.1.0a1.dev0` from a copy of the source tree, installed the wheel
non-editably into a fresh environment outside the source tree, and ran the
installed command. Both passed: on 3.12.13 in 11.7 s, on 3.11.17 in 13.2 s.
What it checks of the removal, besides its earlier checks:

- **No file of LOCAL mode in either artifact or the installed package.**
  No `prompts/local_*.md`, `local_workspace.py` or `run_contract.py` in the
  sdist (82 files), the wheel (70 package files) or the installed package,
  whatever the tree declares, and neither `autoforge.local_workspace` nor
  `autoforge.run_contract` importable from the install. All 13 prompt
  templates rendered from it.
- **Each of LOCAL's fourteen ways in meets its generic refusal** (D3 to
  D5), in a scratch repository whose state file says `mode: "LOCAL"`,
  beside a run log of its own, and with a configuration file that has a
  `local:` key. `--help` names no `local`. `autoforge local`, `autoforge
  local run features/add-filter.md` and `autoforge local doctor` are
  argparse's invalid choice; `--config <that file> status` is the unknown
  top-level key; `status`, `status --json`, `--state-dir <its .autoforge>
  status`, `step`, `resume` and `unblock --reason looked`, each both with
  `--dry-run` and without it, and `run --epic … --issue …` are the
  loader's unknown mode. Each exited 2 with that message. `doctor`'s
  refusal of a `local:` key is `tests/test_cli.py`'s, since `doctor` runs
  `gh` and the agent CLIs.
- **The ordinary invocations, not only their dry runs** (PR #425 R1-F3).
  A dry run of `step`, `resume` or `unblock` loads the state without the
  lock. The ordinary invocation refuses it before the lock, and would
  otherwise take the lock, write and launch. So every command ran in an
  environment of the check's own: no variable of the caller's but `PATH`,
  a scratch `HOME` with its XDG directories, a scratch `TMPDIR`, and
  recording fakes of `gh`, `claude`, `opencode` and `pi` first on `PATH`
  that fail any call, so that a regression reaches no service and no
  model. None was called. With `step`'s pre-lock refusal removed from the
  source tree, the check failed: `step` took the lock and created
  `.git/autoforge/controller.lock`, while every dry-run row still passed.
  Restored, it passed.
- **Nothing written, by bytes, identity and metadata** (R1-F4). Before
  and after each command the check compares, for every path of the
  repository (`.git`, the state directory and the run log included), the
  configuration file, the scratch `HOME` and `TMPDIR`: its bytes or link
  target, device and inode, mode, owner, link count, size, `mtime` and
  `ctime`. `atime` is left out, since a read sets it. A file rewritten in
  place with its own bytes shows by its `mtime`, one replaced by a copy of
  itself by its inode, times put back with `touch -r` by the `ctime`, and
  a lock file taken and removed by its directory's `mtime`. What the
  comparison cannot see: a write outside those paths; an advisory lock
  on a file that already existed (none does: the controller lock would
  create its file); a write within one tick of the kernel's timestamp
  clock of the first snapshot, which the command's own start-up outlasts;
  and a write after the command exits, by a process it left running. The
  REMOTE dry run's check compares the same way.
- **The check is itself tested**, offline, by `tests/test_packaging.py` on
  stand-in commands. Twenty misbehave in one way each against the LOCAL
  check: among them an ordinary `step`, `resume`, `unblock` or `run`
  that takes a lock or launches `claude`, `opencode` or `pi` while its
  dry run behaves; a same-byte rewrite, a replacement by a copy, times put
  back; and a write to the scratch `HOME` or `TMPDIR`. Six do against the
  REMOTE dry run's, a same-byte rewrite among them. One more test checks
  that none of the caller's credentials reaches the command. Before the
  review, the check was also run once against `4b03151`'s command, where
  it fails as it should: that command's `--help` names `local`.

These builds are of an unreleased candidate on this host, for the record
only. Nothing was published or tagged.

### C.8 The documentation audit

Every current document was searched at `3eb68ac` for LOCAL's vocabulary
(`LOCAL`, local mode, `autoforge local`, `local:`, the feature
specification and `features/`, the workspace fingerprint and walk, the run
contract, `sandbox/local`) and for links into ADR 0001's superseded parts:
`README.md`, `SECURITY.md`, the three `AGENTS.md` and `CLAUDE.md`, every
document under `docs/` but the ADRs, `autoforge.example.yaml`, the CI
workflows, `pyproject.toml`, `MANIFEST.in` and the comments under `src/`.
What described LOCAL as current, or pointed at what is gone, was corrected
in #389:

- `docs/agent-guides/secrets-and-logging.md` said the block reason for a
  moved checkout names the feature specification's path;
  `_moved_checkout_recovery` names the checkout and two commands only.
- `src/autoforge/prompts/__init__.py` gave a feature specification as the
  untrusted value a template variable may carry; it now names the ones
  REMOTE has.
- `docs/pi.md` and `docs/pi-policy.md` cited ADR 0001 §2 and §2.2, parts
  this record superseded, for "an agent runs as your UID"; they cite
  `docs/security.md` ("Agents run as you") instead.
- `docs/non-code-rules.md` gave a `features/` spec as its example of a file
  committed in a PR.
- `README.md` listed ADR 0001 among the design records without saying it is
  superseded; ADR 0009's status, the `AGENTS.md` map and `docs/README.md`
  said S4 remained.

Kept, because each is accurate: the refusals of a LOCAL artifact and where
an earlier release's LOCAL run kept its files (`state-and-recovery.md`,
`testing.md`, `usage.md`, the changelog's #387 entry with D10's manual
clean-up); README's note that Pi's one live run was in a mode that is gone;
`docs/compatibility.md`'s Pi row, which says the same; the protocol
history of `state-and-recovery.md`; `docs/development.md`'s measurement of
#341, which names `test_local.py` at `a86b729`; `CHANGELOG.md`'s past
entries and the ADRs, which are history (D10, D11).

### C.9 What S4 does not show

- **The GitHub side of #384 and of the EPIC, as this record cannot read
  it.** S4 is to read back the native sub-issue and `blocked_by` links of
  §5.3 and the coordination comments of §5: #155's amendment note and
  #255's proposal. The agent that wrote this appendix holds no GitHub
  credential and reads only what the controller hands it, #389 alone
  (ADR 0004 D10), so it verified none of them itself. `main`'s history
  shows that the PRs of the decision and of S1a to S3 merged: #406 for
  #384 (`d6c4838`), #409 for #385 (`06ea254`), #410 for #386 (`a4bc60c`),
  #413 for #387 (`aee704e`) and #421 for #388 (`3eb68ac`). The rest is
  evidenced by three notes PR #425's first fix round asked the operator
  for. The controller verifies each on its issue before that round's
  commits are pushed, and the next review judges them:
  - on #383,
    [a note](https://github.com/iceboundrock/AutoForge/issues/383#issuecomment-6552134554)
    with the native parent, `blocked_by` links and states of #384 to #389,
    as the operator read them back before posting it. The controller
    verified it as posted exactly as prepared;
  - on #155,
    [a note](https://github.com/iceboundrock/AutoForge/issues/155#issuecomment-6552134735)
    with §5.1's amendment now that S2 and S3 are on `main`, which
    supersedes or gives the REMOTE form of each LOCAL line. Verified the
    same way;
  - on #255, a note that cites the maintainer's two decisions of
    2026-10-30 (§5.2's closing note) and says what S4 found on `main`
    against them, below. It does not reopen the order.

  An operator who finds a link, a state or a comment other than a note
  says declines that note, and the difference comes back to this record.
  That happened once. The round's first note on #255 was written before
  this record knew of the decisions: it applied option 3, quoted §5.2's
  proposed C7 and said C7 was undecided. The operator declined it, since
  posting it would have misstated a recorded decision, and named the two
  decision comments. This record took them from that reason, with their
  links, and was brought into line (the header, §4, §5.2's closing note
  and this section); the second note replaces the first.

  What S4 found on `main` against the decisions:
  - **Option 1's prerequisite has landed.** S2 (#387, `aee704e`) and S3
    (#388, `3eb68ac`) are on `main`, so a release candidate cut from it
    carries the removal and protocol 13.
  - **"Not advertised."** C.8's audit found no current document that
    describes LOCAL as available: `docs/local-mode.md` is gone (S2), and
    README's "Current maturity", the levels the Public Alpha claims,
    names LOCAL only as the gone mode of Pi's one live run.
  - **"Not supported."** The installed wheel and sdist carry no file of
    LOCAL mode, reject `autoforge local`, refuse a `local:` key, and
    refuse a `mode: "LOCAL"` state with nothing written (C.7).

  What this record cannot read, and the notes ask nothing of: whether
  #255 or #383 now carries a link for option 1's order, and what #255's
  children (#259, #260, #262) say of LOCAL. Nothing here edits C7 or any
  gate. Until the controller has verified the #255 note, this part of S4
  is not done.
- **An independent review.** The security of the removal, the state
  migration and its crash windows are for the review of #389's PR and the
  maintainer's; this appendix records tests and measurements, and is
  evidence for that review, not the review.
- **A live model run.** G's live test was not run (C.6).
- **The removal's own effect on the suite's time.** Between `4b03151` and
  `3eb68ac` lie eleven commits. Besides S1a to S3 and this record they
  carry #341 (`acebe52`), which cut the serial suite's wall time by 36% on
  a 4-CPU host with `TMPDIR` on a disk, and by less with it on a tmpfs
  (`docs/development.md`, "Where the serial suite's time goes"), four other
  changes with tests of their own (#393, #343, #349, #287) and ADR 0010's
  tooling (#342). C.3's difference is all of theirs together; C.3 and C.4
  split it by test and by module, which says where time and processes
  moved, not which commit moved them.
- **A release.** Nothing is tagged, published or declared ready. The next
  `0.1.0aN` needs the maintainer's approval, its own Go/No-Go and
  `docs/releasing.md`'s procedure. Under option 1 the Public Alpha's release
  candidate carries the removal; whether the next `0.1.0aN` is the Public
  Alpha is #255's decision, not this record's.
