# State, persistence, locking, recovery, and error classification

Read this before changing the persisted state file or its schema
(`src/autoforge/state.py`, `src/autoforge/run_contract.py`), atomic writes and
the runtime filesystem boundary (`src/autoforge/safefs.py`), `resume` or any
other crash-recovery path, merge counting, the repository lock
(`src/autoforge/locking.py`), the error taxonomy (`src/autoforge/errors.py`),
or which failures are retried and which block.

---

## Persistent state

Runtime state belongs under `.autoforge/` and must not be committed.

Expected state includes data such as:

- protocol version
- controller version
- prompt version
- run ID
- repository
- EPIC URL
- current issue URL
- current PR URL and its branch
- phase
- review round
- reviewed PR URL, reviewed HEAD SHA, reviewed base branch and reviewed
  merge base SHA (the revision the last review round decided on, the
  merge base being the commit its diff was computed from; the merge gate
  requires all four)
- current HEAD SHA, current base branch and current merge base SHA (read
  from GitHub before the review is launched; the merge base is cleared
  wherever the HEAD or base is rebound outside `REVIEW`, and the next
  `REVIEW` entry reads it again)
- latest review comment URL
- latest review result
- merged-since-EPIC-update count (merges not yet reflected in the EPIC's
  roadmap section; drives `workflow.epic_update_every` and is reset only
  after the controller's roadmap write is read back)
- already-counted merged PRs (in merge order; the last
  merged-since-EPIC-update entries are the batch handed to the agent)
- attempt number
- block reason (redacted and then bounded to `state.MAX_BLOCK_REASON_CHARS`
  (8000 characters) by every engine writer, through `state.bound_block_reason`: `_block` and
  the two agent-message writers; a longer reason keeps its head, an
  omission marker naming the count dropped, and its last
  `BLOCK_REASON_TAIL_CHARS` (1000), so what happened and what the operator must do
  both survive. It is operator diagnostics, not work a later phase acts on,
  which is why it may be clipped where a finding or resolution is rejected
  instead; what is clipped is still whole in `last_fix_resolutions` (a
  LOCAL fix round's unresolved rationales) or the run log (an agent's
  `message`). The bound is a writer's rule, not a loader's: a longer value in a
  file, written by an older controller, loads and is not rewritten), and the
  operator unblock history (one entry per applied
  `autoforge unblock`: timestamp, reason, the block reason it cleared, the
  phase re-entered and the controller's detail, plus `action`
  (`approved A-…` or `declined A-…`) when it approved or declined an
  acceptance action (#211), and `bound X-… <comment URL>` (one per pair,
  `; `-separated) or `declined X-…` (followed by `(withdrawn: X-…)` when
  the decline withdrew other pending actions) when it bound evidence to,
  or declined, operator actions (#243); refused unblocks are only in
  the run log; the list is kept across issue switches, redacted before it is
  written, and validated on load like every other list field)
- the recorded configuration of a REMOTE run (`config_fingerprint`, #219).
  For each profile the run routes to it holds the provider, model, effort
  and effective idle and max-runtime limits. It also holds the resolved
  agent worktree root, the absolute `--config` path (`""` for the built-in
  defaults), its `source` (`created` by `run`, `upgraded` from a file
  without one, `accepted` by `--accept-config-change`) and `recorded_at`.
  `step`, `resume` and `unblock` compare their configuration with it
  before any phase executes. They refuse a difference, writing nothing,
  unless the operator accepts it, and an accepted change goes to the run
  log (`<PHASE>-config-change`) before the state save. The record holds no
  `command`, `extra_args`, `options` or anything else that could carry a
  secret, and it is validated on load (`config_fingerprint.py`, an exact
  key set per schema 1). That parser also runs on every record before it
  is written: a configuration it would refuse (a model over 512
  characters, an `update_epic` limit of 0) is a configuration error at
  `run`, `doctor`, an upgrade or an accepted change, so no state file is
  written that could not load again. It is `{}` for a LOCAL run, whose run contract
  defines it ([The run contract](../local-mode.md#the-run-contract)), and
  for a run loaded from a protocol-8 file until its next command records it
- candidate validation evidence (#172, [below](#validation-evidence)):
  `validation_records`, `validation_attempt`, `review_evidence`,
  `review_certificate`, `producer_validation_failure` and
  `validation_override_seq`
- timestamps

Besides these, the state records the review loop: `review_round`,
`last_review_needs_fix`, `open_findings`, `prior_findings` (a stale round's
findings, carried to the next review to re-check), `last_fix_resolutions`,
`review_history` (one entry per completed review round of the current PR:
round, reviewed SHA, result, finding count, fingerprint of the requested
resolutions) and `step_count` (cumulative for the run, never reset). A
REMOTE `last_fix_resolutions` entry carries `acceptance_comment_url`: the
comment the controller verified for an `acceptance_action` resolution
(required, on the run's issue), and `""` for any other (#211).
`acceptance_outcomes` lists what became of the current PR's acceptance
actions once their plan is gone: one entry per action (`action_id`,
`round`, `finding_id`, `outcome` `verified`, `declined` or `stale`, `at`,
`comment_url` for a verified one, and the redacted, bounded operator
`reason` of a decline), at most `MAX_ACCEPTANCE_OUTCOMES` (64, oldest
dropped, but never a decline of the round being fixed: a decline re-enters
`FIX` without advancing the round, and a dropped one would let its action
through again, PR #221 R3-F1), validated on load and reset with the other
per-PR fields (a new issue, an activated replacement PR, a newly bound PR).
A round holds at most `MAX_DECLINES_PER_ROUND` (16) declines, below the
history's bound: once it holds that many, `FIX` refuses any further
acceptance action of the round, and a file with more in any round is
refused. The FIX prompt lists the round's declines, and the controller
refuses a declined id again. A REMOTE `last_fix_resolutions` entry also
carries `operator_evidence` (#243): the operator comments on other issues
verified for its finding in the round, each `action_id`, `target`,
`comment_url`, `evidence` (`exact` or `bound`), `body_sha256` and
`payload_sha256`, and `[]` when none. `operator_comment_outcomes` lists
what became of the current PR's operator actions (ADR 0004 K10) once their
plan is gone: one entry per action (`action_id`, `round`, `finding_id`,
`target`, `outcome` `verified`, `declined`, `withdrawn` or `stale`,
`evidence` `exact` or `bound` for a verified one and `""` otherwise,
`comment_url` and `body_sha256` of a verified comment, `payload_sha256` of
the requested payload, whose text is not kept, `at`, and the redacted,
bounded `reason`). A `verified` entry is unique per (action, comment). The
list holds at most `MAX_OPERATOR_COMMENT_OUTCOMES` (80), which is at least
`(MAX_DECLINES_PER_ROUND + 1) × MAX_OPERATOR_ACTIONS_PER_FIX` (68), so one
round always fits; the oldest entries are dropped, never one of the round
being fixed. It is validated on load and reset with the other per-PR
fields, and read by `status`, by the FIX prompt and result check (a
declined id, or a (finding, target) already verified in the round, is a
correction) and by REVIEW's `operator-evidence.md`. The round's `declined`
entries of both histories count together toward `MAX_DECLINES_PER_ROUND`;
a `withdrawn` one does not. A
LOCAL run carries none of this. A
REMOTE run also records its replan history: `execution_attempt` (initial
implementation is 1),
`escalation_count` (completed replans), `superseded_prs` (each entry naming
the transaction that superseded it), and the durable `replan_transaction`
record: the controller's intent journal, which `resume` replays rather than
re-deriving a decision from GitHub facts
([replan-transaction.md](replan-transaction.md)).

A REMOTE run also records the effect state of the current phase entry (ADR
0004, `src/autoforge/effects.py`; [github-safety.md](github-safety.md),
"Controller-owned effects"):

- `effect_records`: one record per controller write the entry plans, in
  plan order. Each holds the kind (the closed Wave 1 set: push,
  implementation PR, adopt PR, review comment, follow-up issue, follow-up
  append, replacement PR, progress comment; and, since #211, the acceptance
  comment, whose precondition also records its `authorization`, `operator`
  or `policy`, and the `approved_action_id`, which must equal the action id
  recomputed from the record; and, since #243, the operator comment, which
  the operator performs and the controller only verifies: never
  `attempted`, its attempt count 0, its precondition recording why its
  target is allowed, the digest of the issue body the target was read from
  and the operator's `binding` (`null`, or the bound comment's URL, id and
  body digest), and its observed result the comment's URL and id, its
  `evidence`, `exact` or `bound`, and its body's digest), its owner (run,
  phase, issue,
  PR, transaction), the identity, the exact target, the precondition, the
  payload exactly as it is sent, the attempt count (at most
  `MAX_EFFECT_ATTEMPTS`, 2), the stage (`intended`, `attempted`, `observed`,
  `conflict`), the observed object and a conflict reason (on an `intended`
  operator comment, the reason its last verification failed: a changed or
  deleted bound comment, or a target that cannot be read or no longer
  qualifies, replaced or cleared by the next verification and cleared by a
  new binding; `status` reports it as `verification_failed`). Only redacted,
  bounded values are stored: a payload that is not redaction-invariant is
  refused, never redacted into something else, and no credential, header or
  environment value is ever stored. At most `MAX_EFFECTS_PER_PLAN` records,
  and every payload string of the plan plus the completion context at its
  stored bound fit in `MAX_EFFECT_STATE_CHARS`.
- `entry_observation`: what the entry's identity reads saw before the
  phase's first launch (D4.4), so that an object found later is explained by
  a record, by this observation, or not at all (D9.1): the heads of the refs
  it read, the base, the marker-bearing objects, and, under the optional
  key `prs`, the one open PR headed at a ref it read, or none. `prs` is
  stored only when PRs were read (`ANALYZE_EXECUTE`, whose adoption
  candidate carries no marker yet, #161), so every other phase's
  observation is stored exactly as before; a ref missing from `prs` was
  not read, which is never taken for "no PR", and a stored `prs` that is
  empty, over its bound, or names a ref the observation did not read is
  refused. The `ANALYZE_EXECUTE` observation also records the default
  branch it read, as a ref at the base. The `REPLAN_REEXECUTE` observation
  (#164) records the derived replacement branch as absent and the
  transaction's default branch at the base the replan agent builds on. A
  `FIX` observation (#243) also records, under the optional key
  `operator_targets`, the issues an operator action may target, read once
  from the run's issue before the first launch (ADR 0004 D5.8): `numbers`
  (the issues its body references, first mention first, at most 60, then
  the EPIC), `allowed_by` per number (`issue_body`, `epic`) and
  `issue_body_sha256`. It never holds the run's issue or PR, no other
  phase stores it, and it is absent when the run offers no operator
  action (`safety.acceptance_actions: off`, an issue that could not be
  read conclusively, or an entry observed under protocol 10).
- `completion_context`: what the phase needs to finish without relaunching
  its agent once its result is accepted (D4.6). For `UPDATE_EPIC` this is
  the roadmap section, the validated selection, the digests of the EPIC body
  outside the markers, and which input a persisted rejection voided. For
  `ANALYZE_EXECUTE` (#161) it is only the issue: the push and PR records
  carry everything the completion needs. For `FIX` (#163) it is the round
  and one resolution per open finding: the redacted rationale, the commit
  the fixer named, and the follow-up source of a deferred finding (`entry`
  for the follow-up the entry handed over and the fixer reused, or the plan
  position of the `follow_up_issue` or `follow_up_append` record that
  records the deferral), plus `acceptance_source`, the plan position of the
  `acceptance_comment` record of the one `acceptance_action` resolution, or
  `null` (#211), and `operator_sources`, the plan positions of the
  `operator_comment` records of an `operator_acceptance` resolution, or
  `null` (#243). The plan holds the acceptance comment first (at most one),
  or the operator comments first (at most four, in result order, never
  beside an acceptance comment), then the creates, then the appends, then
  the `push` of the candidate, last, when the fixer committed. Each
  operator comment is named by a resolution of its finding and round,
  and its target, `allowed_by` and body digest equal the entry
  observation's `operator_targets`. For `REPLAN_REEXECUTE` (#164) it is
  the transaction id and
  the two counts the replacement's transaction marker attests; the plan is
  the `push` of the candidate to the derived replacement branch, then the
  `replacement_pr` from it.
- `launch_label` (D13.3): whether the current entry's launches ran under the
  agent-publishes or the controller-publishes contract of its phase.

Each record changes stage in a save of its own, and the `attempted` save
precedes the write (D4.1). The effect state belongs to one phase entry, and
every save settles it (`settle_phase_effects`, D2.4): a save in another
phase drops it, except `BLOCKED` and `FAILED`, which keep it for the
operator and for `unblock` back into the phase (which reconciles a
`conflict` record again, with its attempt count kept). Because of that,
`run` refuses to replace a `BLOCKED` or `FAILED` run whose plan holds
operator actions unless `--force` is given, naming each action and any
observed comment (#243); the comments stay on GitHub either way. The fields are
validated on load against the run's issue, PR and phase, and a corrupt or
incomplete value fails loudly, never replaced by a default. Every protocol-7
save writes all four fields, so a protocol-7 file missing one is refused:
an absent `effect_records` read as its empty default would be an empty plan,
and the phase would complete from its context without the write it had
planned. An `UPDATE_EPIC` completion context is checked against the plan and
the entry observation of its own phase for the same reason: it loads with
the one `progress_comment` record of its own (issue, PR) marker, or with no
record only when the entry observation records that marker's comment as
adopted (the D13.7 legacy re-entry). Neither, both, or no entry observation
is refused, and completion itself refuses to splice the roadmap section or
switch issues without a posted or adopted comment. An `ANALYZE_EXECUTE`
completion context loads only with its plan of exactly two records, the
`push` first and then the `implementation_pr` from the pushed branch or the
`adopt_pr`, and with the entry observation it is checked against: the push
is over the head the entry observed on its ref, for a candidate checked
against the base the entry read; the observation read the PRs on that ref;
an `implementation_pr` follows an observation of no open PR there and is
onto the default branch the observation records at the base; an
`adopt_pr` targets the very PR the observation recorded there. Anything
else is refused, so a plan whose push or PR no entry read explains is
never completed. A `REPLAN_REEXECUTE` completion context is held to the
same rule: it loads only for the state's transaction, with its plan of
exactly two records, the `push` of a ref the entry observation read as
absent (expected old head absent) for a candidate checked against the
base the entry read, then the `replacement_pr` from that ref onto the
default branch the observation records at the base, its transaction
marker attesting the counts the context saves. Text the recovery path
publishes with no agent result in between is validated again under the
parser's rules for its field, not only for its bounds and redaction
invariance: the `UPDATE_EPIC` roadmap section in the
completion context (`validate_roadmap_section`), the progress text of a
progress-comment record (`validate_progress_text`), the title and the
agent's part of the body of an `implementation_pr` record
(`validate_pr_title`, `validate_pr_body`), the findings and prose
sections of a `REVIEW` completion context (`check_published_finding`,
`validate_review_section`), and the title and the fixer's part of the body
of a `follow_up_issue` record (`validate_follow_up_title`,
`validate_follow_up_body`), and the title and the agent's part of the body
of a `replacement_pr` record (`validate_pr_title`, `validate_pr_body`), in
the stripped form the parser returns. A
`FIX` completion context loads only for the round being fixed, with
exactly one resolution per open finding, and each deferred finding has
exactly one source the plan and the entry observation explain: a finding
whose follow-up the observation recorded reuses it through `entry` and is
in no append block; any other names a create of its own marker or an
append whose block carries it.
Controller text stored with them is checked to be exactly what the
controller renders: a `follow_up_issue` body is the fixer's text, the
controller's reference to the PR and finding and the finding's marker, in
the repository of its owner's PR; an `implementation_pr` body ends in a blank line and
the closing block of its owner's issue (`Closes #n`, a blank line, the
implementation marker), a `replacement_pr` body is the agent's text, that
closing block and the transaction marker, each after a blank line
(`replacement_pr_body`), and the marker attests passed tests, an
`adopt_pr` block is that closing block, both
PRs are in the issue's repository, the one `Closes #n` names, and a
`review_comment` body is byte-for-byte the comment `review_comment_body`
renders from the `REVIEW` context saved with it, at the HEAD, base and
merge base its marker binds, and passes `review_comment_problem` as a
whole, so no text in it, and no composition of its fields, escaped the
parser. The
existing body an `adopt_pr` appends to is a human's text and keeps only
the append checks. A stored value the result path would have refused is a
`StateError`, never rewritten. LOCAL
state carrying any of them is refused as corrupt: a LOCAL run performs no
external effect.

### Validation evidence

Candidate validation (#172, ADR 0006; `src/autoforge/evidence.py`) adds
six fields, in REMOTE and LOCAL state alike:

- `validation_records`: one `ValidationRecord` per validator the
  controller launched, oldest first, each with a `seq` that increases
  strictly and is never reused. A record holds the candidate (REMOTE:
  `kind: remote`, repository, object format, commit and the tree read
  from the commit's authenticated bytes, plus a provenance that is
  displayed and never compared; LOCAL: `kind: local`, the workspace
  fingerprint, the workspace-policy digest and the run id), the
  definition (its index, its argv redacted, its `definition_digest` over
  the argv as configured, its scope), policy P (`policy_digest`, mode,
  timeout), the outcome and the `run_log` step directory. An observed
  record (`PASS`, `FAIL`, `TIMEOUT`) adds the enforcement facts the
  executor returned (`enforced`, backend, version, network, teardown;
  `enforced: false` under `sandbox.mode: disabled`), the exit code (`null`
  for a timeout), the start and finish times, ADR 0002's leftovers and the
  output: a tail of each stream of at most 2,000 characters, redacted
  over the whole capture before it is cut (a secret straddling the cut is
  not half-kept), every control character but newline and tab escaped
  (`\x1b`, `\u2028`), cut on whole escapes, with the stream's byte count
  and whether the capture or the cut dropped anything. A LOCAL record adds
  `start_fingerprint` (its candidate's) and `end_fingerprint` (the walk
  after the command; the next command's start). An `INTERRUPTED` record
  is built from a marker alone: the marker's fields and `recovered_at`,
  no enforcement, exit code or output. It claims no launch and is never
  evidence. A launch, setup or materialization failure leaves no record.
- `validation_attempt`: the attempt marker, saved immediately before a
  launch (after the preflight, the sandbox gate and the materialization,
  so a refusal by one of them writes none), and cleared in the same save
  that appends the launch's record, or in the save after a launch
  failure. `{}` when none is pending.
- `review_evidence`, `review_certificate`, `producer_validation_failure`
  and `validation_override_seq`: the review binding, the review
  certificate, the producer failure counter and the operator's override
  watermark. #172 validates them on load and keeps the records they name;
  #174 issues the certificate (`certificate.py`;
  [workflow.md](workflow.md#candidate-validation-gates)) in the save that
  enters `READY_FOR_MERGE` or LOCAL `DONE`, copying the candidate, the
  records and the set and policy digests from the round's binding, and a
  clean round it cannot certify exactly blocks in that save instead. The
  merge gate requires it to apply to the live PR revision on every pass
  (#175; [github-safety.md](github-safety.md#pre-merge-verification-and-its-failure-classes))
  and writes nothing to it. A certificate is never cleared or rewritten by
  a later round, a drift or a merge: one of an earlier round no longer
  applies, and old records and certificates stay as history until evicted
  or cleared. #173 writes the other three
  ([workflow.md](workflow.md#candidate-validation-gates)): the `REVIEW`
  entry saves the binding with the reviewer's launch (a relaunch for the
  same round, head, set and policy reuses it); a REMOTE producer's failure
  saves the counter with the failing record in one write, and every save
  in another phase clears it, so it counts one phase entry; an applied
  REMOTE `unblock` sets the watermark to the highest retained seq (0 for
  none) in its own write, and nothing else sets it.
- Recovery: a crash after a failing record's save resumes with the count
  it saved, and one after the save that reached the bound blocks at the
  next entry without relaunching. A crash after a `PASS`, before the plan,
  reuses the record when the agent hands back the same commit. A record
  is never rewritten to change what a gate decides; only the operator's
  watermark makes a failure historical.

**Bounds** (D10.3, `evidence.py`'s constants, part of the protocol): at
most 64 records; at most 16 definitions per set and so 16 records per
binding or certificate; an argv of at most 32 elements, stored redacted at
most 3,072 characters an element and 12,288 in all (the configured 1,024
and 4,096 times redaction's growth factor); a seq at most 2^31-1; a byte
count at most 2^53-1; an exit code in [-128, 255]; timestamps ISO 8601 UTC
of at most 32 characters; repositories, refs, run logs, backends and
teardown text each bounded. A writer builds every value through
`evidence.py` and checks it with the loader's parser before it is saved,
so no value is written that would not load.

**Integrity** (D11.1), checked on every load with the rest of the state:
each record's exact key set for its outcome and kind, every field against
its bound and format, and its `digest` (SHA-256 over the canonical JSON of
every other field), so a record edited by hand fails the load. A record
of the other mode's kind, another repository, another run, or a run log
outside this run's directory is refused, as are a mixed candidate and a
seq out of order. The binding and the certificate name records by
`(seq, digest)`: each must be retained, under that digest, not
`INTERRUPTED`, for the same candidate identity and policy, and at its
definition's index. A binding's only non-`PASS` may be its last record
(the set stops there, D10.6), and a binding of `PASS` records only (none
for an empty set) carries their set digest; a certificate's records must
be `PASS`, its set digest theirs, its comment canonical, on the reviewed PR and keyed by the
certificate's own round, head, base and merge base, and its own digest
must match. The counter names a retained `FAIL` or `TIMEOUT` of its
commit. The watermark lies in `[0, highest seq]`; LOCAL has none. Anything
else is a `StateError`: never clipped, defaulted or repaired.

**Retention** (D11.4): a record past the 64th evicts the oldest one no
binding, certificate or counter names; a named record is never evicted.
Nor is a record of the set being run (#173): a gate that reuses some of a
candidate's records and runs the rest pins both while it appends, so the
binding or counter it writes next never names an evicted record (at most
3 x 16 + 1 records are pinned, below the bound).

**Read-only load** (D11.3): loading reports a pending marker
(`status` shows it as "Pending attempt", the dry-run plan as a note) and
converts nothing, so `status` and `--dry-run` leave the file byte-identical.
A real step converts it under the lock before it plans anything: a plan can
refuse (a LOCAL working tree the interrupted validator left a FIFO in), and
a refusal must not leave the marker pending for every resume after it.

State keeps compact finding summaries and review comment URLs, rather than
copying unbounded PR discussion bodies. Those summaries are bounded
(`loop_guard.MAX_PERSISTED_FINDINGS_PER_ROUND`, 100 findings per round,
above the 50 findings the parser accepts; `MAX_REQUIRED_RESOLUTION_CHARS`,
6000 characters per required resolution, which is the parser's
2000-character bound times the most redaction can lengthen a text). A round
that hits either bound is marked `evidence_truncated`, and because
superseding a PR deletes the controller's only record of its findings, such
a round makes the replan policy block for a human instead. Because the
parser refuses an oversized review before it is recorded, only state written
by an older controller or edited by hand can carry that mark. Relevant
controller verification failures are retained as a bounded per-issue list
(`verification_failures`) and supplied to the replan prompt; all PR comment
text remains GitHub audit data rather than state payload.

State writes must be atomic.

Preferred pattern:

```text
write temp file
flush/fsync when appropriate
atomic replace
```

A corrupt existing state file must fail loudly. Never silently replace corrupted state with a fresh run.

As implemented: temp file + fsync + `os.replace`. `run`, `resume`, `step`
and `status` all exit 2 on an unreadable or foreign-protocol `state.json`
(bad JSON, unknown phase, invalid UTF-8, dangling symlink, or a non-regular
entry such as a FIFO, socket, device or directory; the entry is inspected
and opened non-blocking, so a FIFO never hangs the command).

`run --force` moves the entry aside as `state.json.corrupt-<timestamp>`
instead of deleting it (a symlink is archived as a link; its target is never
touched; a directory cannot be archived and is refused). The archive name is
reserved with `link(2)`, which never replaces an existing archive; on a
filesystem without hard links (vfat/exFAT, some FUSE, SMB and overlay
mounts) the entry is copied into an exclusively created name instead, with
the source's permission bits regardless of the umask where the filesystem
holds any (where it holds none and refuses `chmod` as it refuses `link`, the
copy is made anyway and is at most as permissive as the source, never more;
a FIFO, socket or device cannot be copied and is refused there); a copy that
fails at any point after its name exists removes that name again, so a
failed archive never sits beside an untouched original. The original is
unlinked only after the archive exists, so a crash in between leaves both,
and the next `run --force` archives the leftover again under a new name: a
duplicate copy, never a lost one. `run` inspects, decides, quarantines,
writes the first state and executes it under one continuous controller
lock, so a concurrent controller can never be quarantined or overwritten on
a stale verdict, nor slip in between the first save and the engine loop.

The `protocol_version` is the state file's schema, the nested replan journal included, and it is what tells an old-controller file from a corrupt one: a field a controller of *this* protocol always writes is corruption when absent, so a schema change that tightens what a stage requires must bump the protocol rather than let the old shape be diagnosed as corruption. Old-controller compatibility is decided at the state boundary by the version label, never by shape, and it is explicit and tested per stage: protocol 1 → 2 added the review decision's PR and issue binding to the replan journal, so a protocol-1 file with no replan in flight (an empty or `REJECTED` journal) is loaded and relabelled, while one with an in-flight journal is refused with its stage, PRs and the fate of the source PR named, and never migrated by filling the decision from the run's current PR and issue (that is the rebinding the fields exist to forbid), and never handed to the journal loader to be called corrupt. Protocol 2 → 3 added the reviewed PR and base branch to the review binding (`reviewed_pr_url`, `reviewed_base_ref`, `current_base_ref`) and the reviewed base to the replan journal (`decision_base_ref` at `PENDING`, `source_base_ref` at `PREPARED`): a protocol-2 file with a replan in flight is refused exactly as a protocol-1 one is, naming the base as the binding it lacks instead of the PR and issue, and never migrated by filling it from the source PR's current base; a protocol-2 (or protocol-1) file with no replan in flight and in any phase other than `READY_FOR_MERGE` / `MERGE` is loaded and relabelled, because the next review writes the binding, while one parked in those phases is refused with the PR and HEAD named, and never migrated by filling the binding from the run's current PR (the same rebinding rule). Protocol 3 → 4 added the reviewed merge base to the review binding (`reviewed_merge_base_sha`, `current_merge_base_sha`; both validated on load as a full lower-case SHA or empty, matched in full so a trailing newline is not a SHA) and to the replan journal (`decision_merge_base_sha` at `PENDING`, `source_merge_base_sha` at `PREPARED`), so both rules apply to a protocol-3 file exactly as they apply to a protocol-2 one, for the merge base: one with a replan in flight is refused with its stage, PRs and the fate of the source PR named, naming the merge base as the binding it lacks, and never migrated by reading the merge base GitHub reports now (the base may have been rewritten since the review, which is exactly what the field exists to detect), and never handed to the journal loader to be called corrupt; one with no replan in flight and parked in `READY_FOR_MERGE` / `MERGE` is refused, naming the merge base as the review binding it lacks, and never migrated by reading the merge base after the fact for the same reason; in any other phase with no replan in flight it is loaded and relabelled, because the next review writes the binding. Protocol 4 → 5 added the close watermark to the replan journal (`source_closed_event_count` present and `close_attempts` non-zero at `SUPERSEDE_INTENT`, #69) and changed nothing about the review binding, so the rule is scoped to the stages whose record changed (`replan_txn._LEGACY_GAP_STAGES`): a protocol-4 file whose journal has reached `SUPERSEDE_INTENT`, `COMPENSATING` or `SUPERSEDED` is refused in every phase, naming the closed-event count and attempt count as the binding it lacks, and never migrated by reading the count GitHub reports now (that would record the very close the watermark exists to detect as if it predated the intent, and license the retry the field was added to forbid in that case); one whose journal is empty, `REJECTED`, or at `PENDING`, `PREPARED` or `VERIFIED` is byte-for-byte what protocol 5 writes there and is loaded and relabelled in every phase, the merge phases included. `run --force` moves such a file aside as it does any unreadable one; it is never overwritten in place.

Protocol 7 → 8 (#211) added the acceptance-action state: `acceptance_outcomes`, `acceptance_comment_url` on each REMOTE `last_fix_resolutions` entry, `acceptance_source` on each `FIX` completion-context resolution, the `acceptance_comment` effect record and the optional `action` of an unblock entry. Every protocol-8 save writes `acceptance_outcomes`, so a protocol-8 file missing it is refused. A protocol-7 file loads in every phase and is relabelled on its next save, with no outcomes, `acceptance_comment_url: ""` and `acceptance_source: null`, which is exactly what a protocol-7 controller meant: it never published an acceptance comment. A protocol-7 (or older) file that carries any of these is a hand edit or a foreign file and is refused, unchanged. The number is the implementation's; it is settled on #23 and #158 before the change merges (never 6, which stays skipped).

Protocol 8 → 9 (#219) added the recorded configuration of a REMOTE run (`config_fingerprint`). Every protocol-9 save writes it, `{}` included, so a protocol-9 file missing it is refused: read as absent, it would turn the check off for the rest of the run. A protocol-8 file loads in every phase with none and is relabelled on its next save. Its acceptance state is kept as written: the protocol 7 → 8 migration above does not apply to it. The first `step`, `resume` or `unblock` records the configuration that command runs on as `upgraded` and saves it with that command's next save, and later commands are checked against it. A protocol-8 (or older) file that carries the field, `{}` included, is a hand edit or a foreign file and is refused, unchanged. The record has its own `schema` (1). Another schema is refused, never migrated, so a change to what is fingerprinted bumps both. The number is the implementation's; it is settled on #23 and #158 before the change merges.

Protocol 9 → 10 (#172) added the [validation evidence](#validation-evidence) above: `validation_records`, `validation_attempt`, `review_evidence`, `review_certificate`, `producer_validation_failure` and `validation_override_seq`. Every protocol-10 save writes all six, so a protocol-10 file missing one is refused: an absent marker read as "none" would hide the crash the resume path must record. A protocol-9 file, and every older file its own rules load, loads in every phase with all six at their empty values (no record, no marker, no binding, no certificate, no counter, `null` watermark), fabricating nothing, and is relabelled on its next save. A protocol-9 (or older) file that carries any of the six, even empty, is a hand edit or a foreign file and is refused, unchanged. The records have their own `schema` (1). The number is the implementation's; it is settled on #23 and #158 before the change merges.

Protocol 10 → 11 (#243, ADR 0004 D13.10) added the operator-action state: `operator_comment_outcomes`, `operator_targets` in a `FIX` entry observation, `operator_sources` on each `FIX` completion-context resolution, `operator_evidence` on each REMOTE `last_fix_resolutions` entry and the `operator_comment` effect record, whose `binding` is part of the same bump. Every protocol-11 save writes `operator_comment_outcomes`, so a protocol-11 file missing it is refused. A protocol-10 file, and every older file its own rules load, loads in every phase and is relabelled on its next save, with no outcomes, no `operator_targets` (that entry offers no operator action), `operator_sources: null` and `operator_evidence: []`, which is exactly what a protocol-10 controller meant: it never asked the operator for anything. Its K9 state is kept as written. A protocol-10 (or older) file that carries any of these, even empty, is a hand edit or a foreign file and is refused, unchanged. The number is the implementation's; it is settled on #23 before the change merges (#158 is closed).

Protocol 5 → 7 (#160) added the effect state above (`effect_records`, `entry_observation`, `completion_context`, `launch_label`). Protocol 6 is skipped, and stays skipped for good: #23 once reserved it for #126, which now takes the next number like every other bump (see "Numbering a new protocol" below). The number this bump took was settled on #23 before it merged. State persisted under protocol 5, or under any older protocol that its own rules load, carries no effect state and loads as none; a legacy file that does carry one of these fields is a hand edit or a foreign file and is refused. A legacy REMOTE file with `attempt >= 1` gets the launch label `agent_publishes` for a phase whose agent published under the previous contract, so that the first entry of this version knows an agent may already have published (D13.3). `UPDATE_EPIC` is the one such phase whose publication this version performs itself: its one-shot legacy re-entry adopts the progress comment that agent posted, with no record, and asks for the selection only (D13.7; [github-safety.md](github-safety.md), "Before UPDATE_EPIC"). LOCAL state, whatever its protocol, carries no effect state.

**Numbering a new protocol.** A change that adds or changes persisted state takes N + 1, where N is `__protocol_version__` on `main` when its PR is ready to merge, and it makes that bump alone: two independent persisted changes never share one. Before the PR merges, its issue announces N → N + 1, the new fields and the migration rule on #23, the home of protocol numbering. If `main` moves past N first, the PR rebases, takes the new N + 1 and says so there; after the merge, #23 records the squash commit. No number is reserved ahead of time. Label 6 stays skipped: never written, and refused as unknown like a future label (`tests/test_state.py`, `test_protocol_6_and_other_unknown_labels_are_refused_and_left_unchanged`). Each bump states its migration rule in this section, in the form above: what a file of the previous protocol loads as, what an older-labelled file carrying the new state is refused as, and which new field a file of the new protocol must carry.

---

## Idempotency and crash recovery

Assume the process may crash after an external side effect but before local state is persisted.

Examples:

- a PR may already have been created
- a review comment may already exist
- a fix may already have been pushed
- a PR may already have merged

Resume logic must inspect actual Git/GitHub state before repeating destructive or duplicative actions.

A controller effect (ADR 0004) is resumed from its record and never re-sent blind. Every crash window has one answer: a crash before the intent is saved means nothing happened and the step re-runs; an `intended` or `attempted` record is reconciled by its identity before anything is sent, so a write that landed before the save is adopted as `observed` (no second write) and one that did not is issued again within the attempt bound; an object that a human created or changed between the intent and the write, a conflicting identity, or two marker-bearing objects make the record `conflict` (`BLOCKED`, naming the object); an exhausted bound is `BLOCKED` naming the record and the manual step; and a transient GitHub failure during reconciliation leaves the state unchanged for `resume`. A phase whose effect landed finishes from its completion context without relaunching its agent. `FIX` (#163) is the case with the most windows, and each has one outcome: a crash before the plan is saved sent nothing, so `resume` launches the fixer again, against the entry observation saved before the first launch (a follow-up issue found later that it does not explain blocks); a crash after the plan is saved, at any stage of any create, append or push, completes the plan from the journal without relaunching the fixer, each record adopted or issued at most once in plan order; a PR head at the planned candidate is the controller's own push and never takes a `REVIEW` detour; and a head that is neither the reviewed HEAD nor the candidate is someone else's push (`FIX -> REVIEW`, the plan dropped, no fix recorded). An acceptance comment (#211) adds its own: a crash after the plan save, before the `BLOCKED` save, re-reads the issue at `resume` and blocks again with nothing sent and no launch; an approval saved by `unblock` is published by the next `resume` (`attempted` saved first); a comment that landed before its `observed` save is adopted, never posted twice, as is the exact payload an operator posted; a marked comment with other bytes, or two, is a `conflict`; a comment without the marker is not evidence and the action keeps waiting; a second approval, or one with a stale or mismatched id, is refused with the state byte-identical; a push to the PR while it waits refuses an approval or a decline as stale, and a plain `unblock` routes the drift to `REVIEW`, recording the action `stale`; a closed issue, or one outside the run's repository, holds the comment and the adoption of an exact one, landed or posted, with nothing later in the plan sent (PR #221 R1-F2), also when the issue stops qualifying between the comment's write and its read-back: that step blocks with the eligibility reason, the record stays `attempted` and the reason says the comment was sent (PR #221 R2-F1); and a K9 already `observed` is never sent again while the rest of the plan completes. Operator actions (#243, ADR 0004 K10) are never sent at all, so their windows are reads: a crash after the plan save, before the `BLOCKED` save, re-reads every target at `resume` and blocks again with nothing sent and no launch; each record's `observed` (or `conflict`) is saved before the next target is read, so after a crash between two of them the next pass re-reads only the records still pending; a binding saved by `unblock` is verified by the next `resume`, which completes the rest of the plan; a decline is one save (plan dropped, outcomes appended, history entry), so it either happened or the state is unchanged; and a push to the PR while they wait refuses a binding or a decline as stale, and a plain `unblock` lets `FIX` route the drift to `REVIEW`, recording each action `verified` (already observed, or found by a read-only reconcile that saves nothing) or `stale`, never posted. `REPLAN_REEXECUTE` (#164) has the same split: a crash before its push and PR plan is saved sent nothing, so `resume` launches the replan agent again on the base the entry observation recorded (a replacement branch, or a PR carrying the transaction's marker, found later is not the controller's and rejects the transaction); a crash after it, before the push, between the push and the PR or after the PR was created, completes the plan without relaunching the agent, and the PR is created only while the derived branch still holds the pushed candidate. The journal never stands in for GitHub on replay: before the completion sends anything it re-reads the PR's base and merge base (moved: the same `REVIEW` route, findings carried), its head repository and branch (no longer the plan's target: `BLOCKED`), and the open issues against every resolution, including follow-ups already journaled as `observed` (closed, unmarked, duplicated, or a marker the controller did not write: `BLOCKED`). The run log's `events.jsonl` is never read by any recovery path.

A validator (#172, ADR 0006 D10.7, §2.16) has its own windows. A crash before the attempt marker is saved recorded nothing, and the caller runs the definition again. A crash with the marker saved, the validator running or already exited, leaves the marker: the next `step` or `resume`, under the lock and before anything else of the step, appends an `INTERRUPTED` record built from the marker alone and clears the marker in the same save, and the definition runs again when its phase does. An exit 0 the controller did not record is therefore never a `PASS`. The record and the marker clear are one save, so a crash between them is impossible; a marker whose record exists (only a hand edit makes one) is cleared and nothing is appended, which also makes a duplicate resume idempotent. More than two `INTERRUPTED` records for one candidate, definition and policy (those at or below the operator's override watermark not counted) enters `BLOCKED` in the save that writes the third, naming the candidate and the definition. A launch or setup failure clears the marker in the next save and writes no record.

The review certificate (#174, ADR 0006 §2.16) has two windows. A crash after a clean round's comment was published and before the state was saved is #162's: the journal completes the round from the observed comment, with no relaunch and no new comment, and the certificate is issued from the comment the journal observed and from the binding the plan was saved under, empty set included, in the same save that enters `READY_FOR_MERGE`; a set or policy that changed in between makes that save `BLOCKED` instead, still holding the certificate (D12.6). A crash before that save (or before the LOCAL `DONE` save) wrote neither the certificate nor the transition, so the next step completes the round again. The certificate and its transition are one write, so a crash between them is impossible.

The merge gate (#175, ADR 0006 D12.4, D13, §2.16) holds no state of its own across a crash: each `_verify_pr_for_merge` pass reads the PR again and checks the certificate against what it read. A crash after the `READY_FOR_MERGE -> MERGE` save runs the whole gate again in `MERGE`, certificate included, so a configuration edited before the restart is refused there exactly as at the gate pass. A crash while a `merge.verification_commands` command runs, or after it ran and before its result was saved, caches nothing (`premerge_verified_*` records an observed pass only): the next pass checks the certificate again and runs the command again. A crash after the merge call and before its save finds the PR MERGED at the next `MERGE` step, which reconciles and counts it once, with no second merge and no certificate issued, rewritten or required: counting a merge already made is not a merge decision. A restart with an applicable certificate and an unchanged PR merges as if nothing had happened. A push that lands while the `REVIEW` entry validates the head leaves records that stay true for the commit they ran on; the reviewer reviews that commit, the re-read after the review finds the push, and the round is stale with no certificate, so the next round validates and certifies the new head.

A state saved before certificates existed (protocol 9 or older, migrated with every evidence field empty) is handled per phase. In `REVIEW` the round validates the head at its entry and certifies its own clean result. In `READY_FOR_MERGE` or `MERGE` with an OPEN PR the gate refuses with no applicable certificate (`BLOCKED`, conclusive, nothing run or merged), and `unblock`, as from any `BLOCKED` with a clean review and none held, decides `REVIEW` for a fresh validated round. A legacy PR GitHub already reports MERGED is reconciled and counted as before, with no certificate.

If recovery cannot determine the safe state with sufficient confidence, enter `BLOCKED` rather than guessing.

`BLOCKED` is left only by `autoforge unblock`, which re-runs the same
recovery inspection against live GitHub and re-enters the one phase it
supports through `validate_transition`, or refuses and leaves the state file
untouched when the safe state still cannot be determined (`workflow.md`,
"Leaving BLOCKED"). The operator's reason is recorded; it is never the
evidence the decision rests on.

Merge counters must be idempotent. Track which PRs have already contributed to the counter so a resumed workflow cannot count the same merge twice.

---

## Locking

Only one AutoForge controller instance may operate on a repository at a time.

Use a repository-scoped lock backed by reliable OS-level locking, keyed by the repository identity (the git common dir, e.g. `<repo>/.git/autoforge/controller.lock`) rather than by a caller-selectable path such as the state directory: different `--state-dir` values, invocation directories or linked worktrees of one checkout must all contend for the same lock.

A second controller must fail clearly rather than run concurrently.

As implemented (`flock(2)`, `src/autoforge/locking.py`):

- The lock is `<git common dir>/autoforge/controller.lock`, resolved with
  `git rev-parse --git-common-dir` from the controller's working directory,
  so two controllers cannot escape each other by passing different
  `--state-dir` values, by starting from different subdirectories with the
  default relative `.autoforge`, or by using a linked `git worktree` of the
  same checkout. A second instance exits with `LockError`. Two independent
  *clones* of one GitHub repository are two repositories to this lock and
  are not coordinated.
- A working directory outside a git repository has no lock to take and is
  refused (exit 2, nothing written), so `run`, `step` and `resume` require
  one (a REMOTE dry-run does not).
- `run`, `step` and `resume` take the lock **before** `state.json` is read
  or created and keep it until their last step has been persisted, so a
  state snapshot loaded before the lock is never executed and no second
  controller can take over the repository mid-command. Dry-run takes no lock
  and writes nothing. A REMOTE `run --dry-run` runs no `git`. `step`,
  `resume`, `unblock` and `status` without `--state-dir`, dry runs
  included, first run two read-only `git rev-parse` calls (`--git-dir`,
  `--git-common-dir`) to find which run is meant (`cli._bind_existing_run`:
  a remote one under `state_dir` or a local one in the git directory);
  `--state-dir` skips that lookup. A LOCAL dry run needs a git repository
  and reads it: `local run --dry-run` reads HEAD, branch and the workspace
  walk to build its plan, and a dry run of an existing local run reads the
  repository to check its run contract and current fingerprint.
- Below the git common dir no path component is followed through a symlink:
  the `autoforge` directory is created with `mkdir` and opened relative to
  the git directory's descriptor with `O_DIRECTORY | O_NOFOLLOW`, so a
  symlink or a file in its place is refused (exit 2) and its target is never
  touched; `controller.lock` is then opened relative to that validated
  directory descriptor. The lock entry itself must be a regular file with a
  single name: `controller.lock` is opened with `O_NOFOLLOW` and
  `fstat`-checked, so a symlink, FIFO, socket, device or directory in its
  place, or a hard link that shares its inode with another file, is refused
  with `LockError` (exit 2) before anything is written: a tampered path or
  entry can neither redirect the PID write to another file nor produce a
  traceback.
- These checks cover entries that are damaged or tampered *at rest*; a
  writer with write access to the git directory who races the
  check-then-write window, or swaps the `autoforge` directory for another
  directory between two controller invocations, has the controller's own
  privileges and is outside the trust boundary (the git directory is assumed
  to be writable only by the operating user).

**The parallel EPIC scheduler generalizes this invariant; it does not remove
it** ([ADR 0008](../adr/0008-parallel-epic-scheduler-authority-run-identity-claims-and-locks.md),
D10–D12, D17.2). #180 implements the three lock domains in
`src/autoforge/locking.py` and the ownership guards in
`src/autoforge/ownership.py`; #182's scheduler (`src/autoforge/scheduler.py`)
shares L0 with its workers and hands each one its issue's L2 (Scheduler
workers and supervision, below), and #183's `epic run` and `epic resume`
start it (The `epic` commands, below); a standalone command holds L0 and
takes L1:

- **L0: one orchestration authority per git common dir.** This lock is L0,
  unchanged for a standalone command (including its `LOCK_UN`-then-close
  release); its refusal now adds "an AutoForge scheduler or its surviving
  workers may hold it" (exit 2). A scheduler invocation (`epic run`, `epic
  resume`, `epic archive`) holds it together with the workers it launched.
  It switches to a **close-only release** (`ControllerLock.share`) before its
  first spawn, so neither its return, an exception nor SIGKILL unlocks the
  open file description its workers inherited (`pass_fds`): `LOCK_UN` on any
  copy would release it for every holder at once. A worker never acquires
  L0: it adopts the inherited descriptor (`ControllerLock.adopt`) only after
  the fresh-descriptor proof (`locking.prove_held`): the expected
  coordination root opened afresh (`O_NOFOLLOW | O_CLOEXEC`) names the same
  `(st_dev, st_ino)`, and `flock(LOCK_EX | LOCK_NB)` on that fresh
  descriptor fails with `EWOULDBLOCK`. No environment variable, PID record
  or claim authorizes adoption. An adopted lock is close-only, is made
  non-inheritable and never writes the PID record, which keeps naming the
  scheduler for diagnostics only.
- **L2: at most one worker per issue.** `IssueRunLock`,
  `<git common dir>/autoforge/scheduler/issues/<n>/run.lock`, is the only
  proof of a live owner. Every directory on its path is opened
  `O_NOFOLLOW`, the file `O_NOFOLLOW | O_CLOEXEC`; it must be a regular
  file with one link, and it is created once and never written, truncated
  or unlinked. Only the L0-holding scheduler acquires it, `LOCK_EX |
  LOCK_NB` only: a held one is `IssueBusy`, never a wait. It is handed to
  exactly one child, every release (scheduler or worker) is `close` only,
  and a worker adopts it with the same proof as L0. The lock-free probe
  `issue_run_lock_live` opens it read-only without `O_CREAT` and tries
  `LOCK_SH | LOCK_NB`: a missing file is no owner, `EWOULDBLOCK` is a live
  one. Every write to a per-issue state happens under that issue's exclusive
  authority: L2 for a worker, L0 for a standalone command.
- **L1: controller writes to git's shared data in the common dir are
  serialized.** `SharedGitLock`, `<git common dir>/autoforge/git.lock`
  (hardened like L2), is held around exactly the writes of ADR 0008 D12.1:
  the agent worktree's check-then-create (`_ensure_agent_worktree`), the
  controller's fetch (taken inside `GitTransport.fetch`, so every caller,
  the pre-merge fetch and `read_commit`'s re-fetch included, holds it), the
  strict import's `index-pack` alone, and the REMOTE creation of the
  issue's sandbox directories (`_sandbox_dirs`), the shared
  `autoforge/sandbox` parent included, but not the launch after it. Each of
  those git processes runs with automatic maintenance off
  (`git_transport.NO_AUTO_MAINTENANCE`: `gc.auto=0`,
  `maintenance.auto=false`). It is the one blocking acquisition: a poll
  bounded by `SHARED_GIT_LOCK_WAIT_SECONDS` (the fetch's 600 s plus 60 s)
  whose expiry is the transient `GitLockTimeout`, which the guarded
  operation reports as its own failure (the fetch's `GitTransportError`, the
  import's failure, the worktree creation's "Nothing was launched; 'resume'
  tries again", the sandbox directories' `SandboxLaunchRefusedError`). Reads
  take no lock. LOCAL's sandbox cache in its own git directory takes no L1;
  the PID record is covered by L0.
- **Order L0 → L2 → L1.** L0 and L2 are only ever taken non-blocking, and
  the holder of L1 takes nothing while holding it, so no wait-for cycle can
  form; a standalone command takes L0 then L1 and never L2. A debug
  assertion (`LockOrderError`, a controller bug) refuses, while this
  process holds L1, any lock acquisition, agent launch, GitHub call or
  state save.
- **Filesystem.** In scheduler mode only, `check_local_lock_filesystem`
  refuses (`NetworkFilesystemError`) a coordination root, and every
  `run.lock` and `git.lock` descriptor, on NFS, CIFS/SMB, 9p, AFS, a
  cluster filesystem or any FUSE filesystem (Linux `statfs` magic, macOS
  `f_fstypename`), and one it cannot identify: the live-ownership proof
  needs local kernel locks. Standalone commands never check.
- **No stale-lock cleanup.** No lock has a timeout, lease or heartbeat: the
  kernel releases each when its last descriptor closes, SIGKILL included,
  and nothing ever removes a lock file.
- **Ownership (D17.2).** L0 excludes a standalone command and a scheduler
  in time; the guards exclude them in ownership. **Forward:** `run`,
  `step`, `resume` and `unblock` refuse (exit 2, `SchedulerOwnedIssueError`
  naming `autoforge epic archive`) an issue a scheduler namespace owns,
  checked under L0 before anything is written: the namespace holds any
  `state/state.json` (loaded or not), its `claim.json` holds an open claim
  (#181), or a live owner holds its L2; a namespace or a claim file that
  cannot be read (an unknown claim schema included) owns its issue
  (fail-closed), and a claim file of closed claims only owns nothing. The
  CLI hands the standalone engine the same predicate
  (`ControllerEngine.issue_ownership`), one more rejection fact of
  `_verify_issue_selectable`, so a sequential run's `next_issue_url` naming
  an owned issue takes the bounded rejection path. A worker engine gets no
  predicate, and `unblock` of a namespace's own state directory exempts
  that namespace. **Reverse:** `ownership.standalone_hold` reports the issue
  the configured standalone state names as `current_issue_url` while its
  phase is anything but `DONE` (`BLOCKED` and `FAILED` included), which
  the scheduler holds as `held:standalone-state` (#182); an unreadable
  standalone state is reported and holds nothing.

---

## Runtime artifacts

Runtime artifacts must remain untracked.

At minimum `.gitignore` should exclude appropriate entries such as:

```text
.autoforge/
__pycache__/
*.pyc
*.egg-info/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.venv/
```

Do not commit generated controller state, logs, lock files, credentials, or local virtual environments.

The state directory defaults to `.autoforge/` in the working directory for
a REMOTE run (overridable via `--state-dir` or `state_dir` in config) and to
`<git dir>/autoforge/state` for a LOCAL run, outside the tree it
fingerprints, since its own writes would otherwise keep invalidating its own
review. Either way it must be on a filesystem with hard links:
`SafeRoot.create_exclusive` publishes a complete, fsynced temporary by
`link(2)` and deliberately has no fallback that writes the final name
directly (ADR 0001, known limitation 8), so on vfat/exFAT and some FUSE,
SMB/CIFS and overlay mounts it raises `HardLinksUnavailable`, and the
run-log probe (before every launch) and `doctor` refuse with the
requirement named (#120). Same layout either way:

```text
<state dir>/
    state.json          # persisted run state (atomic writes)
    state.json.corrupt-<timestamp>   # unreadable state moved aside by 'run --force'
    logs/<run-id>/
        events.jsonl                       # one line per agent invocation, append-only
        <seq>-<phase>-<attempt>/
            progress.log                   # agent steps: live progress lines, appended while the
                                           # agent runs (published before the launch; bounded, redacted)
            request.json                   # profile, model, effort, command, limits (idle timeout,
                                           # max runtime; the wall-clock timeout of a command),
                                           # an agent's loop detection settings, the names of the
                                           # variables the controller set (env_set, #165), the
                                           # configured sandbox.mode (sandbox_mode, #168)
            prompt.md                      # rendered prompt (redacted)
            execution.json                 # exit code, timing, timed_out, the limit that fired
                                           # (idle / max_runtime / loop), configured limits, last
                                           # activity, truncation, leftovers, error; for an agent
                                           # also ended_by (exit / idle / max_runtime / loop), loop
                                           # detection settings, loop warnings and the loop report
                                           # (names, counts and times only), provider_summary with
                                           # the loop_* calibration fields; the outer sandbox's
                                           # facts (sandbox) for a launch that carried one, a
                                           # refused one included (enforced: false, the reason)
            stdout.log / stderr.log        # redacted; head + marker + tail past the capture bound
            control-result.json            # parsed CONTROL_RESULT (when valid)
    read-context/<run-id>/                 # REMOTE only (#165)
        <phase>-<attempt>-<token>/         # one per launch, written before it: the GitHub data its
            README.md                      # prompt names (index), issue.md, pull-request.md, ...
                                           # (redacted, bounded); never read back by the controller
```

The read context is the agent's copy of what the controller read from
GitHub for one launch (ADR 0004 D10; [GitHub safety](github-safety.md#the-agent-read-path)).
It is written through the state directory's capability like a run log, is
not state (nothing in `state.json` points at it and recovery never reads
it), and is kept, as logs are, so an operator can see what an agent was
handed. A dry run and a LOCAL run write none.

The controller also keeps runtime artifacts inside the repository's git
directories, which git never tracks: in the git common dir the controller
lock (`.git/autoforge/controller.lock`) and, in REMOTE mode, one detached
agent worktree per issue (`.git/autoforge/worktrees/<issue-number>`, unless
`execution.worktree_dir` points elsewhere; a configured location must lie
outside the checkout's working tree). A worktree is created detached at the
checkout's HEAD at the first agent launch for its issue, reused as the agent
left it by every later phase of that issue
across `resume`, and never deleted, moved or pruned by the controller; the
operator removes it with `git worktree remove` when the issue is done. A
path that exists there but is not a worktree of this repository is refused,
never adopted: a symbolic link at the path is refused on the entry itself,
whatever it points to (a link to the operator's checkout or to another
worktree would otherwise answer git as that tree), a directory is
reused only when the repository's registry lists it as a worktree root
(read from `<common>/worktrees/*/gitdir` without git, so no worktree's
`HEAD` is read and no link followed, the registry directory's own
included, #223), and a
path reached through a symbolic link above it (`.git/autoforge/worktrees`
linked into the working tree, say) is refused before anything is created,
so a worktree only ever appears at the literal derived path. Reuse also
requires the registry to bind the worktree to its admin dir, in every
`sandbox.mode` (ADR 0005 D6.4): exactly one `<common>/worktrees/<id>/gitdir`
names `<worktree>/.git`, and the worktree's `.git` is a regular file naming
that entry. A worktree or repository moved by hand breaks the binding for
git too; the step then refuses with a `VerificationError` naming the
inconsistency and `git worktree repair` (run from the repository, or
`git worktree repair <worktree>`) before anything is launched, so repair
it and `resume`. An agent that rewrites its own `.git` can only make
this check refuse, never make it name another worktree. A worktree created
before #168, possibly by unsandboxed agents, is reused once it binds
(D15.2).

Under `sandbox.mode: required` the controller keeps the outer sandbox's
directories beside them (ADR 0005 D8.4):

```text
<git common dir>/autoforge/sandbox/<key>/     # REMOTE; <key> is the worktree's name (<n>, epic-<n>)
    objects/          # the issue's private object directory: the upper layer of the agent's
                      # overlay over <common>/objects, where the objects it writes land
    overlay-work/     # the overlay's work directory; never read
    cache/            # ~/.cache of ANALYZE_EXECUTE, FIX and REPLAN_REEXECUTE launches
<git dir>/autoforge/sandbox/local/
    cache/            # ~/.cache of LOCAL launches that produce a change, and of validation
```

They are created through `SafeRoot` at the first enforced launch that needs
them (a link anywhere in their path refuses that launch), lie there
whatever `execution.worktree_dir` says, and are never deleted, moved or
pruned by AutoForge: remove an issue's with its worktree. Nothing in
`state.json` names them; they are derived from the git directory and the
worktree name (D17.1), so a run resumes under either mode with no
migration (D15.3).

What an agent commits under enforcement stays in `objects/` until the
controller imports it strictly into the shared store (D6.5), which it does
in every mode before every REMOTE launch and after every launch that
exited cleanly, so a crash between a launch and its import, or a switch
back to `disabled`, leaves no commit unreadable. Until then the operator's
own `git -C <worktree> log` cannot read those commits; inspect them
read-only with
`GIT_ALTERNATE_OBJECT_DIRECTORIES='"<git common dir>/autoforge/sandbox/<n>/objects"' git -C <worktree> log`
(git splits the variable on `:` unless an entry is double-quoted, and
unquotes a quoted one C-style, so a `"` or a `\` of the path is written
`\"` or `\\`; the outer single quotes are the shell's, so a `'` is then
written `'\''`; the controller always names the directory as one such
entry, #224),
and never add the directory to the shared store's `objects/info/alternates`.
The import walks the directory first without following a link: a
directory that is not a real one reached from the common dir through real
directories (a link, another file type, one that cannot be checked, never
taken for an empty store), an `info/alternates` or `info/http-alternates`
file, or an entry that is neither a directory, a regular file nor an
overlay whiteout, is tampering (`SandboxTamperError`), and the step enters `BLOCKED` naming the entry,
with nothing read from the directory and nothing published. The import
reads the commit `HEAD` holds itself, after `lstat` found a regular file
there: a `HEAD` the agent replaced with a link fails the import before any
git process runs, nothing is packed or imported, and the candidate is
refused with it. Inspect it,
remove what does not belong there, then `unblock`. An import that fails
otherwise (a corrupt object, say) is never retried and publishes nothing:
before a launch it refuses the launch (`SandboxLaunchRefusedError`, before
anything is saved), after one it refuses the candidate, which the agent is
asked to correct.

### Per-issue scheduler namespaces

The parallel EPIC scheduler
([ADR 0008](../adr/0008-parallel-epic-scheduler-authority-run-identity-claims-and-locks.md)
D5, D17.3) gives every issue a stable identity and a directory of its own,
found again from the repository alone after a crash. #178 implements the
identity, the layout and the files it owns (`src/autoforge/run_namespace.py`);
#180 the run lock and the ownership guards (Locking, above); #181 the claim
file, the per-EPIC scheduler state and restart reconciliation
(`src/autoforge/scheduler_state.py`, below); #182 the scheduler and its
workers (`src/autoforge/scheduler.py`, `src/autoforge/worker.py`, below);
#183 the `epic` commands that start them and act on them
(`src/autoforge/epic_commands.py`, below).

- **RunKey** is `(repository_id, issue_number)`: GitHub's numeric
  repository id and the issue number, both positive integers, rendered
  `<repository_id>/<issue_number>`. Titles, URLs and the owner/name pair are
  never part of it, so a renamed repository keeps its namespaces.
- **Layout.** A pure function of the coordination root (the git common
  dir, the key the controller lock already uses) and the issue number,
  independent of `--state-dir`, `config.state_dir` and the working
  directory:

  ```text
  <git common dir>/autoforge/scheduler/issues/<n>/
      run.lock         L2 (#180): created by its first acquisition, never removed
      binding.json     autoforge.run-binding/1 (below)
      claim.json       autoforge.scheduler-claim/1: the scheduler claim (#181, below)
      state/           an ordinary state directory: state.json, logs/<run-id>/,
                       read-context/<run-id>/ (the layout above, unchanged)
      workers/         <generation>.log: one worker's stdout and stderr (#182)
  ```

  The **anchor** is the git common dir: `StatePaths` for `state/` is
  anchored there, as a LOCAL run's state is anchored at the git dir, and
  every component from `autoforge` down is opened descriptor-relative with
  `O_NOFOLLOW`. A symbolic link planted at `autoforge`, `scheduler`,
  `issues`, `<n>` or `state` refuses every write, archive and engine save
  (`UnsafePathError`) and is never followed. Two issues' namespaces share no
  file. The issue's worktree is not in the namespace: its path is the
  engine's existing derivation (`execution.worktree_dir/<n>` or
  `<common>/autoforge/worktrees/<n>`,
  `ControllerEngine.issue_worktree_path`), so a worker reuses the worktree a
  standalone run of the issue created. The scheduler root is controller
  private and outside every working tree.
- **`binding.json`**, schema `autoforge.run-binding/1`: `schema`;
  `run_key` {`repository_id`, `issue_number`}; `issue_id` (GitHub's numeric
  issue id); `repository` {`owner`, `name`} (display only); `epic`
  {`number`, `issue_id`} (the EPIC the namespace was first bound under);
  `worktree_path` (absolute, diagnostic only); `created_at` (ISO 8601 UTC).
  Loading is strict: an unknown schema version, an unknown or missing field
  at any level, a duplicated key, an id that is not a positive 64-bit
  integer, an owner over 39 or a name over 100 characters, a relative or
  over-long worktree path, a file over 16 KiB or not UTF-8 JSON, or one
  past the JSON decoder's own integer-digit or nesting limit (its
  `ValueError` or `RecursionError`, not a `JSONDecodeError`) is a
  `NamespaceError`, whose message quotes at most a bounded part of the
  file; a binding whose `run_key` names another issue than its
  directory is a `NamespaceMismatch`. It is written once, with
  `SafeRoot.create_exclusive` (fsynced temporary, then `link(2)`), so a
  crash leaves no binding or a complete one; a second write is refused and
  the first binding kept. `check_binding` compares a binding with the
  caller's expectation and raises `NamespaceMismatch` naming the first
  identity that differs (`repository_id`, `issue_number`, `issue_id`, then
  `epic` when the caller asks): the caller decides what that means
  (`held:other-epic`, uncertain, global uncertainty; D14).
- **Lookup** (`load_issue_namespace`) is read-only: it creates no
  directory, quarantines nothing and writes nothing. It returns *absent* (no
  binding, no claim file, no `state/state.json`; a directory holding only
  `run.lock`, logs or worker logs is absent), *unbound* (a claim file or a
  state without a binding) or *bound*, together with the strict,
  read-only load of `state/state.json` (absent, loaded, or corrupt with a
  redacted, bounded reason: unparseable or past the decoder's limits
  (`load_state` reports both as corruption), an unknown protocol, not a
  regular file, reached through a link) and whether `claim.json` exists. A corrupt
  state is reported for its own namespace only; a corrupt binding raises,
  for that namespace only.
- **Enumeration** (`list_issue_namespaces`) lists the issue directories
  holding a binding, then those without one, in ascending issue number. It
  refuses a directory of more than 10 000 entries while reading it, and
  reports, never opens, an entry that is not a canonical decimal issue
  number (`abc`, `011`, `0`) or not a directory (a symbolic link, a regular
  file, a FIFO).
- **Archive** (`archive_issue_state`) moves `state/state.json` aside as
  `state.json.archived-<UTC timestamp>` through a root reached from the
  common dir, with the same link-then-unlink primitive as `run --force`'s
  quarantine (`state.archive_state_file`; the `corrupt-` behaviour is
  unchanged): a crash between the two syscalls leaves both names and a
  re-run completes the move. With no state it reports nothing to archive
  and changes nothing; logs, worker logs, binding, claim and worktree are
  kept. The caller holds the issue's L2 (#180); `epic archive` (#183) closes
  the claim first (D17.4, `archive_claim`, below).
- **Engine binding.** `ControllerEngine.bind_issue_namespace(namespace)`
  points an engine's `paths` at the namespace's anchored state directory
  before the run starts, as `bind_local_state_dir` does for LOCAL, so a
  worker (#182) runs the unchanged engine there. A namespace of another
  repository than the engine's is refused; an engine never given one is
  unchanged.
- **Standalone commands refuse the scheduler root** (D17.3). `run`,
  `step`, `resume` and `local run` refuse (`SchedulerStateDirError`, exit
  2) a `--state-dir` or `config.state_dir` that resolves inside
  `<git common dir>/autoforge/scheduler/` (decided on the resolved path, so
  a relative path or a link is judged by where it leads), before the lock
  is taken and before any state is read or written; the error names
  `autoforge epic resume`, `epic status` and `epic archive` instead.
  `step` and `resume` judge the directory they were given before they look
  for their run in it or in LOCAL's location, so one in the scheduler root
  that holds no `state.json` is refused rather than giving way to a LOCAL
  run's state, and judge the directory they bind again.
  `status` reads such a state directory; `unblock` acts on it under the
  controller lock, as today, and then names `epic resume` rather than a
  standalone `resume`; the forward guard (Locking, above) exempts that
  namespace's own issue, and refuses an issue another namespace owns. A dry run is not refused: it takes no lock and runs
  no git, so it has no coordination root, and it writes nothing. LOCAL's
  default state directory (`<git dir>/autoforge/state`) is not inside the
  scheduler root and is unaffected.

No per-issue state field, protocol number or state migration comes with
any of this: the state inside a namespace is an ordinary `AutoForgeState`,
and an existing standalone `.autoforge/` or LOCAL state is never read,
moved or rewritten by a namespace function.

### Scheduler claims, scheduler state and restart reconciliation

#181 implements ADR 0008 D6, D7 and D14 in `src/autoforge/scheduler_state.py`;
#182's scheduler is its caller. **A claim is recovery data, not liveness:**
an issue has a live owner exactly when a process holds its L2. A claim
records which generation of which invocation was admitted, so a restarted
scheduler can tell an interrupted assignment from a settled one. Nothing in
the module spawns a process, reads or writes GitHub, writes or quarantines a
per-issue state or parses `CONTROL_RESULT`; timestamps and PIDs are
diagnostics, and every `detail` is redacted and bounded (500 characters).

- **`claim.json`**, schema `autoforge.scheduler-claim/1` (D7.1): `run_key`,
  `issue_id` and `epic` (copied from the binding), `next_generation`,
  `current` (the one open claim, or `null`), `history` (at most 16 closed
  claims, oldest dropped) and `updated_at`. A claim holds `claim_id` (32
  random hex), `generation`, `invocation_id`, `kind` (`start`, `resume`),
  `admission` {`snapshot_digest`, `node_digest`, `observed_at`},
  `state_ref` {`present`, `run_id`, `settlement`} at admission,
  `created_at` and `worker` (`null` or {`pid`, `process_start_time`,
  `spawned_at`, `log`}, diagnostics only). A closed claim adds `closed`
  {`at`, `classification`, `settlement`, `exit_status`, `detail`}. Loading
  is strict: an unknown schema (refused as a version before any field), an
  unknown or missing field at any level, a duplicated key, `NaN`, a value
  out of its bound, a file over 1 MiB, not UTF-8 JSON or reached through a
  link, a file holding JSON `null` (corrupt, never read as no file),
  generations out of order, a `current` that is not the last generation
  allocated, a claim id used twice or a history the closing rule forbids is
  a `SchedulerStateError`. Generations are monotonic within the file.
- **One closing rule** (D7.2). `superseded` only in the write that installs
  the replacing claim (`open_claim(..., supersedes=)`); a claim closed
  without a replacement is `abandoned` when no worker was recorded on it
  and `interrupted` when one was; `settled` needs a settlement other than
  `in_progress`; `archived` is `epic archive`'s. The rule is decided on the
  claim as it is on disk, so a caller's stale copy cannot pick the other
  class.
- **Writers.** `open_claim`, `close_claim`, `close_unready`,
  `finalize_worker` and `archive_claim` take the issue's held
  `IssueRunLock` as an argument (refused, `SchedulerStateError`, without
  it), and every write is refused while this process holds L1
  (`LockOrderError`). `record_worker` writes the diagnostics of a claim the
  scheduler just spawned, without L2 (the worker holds it), once. Each write
  compares `(claim_id, generation)` on disk with the caller's record first:
  a mismatch, or a slot already taken, is `ClaimConflict` and the file is
  left as it is, so a stale claimant cannot finalize a newer generation.
  `open_claim(..., supersedes=)` and `close_unready` also classify the
  namespace under the held L2, with the caller's `expected` identity when
  it passes one, and refuse (`SchedulerStateError`, nothing written) unless
  it is `resumable`: a settled or uncertain claim is never replaced (D7.2).
  A claim is written only over this namespace's own file: no binding, a
  claim file that does not load, or one naming another run or EPIC than the
  binding is refused. Writes are atomic (fsynced temporary,
  `replace`, directory fsync) through `safefs` from the coordination root,
  so a claim is durable before `open_claim` returns, before any worker
  exists.
- **`scheduler.json`**, schema `autoforge.scheduler/1` (D6), at
  `<git common dir>/autoforge/scheduler/epics/<n>/scheduler.json`:
  `repository` {`owner`, `name`, `repository_id`}, `epic`, `invocation`
  {`invocation_id`, `started_at`, `ended_at`, `end_reason`},
  `config` {`max_parallel` 1–4, `poll_interval_seconds` 15–3600,
  `stop_grace_seconds` 10–600, `config_digest`}, `admission` {`status`
  `open` / `closed` / `degraded`, `reason`, paired as D6 says},
  `last_snapshot` (digest, time, bounded counts) and timestamps. It holds
  run-level facts only, never a claim (a `claims` field is refused), so the
  two files cannot disagree. Loading is as strict as a claim file's; a
  corrupt one is global uncertainty, moved aside only on request
  (`quarantine_scheduler_state`, for #183's `epic run --force`:
  `scheduler.json.corrupt-<UTC timestamp>`), and a readable one never is.
- **Restart classification** (D14.2, `classify`) is pure: given #178's
  strict lookup and the claim file, read under L2, a namespace is, checked
  in this order, `uncertain(reason)` (the binding cannot be read or does not
  match the repository and issue ids the caller expects; a claim file or a
  state without a binding; a corrupt state, an unknown protocol included; a
  corrupt claim file or one of an unknown schema; a claim file or a state
  naming another run or EPIC than the binding; an open claim admitted with a
  state, `state_ref.present`, whose state is absent), `settled(settlement)`,
  `resumable(in-progress | not-started)` or `fresh`. The `present` rule
  applies only to an open claim. A state is the binding's only when its
  `repository` and the repository of its issue and EPIC URLs are one
  repository, and that repository is named as the binding recorded it at
  first admission or as the caller resolved it with the live
  `repository_id` (`ExpectedIdentity.repository`), compared without case:
  an issue number names an issue of any repository, so foreign/project's
  issue 7 under EPIC 1 is `uncertain(state-mismatch)` in owner/repo's
  namespace 7, and a rename is tolerated through the two names. A state
  naming a name the repository had only in between is uncertain too, and
  so is one whose issue or EPIC URL does not parse (`https://[`, a number
  of thousands of digits): `validation`'s parsers refuse every malformed
  URL as a `ConfigurationError`, never a `ValueError` of `urllib` or
  `int()`, so a damaged namespace is `uncertain(state-mismatch)` and never
  aborts reconciliation of the others. The
  scheduler sees a state only through `transitions.issue_settlement`
  (D15.1): `awaiting_input` (a durable pause,
  #126's, checked before the phase), then `terminal`, `ready_for_merge`,
  `handoff` or `in_progress` from the total `PHASE_SETTLEMENT` table.
- **Reconciliation** (`reconcile_epic`, D14.1) runs under L0 before any
  admission, in ascending issue number. The caller's own children are
  `owned`; otherwise L2 is taken non-blocking and a held one is `owned`
  (`observed_live`) for this pass and never reclassified. Holding L2: a
  binding of another repository is global uncertainty whatever EPIC it
  names (D14.3); a namespace of this repository bound under another EPIC
  is `held:other-epic` and left alone; an uncertain one keeps its claim
  open and is `held:uncertain` with its reason; a settled one has its open
  claim closed `settled` and is `held:settled-namespace`, never relaunched;
  resumable and fresh ones contribute no local fact and resumable claims
  stay open, for admission's supersession or `close_unready` (a snapshot no
  longer shows the issue ready). A run lock that cannot be used is `uncertain(run-lock-unusable)`;
  a binding of another repository, a run lock on a network filesystem, or a
  namespace listing that cannot be read is global uncertainty (the report
  is not `admissible`). It never spawns, writes a per-issue state or
  contacts GitHub. `only=` limits a pass to the named namespaces (the
  scheduler's re-check of those it saw live). Its facts are
  `epic_graph.LocalFact` values, the vocabulary `evaluate` consumes (D4.1,
  #179; see `github-safety.md`, "EPIC dependency graph").
- **Finalization** (`finalize_worker`): after a worker exits and the
  scheduler re-acquires L2, the claim is closed by the state the worker
  left, classified with the caller's `expected` identity when it passes
  one: `settled`, or `interrupted` / `abandoned` by the closing rule for an
  in-progress or never-saved state; anything uncertain, or an open claim on
  disk that is not the reaped worker's, leaves it open. The exit status is
  recorded and never decides.
- **Queries.** `open_claims_for_epic` (D14.4, behind `epic run`'s refusal)
  lists an EPIC's open claims and every claim file that cannot be read
  (fail-closed), lock-free and read-only. `archive_claim` (D17.4) closes an
  open claim `archived` whatever the state holds, moves a claim file the
  writers refuse aside (`claim.json.archived-<UTC timestamp>`, link then
  unlink), and otherwise changes nothing.

### Scheduler workers and supervision

#182 (`src/autoforge/scheduler.py`, `src/autoforge/worker.py`,
`src/autoforge/executor_supervised.py`, `src/autoforge/scheduler_stop.py`;
ADR 0008 D2, D9, D13, D16) is the caller of the claims and locks above.
Every crash leaves state the next start classifies; nothing here is
liveness except L2.

- **One admission** (D2.2), holding L0: L2 non-blocking (held: the issue
  is `owned` for this snapshot and skipped); under it the strict namespace
  read and `classify` (only none, `fresh` and `resumable` go on; a settled
  namespace's open claim is closed `settled`); #179's re-read of the issue
  and its blockers (`stale` writes nothing and forces a new snapshot, three
  times in a row at most: after that the next snapshot and the next re-read
  wait for the poll interval); the binding on first admission; `open_claim`
  (superseding an earlier invocation's open `resumable` claim); the worker log
  `workers/<generation>.log` opened append-only and bounded; the spawn,
  with L0 and L2 passed by inheritance (`pass_fds`) and every other
  descriptor closed; `record_worker` (pid, kernel start time, log path:
  diagnostics, never authority); the scheduler's L2 copy closed (`close`
  only), so the worker is its only holder.
- **Each snapshot** (D14.2): a namespace whose L2 was held at startup
  reconciliation, at an admission or at a `close_unready` (a status probe,
  a process outside AutoForge) is `owned` until the next snapshot, which
  reconciles it again under L2 (`reconcile_epic(..., only=)`) before its
  verdicts are used. One still held stays `owned`; one free now is
  classified as at startup, and an open `resumable` claim found only now,
  or one whose closing the held lock refused, is closed by `close_unready`
  like an earlier one when the snapshot does not show the issue ready
  (`closed`, `blocked`, `unsupported`, or no longer listed:
  `held:orphaned`). Before the scheduler ends with nothing admissible,
  each issue seen held gets one more snapshot, once per invocation, and
  one still held then is reported (its open claim left for the next
  start), never waited for. A binding of another repository found then is
  global uncertainty: admission degrades and nothing more is admitted.
- **A spawn that fails** (D2.3): a log that cannot be opened closes the
  claim `abandoned` at once (no process exists). A spawn that raised
  closes the scheduler's L2 copy and probes L2 afresh: free, the claim is
  closed `abandoned` and the issue held `held:spawn-failed` for the
  invocation; held, a child exists after all, so the claim stays open and
  the issue `owned`. A `record_worker` that fails leaves the claim open with
  no worker recorded; it is finalized after the reap like any other.
- **The worker's startup** (D9.2), each step before the next and every
  refusal an exit 2 before any state is read or written: adopt L0 and L2
  through the fresh-descriptor proof (a wrong descriptor, or one of the
  right object that does not hold its lock, is refused) and make them
  non-inheritable, so no agent launch inherits either; load the binding and
  the claim file strictly and require the open claim to be argv's
  `(claim_id, generation, invocation_id)` and both files to name argv's
  issue and EPIC; require `scheduler.json` to name argv's invocation and
  the configuration the worker loaded to have its `config_digest` (an edit
  since the scheduler started is refused); then bind the engine to the
  namespace (`bind_issue_namespace`) and run it under the adopted L0
  (`ControllerEngine.locked(held=...)`, which neither takes nor releases a
  lock). A `start` claim over a state, or a `resume` claim admitted with a
  state the namespace no longer holds, is refused with exit 2 too, once the
  engine looked for its state and before anything is written.
- **The worker's run** (D9.3): the engine with the merge gate closed and
  `MERGE` / `UPDATE_EPIC` as extra stop phases (Workflow, "Scheduler
  execution policy"); before every step L2 is proven again and the stop
  request read. Its continuation command is `autoforge [--config <path>]
  epic resume <EPIC URL>` (`ControllerEngine.continue_command`, #183),
  never a standalone `resume`,
  which refuses the scheduler root. SIGTERM is turned into
  `KeyboardInterrupt` so the executor's existing teardown of a running
  agent runs; SIGINT already is. Either ends the worker with exit 130 and
  the state as the last step saved it.
- **Reap and finalization.** The scheduler polls its unreaped children
  each tick; one that exited is finalized under a re-acquired L2
  (`finalize_worker`; a probe that holds L2 at that instant only defers it
  to the next tick). Its issue is then held for the rest of the invocation
  by what finalization classified it as. The exit status is recorded and
  never decides.
- **Stop request** (`epics/<n>/stop-request`, D16.4), schema
  `autoforge.scheduler-stop/1`: `{schema, invocation_id, mode
  (drain | now), requested_at}`. `request_stop` (#183's `epic stop`) is its
  one writer and needs no lock: it refuses when no `scheduler.json` exists
  (creating nothing), writes a fsynced temporary
  `.stop-request.<random>.tmp` and publishes it by `link(2)`, which never
  replaces: a second request writes nothing and reports the existing mode.
  It then re-reads `scheduler.json` and reports "scheduler changed; stop not
  delivered" when another invocation started meanwhile, and that an ended
  invocation's workers may still run, which `now` cannot signal. A writer
  killed before its link leaves only a temporary, which is no request.
  Every reader is strict, and a request that does not parse (a link at the
  name included) is a drain for every invocation. Only the L0 holder
  removes one: a request bound to another invocation on any tick, and an
  unparsable request or a leftover temporary at its start.
- **Exit with a live worker.** A second interrupt, the grace expiring or
  an exception ends the scheduler while a worker may still run. Its L0 was
  close-only from before the first spawn, so the worker keeps L0 and its
  L2 until it exits: no standalone `run`, `step`, `resume` or `unblock`
  runs before that (`LockError`), and the claim it leaves is classified at
  the next start.

### The `epic` commands

#183 (`src/autoforge/epic_commands.py`, the `epic` group of
`src/autoforge/cli.py`; ADR 0008 D8.3, D9.8, D14.4, D16.4, D17.4) is the
operator's surface over the files above. Every `epic` command is REMOTE
only and refuses `--state-dir` (`ConfigurationError`, exit 2): a namespace
is found from the EPIC. The operator's view, with every category and exit
code, is [Running an EPIC in parallel](../usage.md#running-an-epic-in-parallel).

- **`epic run` and `epic resume`** are one scheduler invocation each, the
  same loop. `epic run`'s guard (`epic_run_guard`, through
  `SchedulerInputs.before_start`) runs holding L0, once the repository and
  the EPIC are read and before `scheduler.json` is read or anything is
  written: `--force` quarantines an unreadable `scheduler.json` and nothing
  else, then any open claim bound to the EPIC (`open_claims_for_epic`)
  refuses the invocation (`OpenSchedulerClaimsError`, exit 2, nothing
  written), naming each claim and `epic resume`. `epic resume` has no
  guard: its start reconciliation classifies every namespace (Scheduler
  claims, above). An invocation that ended 0 with a claim of the EPIC
  still open exits 3 (`EXIT_OPEN_CLAIMS`) and names each one.
- **`epic status`** takes no lock and writes nothing: it never takes L0
  and never probes it, and it reads `scheduler.json`, the stop request and
  each namespace lock-free (`read_namespace`, `classify_for_epic`, the
  D8.3 run-lock probe). Each child gets one category, decided only from the
  snapshot, the claims, the state and the probe (`categorize`, pure, first
  match wins). A claim's worker PID and process start time are shown as
  diagnostics; nothing is signalled. An unended invocation with no live
  worker is reported as one that may have ended without finalizing. An
  `uncertain` namespace (one that cannot be read, classified or probed), a
  child's or an orphaned one, is one of the report's problems and exits 1,
  as is a `scheduler.json` whose repository id is not the one GitHub gives
  (D14.3's global uncertainty, which no namespace need show).
  The `--json` document is `autoforge.epic-status/1`, redacted, its keys
  fixed.
- **`epic run --dry-run`** takes no lock and creates no namespace, claim,
  worktree, worker or `scheduler.json`: the snapshot, the ordered
  verdicts with the lock-free local facts, and which ready issues the
  first tick would admit.
- **`epic stop`** is `request_stop` (Stop request, above) and reports its
  outcome: delivered (exit 0), delivered to an ended invocation (exit 0,
  with the workers that still hold their L2), nothing written or the
  scheduler changed (exit 1), no `scheduler.json` (exit 2). It lists the
  workers holding their L2 for an invocation that is not ended too: a
  SIGKILLed scheduler never records its end, so they get the survivors'
  caveat conditionally, never from a PID. It signals nothing.
- **`epic archive`** holds L0, then the issue's L2 (a live owner refuses
  it), and calls `archive_claim` then `archive_issue_state` (Per-issue
  scheduler namespaces, above); a namespace bound under another EPIC is
  refused. It writes no `scheduler.json` (ADR 0008 D6.1, amended by #183).
  A re-run after a crash completes the archive, and one with nothing left
  says so.
- **A recorded repository** (`scheduler.json` for `epic stop`, the binding
  for `epic archive`) is compared by its numeric id (D5.1), never by its
  `owner/name`: a rename (which a binding keeps for good) changes the name
  and keeps the id, and a repository transferred or deleted leaves its
  name to another one. So the EPIC's repository id is read from GitHub
  every time, and only the recorded id goes on; another id, or no answer,
  refuses with nothing written (`check_recorded_repository`).
- **The crash windows** (D14.5) are tested end to end by SIGKILL of real
  scheduler, worker and archive processes (`tests/test_epic_recovery.py`);
  the validator-marker row is the engine's own crash recovery.

---

## Error handling

Prefer meaningful typed errors over generic exceptions.

Useful conceptual categories include:

```text
ConfigurationError
StateError
StateTransitionError
LockError
ExecutionError
ExecutionTimeoutError
ControlResultError
ControlResultValidationError
GitHubError
VerificationError
```

Do not retry every failure blindly.

Potentially transient failures may be retried only when their failure class is
known and the retry path is bounded. Current cases include:

- a failed agent launch its adapter reported as a known transient failure
  (`transient_failure`; today an OpenCode `openai/*` run whose run-level
  errors are all `WebSocket closed with code 1000` or `1008` or OpenAI's
  generic `server_error` message), in a REMOTE run: at
  most `MAX_TRANSIENT_RETRIES` (2) relaunches per step, counted apart from
  the corrections, each after the phase-entry reconciliation, and none while
  a process the failed launch started may still be running
- temporary GitHub/network error
- malformed CONTROL_RESULT correction attempt, none while a process the
  launch started may still be running, and in LOCAL mode none after a
  launch that moved the git anchor

Do not blindly retry semantic or safety failures such as:

- real test failures
- repository mismatch
- authentication failure
- permission denial
- merge conflict
- agent-reported real blocker
- controller invariant violation

Those should become a clear failure or `BLOCKED` state.

The outer sandbox (ADR 0005, #168) adds two. `SandboxLaunchRefusedError` is
an `ExecutionError`: under `sandbox.mode: required` a launch the sandbox
could not be established for (a policy refusal naming the path and the
rule, a sandbox directory that cannot be prepared, a backend setup failure,
a host that cannot enforce) never ran, is never retried without the
sandbox and is never attributed to the agent. For an agent the phase is
unchanged and `resume` tries again; until #169's pre-launch gate exists the
attempt counter (and a LOCAL write phase's launch charge) is already saved
by then, as for an executable that cannot be spawned. A refused LOCAL
validation command leaves the phase unverified; a refused pre-merge
verification command blocks the merge gate (`BLOCKED`). `SandboxTamperError`
is a `VerificationError` for a private object directory that holds what no
object store does (above); the step enters `BLOCKED`.

The per-issue scheduler namespaces (#178, above) add three, none of them
retried. `NamespaceError` is a `StateError`: a `binding.json` that is not a
valid `autoforge.run-binding/1` document, a second binding write, a
namespace of another repository or an `issues/` directory beyond its
bound. `NamespaceMismatch` is a `NamespaceError` naming (`field`) the
identity in which a binding differs from what its caller expects; the
binding is never rewritten to agree. `SchedulerStateDirError` is a
`ConfigurationError`: a standalone command pointed at a state directory in
the scheduler root (exit 2, nothing read or written).

The scheduler files (#181, above) add two, neither retried and neither
overwriting anything. `SchedulerStateError` is a `StateError`: a
`claim.json` or `scheduler.json` that is not a valid document of its
schema, a close the closing rule refuses, or a claim operation called
without the issue's held L2. `ClaimConflict` is a `SchedulerStateError`:
the claim on disk is not the caller's (`claim_id`, `generation`), or the
slot is taken; classification treats it as uncertain. It is unrelated to
`ClaimConflictError`, which is about GitHub claims.

---

## Related contracts

- The replan journal nested in the state file has stage-specific completeness
  rules and a rejection-monotonicity rule of its own:
  [replan-transaction.md](replan-transaction.md).
- What must be re-verified on GitHub before repeating a side effect:
  [github-safety.md](github-safety.md).
