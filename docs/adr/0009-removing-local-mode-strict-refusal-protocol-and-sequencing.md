# ADR 0009. Removing LOCAL mode: no compatibility, strict refusal, the state protocol, sequencing and the baseline

- **Status:** policy accepted, nothing implemented. This record changes no
  behaviour.
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
    Alpha's release candidate (§5.2).
  - *Implementation:* none. #385 to #389 implement this record after it
    merges (D12, §5.3).
  - *Release:* nothing is authorized. D10 says what the release that
    carries the removal must say; it is not a Go/No-Go, a tag or a
    publication.
  - ADR 0001 stays accepted, and nothing in any ADR is marked superseded,
    until the removal is complete on `main` (D11).
- **Decides:** #384 (RET-A1), for EPIC #383.
- **Planned against:** `main` `02ce244` (2026-10-30). Every line number in
  this record is at that commit. The inventory was taken at `4b03151`;
  #405, the one commit between the two, changed `engine.py`, eight prompt
  templates (`local_common.md` and `local_analyze_execute.md` among them),
  five test modules and six documents. Every line reference into them was
  moved to `02ce244` and checked against the symbol it names, and the
  counts were taken again. Appendix B's baseline stays at `4b03151`.
- **Will amend:** ADR 0001, superseded except the parts REMOTE still relies
  on, and the LOCAL decisions of ADR 0003 to 0008 that Appendix A.4 lists,
  superseded in part. Both take effect when the removal is complete on
  `main` (D11).
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
| S2. Close every way in | #387 (RET-A4) | D3 to D5: the `local` subcommand; the `local:` key (the internal `LocalConfig` type stays until S3, read by nothing reachable); the loader's mode refusal; the probe; the scheduler's LOCAL skip; `dist_check.py`'s LOCAL half; the example's `local:` block; the live Pi flow's LOCAL form; the current documents; the changelog entry. LOCAL becomes unreachable, but its code is still there | S1a: C1 before the `local:` key goes, G before the key and the subcommand go. S1b |
| S3. Delete | #388 (RET-A5) | Every DELETE row, with its tests: the LOCAL engine paths, transitions, prompts and result models, `run_contract.py`, the rest of `local_workspace.py`, the LOCAL launch classes, the workspace record variant and the `local` certificate kind, `doctor`'s LOCAL subset, `LocalConfig`. D8's protocol bump. The instruction maps, the agent guides and the test contracts. D11's ADR notes | S2. The protocol number confirmed on #23 |
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
  type-checked. S2's tests show that no entry point reaches it, and S3
  deletes it. S2 and S3 should land close together.
- **The protocol bump meets another persisted change.** The numbering rule
  already settles it: whichever merges second takes the next number.
- **The order against the Public Alpha is undecided** (§5.2). Until it is
  decided, whichever lands first must leave #255's C7 wording true.
- **The inventory is a snapshot of `02ce244`.** A LOCAL reference added
  after it is not in Appendix A. Each step re-greps before it merges, and
  S3's structural test catches what is left.

## 5. Related issues and the EPIC's order, read on 2026-10-30

#384 asks for #155 and #255 to be reviewed, and for precise edits and
dependency changes to be proposed on them. This section is that review,
from each issue's current body, comments and native links.

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

