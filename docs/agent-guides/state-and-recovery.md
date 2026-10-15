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
  acceptance action (#211); refused unblocks are only in
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
refuses a declined id again. A
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
  recomputed from the record), its owner (run, phase, issue,
  PR, transaction), the identity, the exact target, the precondition, the
  payload exactly as it is sent, the attempt count (at most
  `MAX_EFFECT_ATTEMPTS`, 2), the stage (`intended`, `attempted`, `observed`,
  `conflict`), the observed object and a conflict reason. Only redacted,
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
  transaction's default branch at the base the replan agent builds on.
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
  `null` (#211). The plan holds the acceptance comment first (at most one),
  then the creates, then the appends, then the `push` of the candidate,
  last, when the fixer committed. For `REPLAN_REEXECUTE` (#164) it is the transaction id and
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
`conflict` record again, with its attempt count kept). The fields are
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
  watermark. #173 writes them; #172 validates them on load and keeps the
  records they name.

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
definition's index; a certificate's records must be `PASS`, its set digest
theirs, its comment canonical, on the reviewed PR and keyed by the
certificate's own round, head, base and merge base, and its own digest
must match. The counter names a retained `FAIL` or `TIMEOUT` of its
commit. The watermark lies in `[0, highest seq]`; LOCAL has none. Anything
else is a `StateError`: never clipped, defaulted or repaired.

**Retention** (D11.4): a record past the 64th evicts the oldest one no
binding, certificate or counter names; a named record is never evicted.

**Read-only load** (D11.3): loading reports a pending marker
(`status` shows it as "Pending attempt", the dry-run plan as a note) and
converts nothing, so `status` and `--dry-run` leave the file byte-identical.

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

Protocol 7 → 8 (#211) added the acceptance-action state: `acceptance_outcomes`, `acceptance_comment_url` on each REMOTE `last_fix_resolutions` entry, `acceptance_source` on each `FIX` completion-context resolution, the `acceptance_comment` effect record and the optional `action` of an unblock entry. Every protocol-8 save writes `acceptance_outcomes`, so a protocol-8 file missing it is refused. A protocol-7 file loads in every phase and is relabelled on its next save, with no outcomes, `acceptance_comment_url: ""` and `acceptance_source: null`, which is exactly what a protocol-7 controller meant: it never published an acceptance comment. A protocol-7 (or older) file that carries any of these is a hand edit or a foreign file and is refused, unchanged. The number is the implementation's; it is settled on #23 and #158 before the change merges (never 6, which #126 holds).

Protocol 8 → 9 (#219) added the recorded configuration of a REMOTE run (`config_fingerprint`). Every protocol-9 save writes it, `{}` included, so a protocol-9 file missing it is refused: read as absent, it would turn the check off for the rest of the run. A protocol-8 file loads in every phase with none and is relabelled on its next save. Its acceptance state is kept as written: the protocol 7 → 8 migration above does not apply to it. The first `step`, `resume` or `unblock` records the configuration that command runs on as `upgraded` and saves it with that command's next save, and later commands are checked against it. A protocol-8 (or older) file that carries the field, `{}` included, is a hand edit or a foreign file and is refused, unchanged. The record has its own `schema` (1). Another schema is refused, never migrated, so a change to what is fingerprinted bumps both. The number is the implementation's; it is settled on #23 and #158 before the change merges.

Protocol 9 → 10 (#172) added the [validation evidence](#validation-evidence) above: `validation_records`, `validation_attempt`, `review_evidence`, `review_certificate`, `producer_validation_failure` and `validation_override_seq`. Every protocol-10 save writes all six, so a protocol-10 file missing one is refused: an absent marker read as "none" would hide the crash the resume path must record. A protocol-9 file, and every older file its own rules load, loads in every phase with all six at their empty values (no record, no marker, no binding, no certificate, no counter, `null` watermark), fabricating nothing, and is relabelled on its next save. A protocol-9 (or older) file that carries any of the six, even empty, is a hand edit or a foreign file and is refused, unchanged. The records have their own `schema` (1). The number is the implementation's; it is settled on #23 and #158 before the change merges.

Protocol 5 → 7 (#160) added the effect state above (`effect_records`, `entry_observation`, `completion_context`, `launch_label`). Protocol 6 is skipped: #23 reserves it for #126, and the number this bump takes must be settled on #23 before it merges. State persisted under protocol 5, or under any older protocol that its own rules load, carries no effect state and loads as none; a legacy file that does carry one of these fields is a hand edit or a foreign file and is refused. A legacy REMOTE file with `attempt >= 1` gets the launch label `agent_publishes` for a phase whose agent published under the previous contract, so that the first entry of this version knows an agent may already have published (D13.3). `UPDATE_EPIC` is the one such phase whose publication this version performs itself: its one-shot legacy re-entry adopts the progress comment that agent posted, with no record, and asks for the selection only (D13.7; [github-safety.md](github-safety.md), "Before UPDATE_EPIC"). LOCAL state, whatever its protocol, carries no effect state.

---

## Idempotency and crash recovery

Assume the process may crash after an external side effect but before local state is persisted.

Examples:

- a PR may already have been created
- a review comment may already exist
- a fix may already have been pushed
- a PR may already have merged

Resume logic must inspect actual Git/GitHub state before repeating destructive or duplicative actions.

A controller effect (ADR 0004) is resumed from its record and never re-sent blind. Every crash window has one answer: a crash before the intent is saved means nothing happened and the step re-runs; an `intended` or `attempted` record is reconciled by its identity before anything is sent, so a write that landed before the save is adopted as `observed` (no second write) and one that did not is issued again within the attempt bound; an object that a human created or changed between the intent and the write, a conflicting identity, or two marker-bearing objects make the record `conflict` (`BLOCKED`, naming the object); an exhausted bound is `BLOCKED` naming the record and the manual step; and a transient GitHub failure during reconciliation leaves the state unchanged for `resume`. A phase whose effect landed finishes from its completion context without relaunching its agent. `FIX` (#163) is the case with the most windows, and each has one outcome: a crash before the plan is saved sent nothing, so `resume` launches the fixer again, against the entry observation saved before the first launch (a follow-up issue found later that it does not explain blocks); a crash after the plan is saved, at any stage of any create, append or push, completes the plan from the journal without relaunching the fixer, each record adopted or issued at most once in plan order; a PR head at the planned candidate is the controller's own push and never takes a `REVIEW` detour; and a head that is neither the reviewed HEAD nor the candidate is someone else's push (`FIX -> REVIEW`, the plan dropped, no fix recorded). An acceptance comment (#211) adds its own: a crash after the plan save, before the `BLOCKED` save, re-reads the issue at `resume` and blocks again with nothing sent and no launch; an approval saved by `unblock` is published by the next `resume` (`attempted` saved first); a comment that landed before its `observed` save is adopted, never posted twice, as is the exact payload an operator posted; a marked comment with other bytes, or two, is a `conflict`; a comment without the marker is not evidence and the action keeps waiting; a second approval, or one with a stale or mismatched id, is refused with the state byte-identical; a push to the PR while it waits refuses an approval or a decline as stale, and a plain `unblock` routes the drift to `REVIEW`, recording the action `stale`; a closed issue, or one outside the run's repository, holds the comment and the adoption of an exact one, landed or posted, with nothing later in the plan sent (PR #221 R1-F2), also when the issue stops qualifying between the comment's write and its read-back: that step blocks with the eligibility reason, the record stays `attempted` and the reason says the comment was sent (PR #221 R2-F1); and a K9 already `observed` is never sent again while the rest of the plan completes. `REPLAN_REEXECUTE` (#164) has the same split: a crash before its push and PR plan is saved sent nothing, so `resume` launches the replan agent again on the base the entry observation recorded (a replacement branch, or a PR carrying the transaction's marker, found later is not the controller's and rejects the transaction); a crash after it, before the push, between the push and the PR or after the PR was created, completes the plan without relaunching the agent, and the PR is created only while the derived branch still holds the pushed candidate. The journal never stands in for GitHub on replay: before the completion sends anything it re-reads the PR's base and merge base (moved: the same `REVIEW` route, findings carried), its head repository and branch (no longer the plan's target: `BLOCKED`), and the open issues against every resolution, including follow-ups already journaled as `observed` (closed, unmarked, duplicated, or a marker the controller did not write: `BLOCKED`). The run log's `events.jsonl` is never read by any recovery path.

A validator (#172, ADR 0006 D10.7, §2.16) has its own windows. A crash before the attempt marker is saved recorded nothing, and the caller runs the definition again. A crash with the marker saved, the validator running or already exited, leaves the marker: the next `step` or `resume`, under the lock and before anything else of the step, appends an `INTERRUPTED` record built from the marker alone and clears the marker in the same save, and the definition runs again when its phase does. An exit 0 the controller did not record is therefore never a `PASS`. The record and the marker clear are one save, so a crash between them is impossible; a marker whose record exists (only a hand edit makes one) is cleared and nothing is appended, which also makes a duplicate resume idempotent. More than two `INTERRUPTED` records for one candidate, definition and policy (those at or below the operator's override watermark not counted) enters `BLOCKED` in the save that writes the third, naming the candidate and the definition. A launch or setup failure clears the marker in the next save and writes no record.

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
reused only when `git worktree list` registers it as a worktree root, and a
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
`GIT_ALTERNATE_OBJECT_DIRECTORIES=<git common dir>/autoforge/sandbox/<n>/objects git -C <worktree> log`,
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
  (`transient_failure`; today an OpenCode `openai/*` run whose only
  run-level error is `WebSocket closed with code 1000`), in a REMOTE run: at
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

---

## Related contracts

- The replan journal nested in the state file has stage-specific completeness
  rules and a rejection-monotonicity rule of its own:
  [replan-transaction.md](replan-transaction.md).
- What must be re-verified on GitHub before repeating a side effect:
  [github-safety.md](github-safety.md).