These edits are made in #155's body alongside this record, as an amendment
that strikes each LOCAL line through and states its REMOTE form, so the
original text stays readable. No dependency edge is added in either
direction. D2 lets an SDK change refuse a LOCAL run instead of implementing
it, so #155 need not wait for #383, and #383 need not wait for #155.

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
proposal is
[posted on #255](https://github.com/iceboundrock/AutoForge/issues/255#issuecomment-6531809299)
for that decision.

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
| S2 | #387 (RET-A4) | Removes the public surface completely, with no stub. It gains the mode refusal, the probe's deletion and the scheduler skip's. The LOCAL prompt templates, ADR 0001's note and the instruction maps move to #388 |
| S3 | #388 (RET-A5) | Deletes the unreachable runtime and evidence code, carries D8's bump and the #23 announcement, and keeps no legacy discriminator |
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
| #387 ← #386 | "Coordinate" | Hard | Yes | C1 before the `local:` key goes; G before the key and the subcommand go |
| #388 ← #385, #386, #387 | Yes | Yes | Yes | Nothing reachable is deleted, and no LOCAL-only security regression is deleted before its REMOTE replacement passes (A.2.3 H) |
| #388's protocol bump ← #23 | No | Confirmed on #23 before the merge (D8) | No | A process gate, not an issue edge |
| #389 ← #387, #388 | Yes | Yes | Yes | |
| #241's item 3 and #386's H | No | One set of tests | No | Whichever lands first; the other links it |
| #255 and #383 | No; #383 names #255 as interacting work | Undecided (§5.2) | No | No edge until the maintainer decides |
| #155 and #383 | No; #383 names #155 as interacting work | None (§5.1) | No | |

The issue bodies of #383 to #389 are rewritten to this plan alongside this
record. GitHub keeps each body as the authoritative copy, and the native
links above are the ones the EPIC scheduler reads.

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
| GENERIC STRICT REFUSAL | 12 | The earlier revision's LEGACY tests. Each asserted that a LOCAL artifact loads, is shown or is left alone. Each is rewritten to assert D3's and D4's generic refusal and an untouched file, or deleted where an S1b or S2 test covers the same case |
| PRESERVE SHARED, edited | 55 | 49 drop a LOCAL row or parameter, 6 move out of a LOCAL-only module |
| PRESERVE SHARED, untouched | 3,885 | |

#### A.2.1 By module

| Module | Functions | In scope | Class |
|---|---|---|---|
| `test_local.py` | 117 | 117 | DELETE 107; REPLACE 8 (A.2.3 A, B, H); move 1 (`test_saved_state_without_a_mode_field_loads_as_remote`); GENERIC 1 (`test_unknown_mode_fails_loudly`, which gains a `"LOCAL"` case) |
| `test_local_recovery_matrix.py` | 24 | 24 | DELETE (journal cases covered in `test_runlog.py` and REMOTE `test_engine.py`) |
| `test_local_red_team.py` | 21 | 21 | DELETE (the artifact-name case is covered in `test_runlog.py`; the state-directory replacement partly in `test_state.py`, the rest in A.2.3 C) |
| `test_local_state_fuzz.py` | 6 | 6 | DELETE 4; REPLACE 2 (A.2.3 B) |
| `test_local_workspace_matrix.py` | 9 | 9 | DELETE |
| `test_durable_run_contract.py` | 43 | 43 | DELETE 30; REPLACE 6 (A.2.3 C, D); move 5 (two AST guards on state access, the `link(2)` publication test, the REMOTE worktree test, the REMOTE half of a correction-fence test); GENERIC 2 |
| `test_live_pi.py`, `test_live_pi_support.py` | 1, 28 | 1, 15 | REPLACE 15 (A.2.3 G); 13 checks of the support module are mode-free |
| `test_doctor_sandbox.py` | 18 | 14 | REPLACE 12 (A.2.3 E); DELETE 1; PRESERVE 1 |
| `test_sandbox_routing.py` | 21 | 8 | DELETE 6; REPLACE 2 (A.2.3 E, F) |
| `test_sandbox_policy.py` | 54 | 3 | DELETE 2; REPLACE 1 (A.2.3 F) |
| `test_state.py` | 160 | 12 | DELETE 7; GENERIC 2; PRESERVE 3 (drop the `local` id) |
| `test_config.py` | 142 | 14 | DELETE 6; PRESERVE 6; GENERIC 2 (the `local` row of the unknown-key test, now expecting a refusal; the non-mapping `local:` block) |
| `test_certificate.py`, `test_validation_evidence.py`, `test_validation_gates.py`, `test_review_certificate.py`, `test_merge_certificate.py` | 40, 37, 44, 20, 29 | 9, 9, 10, 4, 2 | DELETE (each has REMOTE sections) |
| `test_evidence.py`, `test_result_parser.py`, `test_transitions.py` | 77, 134, 19 | 10, 15, 5 | DELETE 5, 4, 2; PRESERVE the rest (drop LOCAL parametrizations) |
| `test_sandbox_gate.py`, `test_sandbox_pin.py`, `test_config_fingerprint.py` | 12, 15, 35 | 5, 4, 2 | DELETE 5, 2, 2; PRESERVE 2 in `test_sandbox_pin.py` |
| `test_cli.py`, `test_ownership.py`, `test_epic_commands.py`, `test_run_namespace.py` | 119, 16, 32, 45 | 3, 1, 1, 1 | GENERIC 2, 1, 1, 1 (`test_run_namespace.py`'s is `test_legacy_standalone_state_is_never_read_moved_or_modified`, which S1b keeps); PRESERVE 1 in `test_cli.py` |
| `test_doctor.py`, `test_prompts.py`, `test_routing.py`, `test_sandbox_parity.py` | 122, 53, 5, 1 | 4, 12, 1, 1 | DELETE 2 in `test_doctor.py`; PRESERVE the rest (drop the LOCAL row, template or profile) |
| `test_sdk_policy.py`, `test_interaction.py`, `test_unblock.py` | 46, 73, 39 | 1 each | DELETE |
| `test_safefs.py` | 49 | 0 | PRESERVE. Introduced with LOCAL, uses none of it |
| `test_engine.py`, `test_lock_domains.py` | 557, 39 | 0 | PRESERVE; they import `StatusEntry` and `GIT_TIMEOUT_SECONDS` from `local_workspace` (C1) |
| The other 46 modules | 1,972 | 0 | PRESERVE |

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
  (`test_local_state_fuzz.py`). They become REMOTE state tests.
- **C. The state root under a running engine** (5,
  `test_durable_run_contract.py`): a state directory replaced mid-run, a
  `state.json` replaced by a symbolic link or hard-linked elsewhere, the
  documented undetectable in-place copy, a link at the state directory or
  its parent at bootstrap. `test_state.py` covers only rename-aside and a
  symbolic link at `state_root()`.
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

Each shared invariant a LOCAL test protects today has an owner who keeps it
protected before S3 deletes that test.

| Invariant | Protected only through LOCAL today | Owner and prerequisite |
|---|---|---|
| A credential helper never reaches a validation command | H-A | #386 (S1a), the mutation-checked REMOTE test |
| A login-shell startup file never changes a validation command's isolation | H-B | #386 (S1a), the same |
| Git reads take no optional lock, no fsmonitor and no credential | A | #386: the tests move with the reader (C1) |
| The state loader refuses unknown fields and non-object roots, and survives fuzzing | B | #386: REMOTE state tests |
| The state root holds under a running engine (replaced directory, links, bootstrap) | C | #386: REMOTE or neutral tests |
| Finding text cannot break out of its block | D | #386 |
| `doctor`'s sandbox rows are right | E | #386: REMOTE rows |
| Launch-class declarations stay read-only, and a provider cannot rewrite its `--config` | F | #386, on a REMOTE class; the real-backend test runs where a host can enforce |
| A live model run through the real CLI | G | #386: the REMOTE flow and its offline rehearsals |
| Sandbox pinning and enforcement | `test_sandbox_pin.py`'s and `test_sandbox_gate.py`'s LOCAL rows | PRESERVE: their REMOTE rows stay. Apart from E and F above, no REMOTE sandbox test depends on LOCAL |
| Candidate validation, evidence and certificate binding | None: each module has REMOTE sections | PRESERVE. S3 adds the refusal of LOCAL kinds in a REMOTE file |
| Crash recovery | `test_local_recovery_matrix.py`'s journal cases | PRESERVE: `test_runlog.py` and REMOTE `test_engine.py` cover them; #386 confirms each case before S3 |
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
interaction and epic requirements. S3 removes those requirements, and adds
D4's and D5's generic refusals as required coverage: a `mode: "LOCAL"`
state refused with nothing written, a `local:` key refused, `autoforge
local` rejected, and no probe of LOCAL's location.

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
| `docs/agent-guides/github-safety.md`, `testing.md`, `replan-transaction.md`, `secrets-and-logging.md` (S3) | "LOCAL prompts" (`github-safety.md` :1265–1266); "LOCAL on Pi" parity (`testing.md`) | the rest (A.2.5 for `testing.md`) | |
| `AGENTS.md`, `src/autoforge/AGENTS.md`, `tests/AGENTS.md` (S3) | the `local-mode.md` map line; `run_contract.py`, `local_workspace.py`; the LOCAL fixtures and test rows; this record's LOCAL routing rule | `preferred` in the isolation terms (:56–66); the ADR map line; the module map; the LOCAL production rule (:184–189), keeping its `safefs.py` pointer to ADR 0001 §5.4 | |
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
