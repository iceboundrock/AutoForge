"""Persistent controller state with atomic file persistence.

State file:  ``<state_dir>/state.json``      (default ``.autoforge/state.json``)
Run logs:    ``<state_dir>/logs/<run-id>/``
Lock file:   ``<git common dir>/autoforge/controller.lock`` -- keyed by the
             repository, not by ``state_dir``; see :mod:`autoforge.locking`

Saves are atomic (temp file in the same directory + fsync + os.replace) so
a crash mid-write never leaves a half-written JSON file.  A corrupted state
file (unparseable, wrong protocol, invalid UTF-8, a dangling symlink, or a
non-regular entry such as a FIFO, socket, device or directory) raises
StateError with a meaningful message and is never silently overwritten with
a fresh state: ``run`` refuses (exit 2) unless ``--force`` is given, and
even then the unreadable entry is moved aside as
``state.json.corrupt-<timestamp>`` by :func:`quarantine_state_file` rather
than deleted (a directory entry cannot be archived automatically and stays
put with an error).  The archive name is reserved by ``link(2)``, or by an
exclusive create / ``symlink(2)`` where the filesystem has no hard links,
and the original is unlinked only afterwards; a crash between the two
leaves ``state.json`` in place beside its archive, and the next
``run --force`` archives it again under a new name -- a duplicate copy,
never a lost one.  The entry is inspected with ``lstat``/``fstat`` and
opened non-blocking before it is read, so a FIFO without a writer fails
loudly instead of hanging the command.  ``run`` inspects, decides, quarantines, writes the first
state and executes it under one continuous controller lock (``step`` and
``resume`` load and execute under it likewise), so the verdict on an
existing entry is never taken from a view another controller may have
changed since, and no second controller can take over between the first
save and the execution.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

from . import __prompt_version__, __protocol_version__, __version__
from .config_fingerprint import RecordedConfiguration
from .effects import (
    ACTION_ID_RE,
    EVIDENCE_CLASSES,
    LABEL_AGENT_PUBLISHES,
    LABEL_NONE,
    LAUNCH_LABELS,
    MAX_CONFLICT_REASON_CHARS,
    OPERATOR_ACTION_ID_RE,
    PUBLISHING_PHASES,
    Binding,
    EffectKind,
    PhaseEffects,
    load_phase_effects,
)
from .errors import ConfigurationError, StateError
from .evidence import EvidenceFields, RecordView, check_evidence
from .loop_guard import validate_review_history
from .replan_txn import LEGACY_JOURNAL_PROTOCOLS, legacy_journal_refusal
from .result_parser import FINDING_ID_RE, MAX_FINDING_ID_CHARS, MAX_OPERATOR_ACTIONS_PER_FIX
from .run_contract import LocalRunContract
from .runlog import validate_run_id
from .safefs import HardLinksUnavailable, ReadLimitExceeded, SafeRoot, entry_kind
from .transitions import LOCAL_PHASES, LOCAL_WRITE_PHASES, Phase, WorkflowMode
from .validation import parse_comment_url, parse_issue_url, parse_pr_url

STATE_FILENAME = "state.json"
LOGS_DIRNAME = "logs"
CORRUPT_SUFFIX = ".corrupt-"
# What :func:`archive_state_file` appends to ``state.json`` when an operator
# retires a scheduler namespace's run (``epic archive``, ADR 0008 D17.4).
ARCHIVED_SUFFIX = ".archived-"
# Prefix/suffix of the temporary file :func:`save_state` renames into place.
TMP_PREFIX = ".state-"
TMP_SUFFIX = ".tmp"
# The most a state file may be before :func:`load_state` refuses it as
# corrupt without reading it.  Everything the controller persists is bounded
# (findings per round, resolution text, digests, verification failures), and
# a real state file is a few tens of kilobytes; the budget is generous so a
# controller upgrade never trips it, and finite so an oversized or sparse
# ``state.json`` -- a same-user process can plant either -- is refused
# rather than materialised into memory.
MAX_STATE_FILE_BYTES = 64 * 1024 * 1024
# Bound on the persisted ``block_reason`` (#88), applied by
# :func:`bound_block_reason` at every engine writer of the field, after
# redaction.  The reason is operator diagnostics -- what ``status`` prints and
# what a human reads before deciding -- not work a later phase acts on, so it
# may be clipped, unlike a finding or a resolution (rejected at the parser,
# never clipped).  Without a bound it was the one agent-derived field with no
# bound of its own: a LOCAL FIX that leaves every finding ``unresolved``
# concatenates up to ``MAX_RESOLUTIONS_PER_FIX`` rationales of up to
# ``MAX_REQUIRED_RESOLUTION_CHARS`` each (~300 000 characters), and an agent's
# ``status: blocked`` ``message`` is bounded only by the whole CONTROL_RESULT
# block (1 MiB); either would be carried by every later load and rewrite of
# ``state.json`` and printed whole by ``status``.  The bound keeps a couple of
# full rationales and their surrounding text; what it drops is still in
# ``last_fix_resolutions`` (persisted whole) or the run log's ``stdout.log``.
MAX_BLOCK_REASON_CHARS = 8000
# What :func:`bound_block_reason` keeps from the *end* of an over-long reason:
# the controller composes its reasons as ``<what happened> (<detail>). <what
# stays where, what a human must do>``, and the detail is the part that
# grows, so the head and the tail are the parts that tell the operator what
# to do and the middle is what is dropped.
BLOCK_REASON_TAIL_CHARS = 1000
# Room reserved for the omission marker between the head and the tail.  The
# marker names the count omitted, so its length varies; the reserve covers
# the longest rendering (a count of at most the state file's own byte bound)
# and :func:`bound_block_reason` keeps the total at or under
# ``MAX_BLOCK_REASON_CHARS`` whatever the input.
_BLOCK_REASON_MARKER_RESERVE = 200


# Names AutoForge gives the entries it writes into a state directory.  They
# are *names*, and nothing here treats a name as evidence of authorship: a
# LOCAL state directory lives outside the reviewed working tree (see
# :meth:`autoforge.local_workspace.LocalWorkspace.check_state_dir_location`),
# so the controller never has to decide whether a file it found is one of its
# own.  The old `is_runtime_artifact` name-shape allowlist existed only to
# carve a state directory out of the workspace fingerprint, and there is no
# longer anything to carve.


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def bound_block_reason(reason: str) -> str:
    """``reason`` at or under :data:`MAX_BLOCK_REASON_CHARS`, head and tail kept.

    A reason within the bound is returned unchanged.  A longer one keeps its
    head, then a marker naming how many characters were omitted, then its
    last :data:`BLOCK_REASON_TAIL_CHARS` characters, so the sentence that
    says what happened and the sentence that says what the operator must do
    both survive and only the middle -- the concatenated detail -- is
    dropped.  Call it on *redacted* text: it clips by character count and
    knows nothing about secrets, so a secret cut in half by the clip would
    no longer match its redaction pattern.  The engine's writers redact
    first, then bound (:meth:`autoforge.engine.ControllerEngine._block` and
    the two agent-message writers); ``status`` redacts what it prints on top
    of that, as it does for every state field.
    """
    if len(reason) <= MAX_BLOCK_REASON_CHARS:
        return reason
    head_len = MAX_BLOCK_REASON_CHARS - BLOCK_REASON_TAIL_CHARS - _BLOCK_REASON_MARKER_RESERVE
    head = reason[:head_len]
    tail = reason[-BLOCK_REASON_TAIL_CHARS:]
    omitted = len(reason) - len(head) - len(tail)
    marker = (
        f" [autoforge: {omitted} characters of the block reason omitted; the bound is "
        f"{MAX_BLOCK_REASON_CHARS} characters, the first {len(head)} and last {len(tail)} "
        "were kept] "
    )
    bounded = head + marker + tail
    if len(bounded) > MAX_BLOCK_REASON_CHARS:
        # Unreachable while the reserve covers the marker; fail loudly rather
        # than persist an over-long reason if a wording change outgrows it.
        raise StateError(
            f"bounded block reason is {len(bounded)} characters, over the "
            f"{MAX_BLOCK_REASON_CHARS} bound: the omission marker outgrew its reserve"
        )
    return bounded


def _validate_findings_field(name: str, findings: object) -> None:
    """Refuse a persisted findings list (open or prior) that is not one.

    A persisted finding's id is rendered into controller syntax (the
    follow-up marker of the FIX prompt, the prior-findings block of the
    REVIEW prompt), so the id is held to the parser's rule on load rather
    than crashing the renderer.
    """
    if not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings):
        raise StateError(f"state field {name!r} must be a list of objects")
    for finding in findings:
        fid = finding.get("id")
        if (
            not isinstance(fid, str)
            or len(fid) > MAX_FINDING_ID_CHARS
            or FINDING_ID_RE.match(fid) is None
        ):
            raise StateError(
                f"state field {name!r} holds an entry whose 'id' is not a finding id "
                "of the form R<round>-F<n>"
            )


def _validate_local_pending(state: AutoForgeState) -> None:
    """Check the LOCAL pending-invocation checkpoint is internally consistent.

    ``local_pending_phase`` records that a write-capable LOCAL phase was
    launched and its work may already be in the working tree; the fingerprint
    beside it is the tree from *before* the first attempt, and the attempt
    count is what bounds re-entry.  The three fields are one record, so a file
    carrying only some of them is corruption — and corruption that matters:
    a lost ``local_pending_phase`` makes an already-launched phase look like a
    first invocation (so "the tree is unchanged, nothing was implemented"
    becomes unprovable), while a lost fingerprint silently rebases that
    question onto the tree the failed attempt left behind.  A typo in the
    phase value would do the same, which is why it is matched against
    :data:`~autoforge.transitions.LOCAL_WRITE_PHASES` rather than merely being
    a string.  And the record belongs to *one* phase entry: a checkpoint for
    FIX under a run whose phase is REVIEW would be closed by the next clean
    review without anyone looking at the work it records, so a mismatch is
    refused unless the run has already ended in BLOCKED or FAILED, where the
    checkpoint is crash evidence rather than a pending resumption (R9-F4).
    """
    pending = state.local_pending_phase
    allowed = ", ".join(p.value for p in LOCAL_WRITE_PHASES)
    if not pending:
        if state.local_pending_fingerprint or state.local_pending_attempts:
            raise StateError(
                "state fields 'local_pending_fingerprint' and 'local_pending_attempts' "
                "must be empty when 'local_pending_phase' is not set (found "
                f"{state.local_pending_fingerprint!r} / {state.local_pending_attempts}); "
                "a pending invocation without the phase it belongs to is corruption"
            )
        return
    try:
        pending_phase = Phase(pending)
    except ValueError:
        raise StateError(
            f"state file has unknown local_pending_phase {pending!r} (expected one of {allowed})"
        ) from None
    if pending_phase not in LOCAL_WRITE_PHASES:
        raise StateError(
            f"state field 'local_pending_phase' must be one of {allowed}, not {pending!r}: "
            "only a write-capable LOCAL phase can leave work in the tree to recover"
        )
    if state.mode != WorkflowMode.LOCAL:
        raise StateError(
            f"state field 'local_pending_phase' is set ({pending!r}) on a "
            f"{state.mode.value} run, which has no local working-tree checkpoint"
        )
    if not state.local_pending_fingerprint:
        raise StateError(
            "state field 'local_pending_fingerprint' must be set whenever "
            f"'local_pending_phase' is ({pending!r}): without the fingerprint from before "
            "the first attempt the controller cannot tell work that was already produced "
            "from work that was never done"
        )
    if state.local_pending_attempts < 1:
        raise StateError(
            "state field 'local_pending_attempts' must be at least 1 whenever "
            f"'local_pending_phase' is ({pending!r}): the checkpoint is written when an "
            "invocation is launched, so zero attempts is corruption"
        )
    if state.phase != pending_phase and state.phase not in (Phase.BLOCKED, Phase.FAILED):
        # The checkpoint belongs to the phase entry that wrote it. A run
        # whose current phase is a different, live phase would resolve that
        # phase and then close a checkpoint it never examined, silently
        # discarding the unverified work the checkpoint exists to recover.
        # A terminal phase may keep it: a run that blocked or failed on top
        # of an unverified launch carries the evidence of that launch, and
        # nothing runs after it that could misread the record.
        raise StateError(
            f"state field 'local_pending_phase' ({pending!r}) does not match the current "
            f"phase {state.phase.value!r}: a pending write-phase checkpoint can only be "
            "resumed by the phase that wrote it, or kept as evidence by a terminal "
            "BLOCKED/FAILED phase"
        )


# Protocol labels an older controller wrote that this one can still read,
# each subject to the boundary rules its successors introduced (see
# :meth:`AutoForgeState.from_dict`). Protocols 1-4 changed the replan
# journal, so those are the journal's; protocols 2 and 3 also changed the
# review binding (``_REVIEW_BINDING_GAPS``), protocol 4 only the journal.
# Protocol 5 is the last label before the controller owned external effects
# (ADR 0004 D13.1, #160): it is the immediate predecessor of protocol 7 (6 is
# reserved for #126) and is loaded under D13.2.
_LEGACY_PROTOCOLS = LEGACY_JOURNAL_PROTOCOLS | {"5"}
# The label before the controller could publish an acceptance comment
# (#211). A protocol-7 file is protocol 8's shape without the acceptance
# state: it carries the effect fields and is loaded with them, in every
# phase, and relabelled.
_PRE_ACCEPTANCE_PROTOCOL = "7"
# The label before a REMOTE run recorded its configuration (#219). A
# protocol-8 file is protocol 9's shape without ``config_fingerprint``: it
# is loaded in every phase with none, which is "not checked", and
# relabelled.
_PRE_CONFIG_PROTOCOL = "8"
# The label before candidate validation evidence (#172, ADR 0006 D15.1). A
# protocol-9 file is protocol 10's shape without the six Wave 2B fields: it
# is loaded in every phase with them empty (D15.4), and relabelled.
_PRE_VALIDATION_PROTOCOL = "9"
# The label before operator acceptance actions (#243, ADR 0004 D13.10). A
# protocol-10 file is this protocol's shape without the operator-action
# state: it is loaded in every phase with ``operator_comment_outcomes: []``,
# no ``operator_targets``, ``operator_sources: null`` and
# ``operator_evidence: []``, and relabelled.
_PREVIOUS_PROTOCOL = "10"
# The fields protocol 7 added (#160): the effect records, the entry
# observation and the completion context of the current phase entry, and the
# launch label. A legacy file never carries one; one that does is a hand edit
# or a foreign file, not an older controller's state.
_EFFECT_FIELDS = ("effect_records", "entry_observation", "completion_context", "launch_label")
# The field protocol 8 added (#211): the outcomes of this PR's acceptance
# actions. Protocol 8 also added a K9 effect kind, an ``acceptance_source``
# to each FIX context resolution, an ``acceptance_comment_url`` to each
# REMOTE ``last_fix_resolutions`` entry and an optional ``action`` to an
# ``unblock_history`` entry (:func:`_acceptance_state_in`).
_ACCEPTANCE_FIELDS = ("acceptance_outcomes",)
# The field protocol 9 added (#219): the configuration a REMOTE run was
# recorded under (:mod:`autoforge.config_fingerprint`), ``{}`` when none is.
_CONFIG_FIELDS = ("config_fingerprint",)
# The fields protocol 10 added (#172, ADR 0006 D11.2): the validation
# records, the attempt marker, the REVIEW evidence binding, the review
# certificate, the producer failure counter and the override watermark
# (:mod:`autoforge.evidence`). An older file never carries one.
_VALIDATION_FIELDS = (
    "validation_records",
    "validation_attempt",
    "review_evidence",
    "review_certificate",
    "producer_validation_failure",
    "validation_override_seq",
)
# The field protocol 11 added (#243): the outcomes of this PR's operator
# actions. Protocol 11 also added the K10 effect kind, a FIX entry
# observation's ``operator_targets``, a FIX context resolution's
# ``operator_sources`` and a REMOTE ``last_fix_resolutions`` entry's
# ``operator_evidence`` (:func:`_operator_state_in`).
_OPERATOR_FIELDS = ("operator_comment_outcomes",)
# ``acceptance_outcomes`` (#211): what became of each acceptance action of
# the current PR. Bounded: at most one action is requested per FIX result,
# and the writer drops the oldest entries past the bound
# (:meth:`AutoForgeState.bound_acceptance_outcomes`), but never a decline of
# the round being fixed. That decline is what refuses the same action again,
# and a decline re-enters FIX without advancing the round, so dropping one
# would let the action through unchanged (PR #221 R3-F1). The round's
# declines are bounded instead, below the history's bound: once a round
# holds MAX_DECLINES_PER_ROUND, FIX refuses any further acceptance action of
# that round, so there is never one more to decline.
ACCEPTANCE_OUTCOMES = ("verified", "declined", "stale")
MAX_ACCEPTANCE_OUTCOMES = 64
MAX_DECLINES_PER_ROUND = 16
_ACCEPTANCE_OUTCOME_KEYS = (
    "action_id",
    "round",
    "finding_id",
    "outcome",
    "at",
    "comment_url",
    "reason",
)
# ``operator_comment_outcomes`` (#243): what became of each operator action
# (K10) of the current PR. ``verified`` is an observed comment (``exact`` or
# ``bound`` evidence), ``declined`` an action the operator declined,
# ``withdrawn`` a pending action dropped with a decline (it may be requested
# again), ``stale`` one the PR's revision left behind. Bounded like
# ``acceptance_outcomes``, never dropping an entry of the round being fixed:
# one round holds at most MAX_DECLINES_PER_ROUND decline cycles of at most
# MAX_OPERATOR_ACTIONS_PER_FIX entries each, plus the round's completion or
# drift, so the bound below that product (68) is what always fits one round.
OPERATOR_COMMENT_OUTCOMES = ("verified", "declined", "withdrawn", "stale")
MAX_OPERATOR_COMMENT_OUTCOMES = 80
MIN_OPERATOR_COMMENT_OUTCOMES = (MAX_DECLINES_PER_ROUND + 1) * MAX_OPERATOR_ACTIONS_PER_FIX
assert MAX_OPERATOR_COMMENT_OUTCOMES >= MIN_OPERATOR_COMMENT_OUTCOMES
_OPERATOR_OUTCOME_KEYS = (
    "action_id",
    "round",
    "finding_id",
    "target",
    "outcome",
    "evidence",
    "comment_url",
    "body_sha256",
    "payload_sha256",
    "at",
    "reason",
)
_OPERATOR_EVIDENCE_KEYS = (
    "action_id",
    "target",
    "comment_url",
    "evidence",
    "body_sha256",
    "payload_sha256",
)
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
# The phases in which the persisted clean review is consumed by the merge
# gate, and so the phases a state without the review's full binding cannot
# be loaded in.
_MERGE_PHASES = (Phase.READY_FOR_MERGE, Phase.MERGE)
# What each pre-protocol-4 label failed to record about the completed
# review, for the refusal text: the binding the merge gate requires that
# the file cannot supply. A legacy label absent here (protocol 4) recorded
# the review whole; only its replan journal is subject to a boundary rule.
_REVIEW_BINDING_GAPS = {
    "1": "which PR and base branch that review was posted on",
    "2": "which PR and base branch that review was posted on",
    "3": "which merge base that review's diff was computed from",
}
# A persisted commit id is a full lower-case SHA or empty (nothing bound).
# Matched with ``fullmatch``: ``$`` alone would accept a trailing newline.
_SHA40_RE = re.compile(r"[0-9a-f]{40}")


def _legacy_review_binding_refusal(
    data: dict, phase: Phase, *, protocol: str, written_by: str
) -> str:
    """Why a pre-protocol-4 state cannot be loaded, or ``""`` when it can.

    Protocol 3 added ``reviewed_pr_url`` and ``reviewed_base_ref`` next to
    ``reviewed_head_sha``, protocol 4 added ``reviewed_merge_base_sha``
    (#96), and the merge gate refuses a clean review that lacks any of
    them. A file in any phase but READY_FOR_MERGE or MERGE is loaded as is:
    nothing in it consumes the binding before the next completed review
    writes it. A file parked in one of the merge phases holds a clean
    review that the gate could only accept by binding it, now, to the run's
    current PR, base and merge base -- and a binding reconstructed from the
    very fields it exists to check is no binding. It is refused at the
    boundary, with the PR and HEAD named so the operator can decide on
    GitHub whether the PR is done, and never read as corruption: it was
    written whole.

    ``data`` is read defensively: this runs before any schema check.
    """
    if phase not in _MERGE_PHASES or protocol not in _REVIEW_BINDING_GAPS:
        return ""

    def _text(name: str) -> str:
        value = data.get(name)
        return value if isinstance(value, str) and value else "(none)"

    return (
        f"state file was written by controller {written_by or '(unknown)'} under "
        f"protocol_version {protocol!r} and is in phase {phase.value} with a clean review of "
        f"PR {_text('current_pr_url')} at HEAD {_text('reviewed_head_sha')}; protocol "
        f"{protocol!r} did not record {_REVIEW_BINDING_GAPS[protocol]}, and this controller "
        "does not bind it to the run's current PR, base and merge base after the fact. "
        "Nothing was merged or counted by this controller. Check the PR on GitHub: if "
        "it is already MERGED the issue is done and the run should continue from the next "
        "issue; if it is OPEN, start a new run for the issue, which adopts the PR and "
        "reviews it again. The state file was left unchanged"
    )


@dataclass
class AutoForgeState:
    protocol_version: str = __protocol_version__
    controller_version: str = __version__
    prompt_version: str = __prompt_version__

    run_id: str = ""

    # Which workflow this run executes. Explicit controller state, never
    # inferred: a LOCAL run has no repository/EPIC/issue/PR at all, and a
    # state file written before local mode existed has no 'mode' key and
    # loads as REMOTE (see from_dict).
    mode: WorkflowMode = WorkflowMode.REMOTE

    repository: str = ""
    epic_url: str = ""

    current_issue_url: str = ""
    current_pr_url: str = ""
    current_branch: str = ""

    phase: Phase = Phase.INITIALIZING

    # Completed review rounds for the current PR; the upcoming round is +1.
    review_round: int = 0

    # The revision the last completed review is bound to: the PR it was
    # posted on (canonical URL), the HEAD the reviewer saw, the base branch
    # the reviewed diff was against and the merge base that diff was
    # computed from. The four are written together by the review and read
    # together by the merge gate: a clean review is a decision about one
    # PR's diff against one base at one commit, so before MERGE the run's
    # ``current_pr_url`` must be this PR by identity and the PR GitHub
    # returns for it must still have this HEAD, this base and this merge
    # base. A substituted ``current_pr_url`` (a same-repository PR at the
    # same HEAD on the same branch against another base, say), a retargeted
    # base, or a base rewritten under the same name (force-push, reset; the
    # merge base moves, the name and HEAD do not, #96) is therefore never
    # merged on the strength of a review it did not receive. Ordinary
    # commits landing on the base leave the merge base where it was.
    reviewed_pr_url: str = ""
    reviewed_head_sha: str = ""
    reviewed_base_ref: str = ""
    reviewed_merge_base_sha: str = ""
    # Latest PR HEAD, base branch and merge base the controller observed
    # through `gh`; all are re-bound right before a review, and the
    # post-review read must find them unchanged for the round to be current
    # rather than stale.
    current_head_sha: str = ""
    current_base_ref: str = ""
    current_merge_base_sha: str = ""

    last_review_comment_url: str = ""
    last_review_result: str = ""  # "needs_fix" | "clean" | "stale" | ""
    last_review_needs_fix: bool | None = None
    # Findings from the last review that still require a FIX round
    # (each: {"id","classification","required_resolution",...}).
    open_findings: list[dict] = field(default_factory=list)
    # Findings of the latest review of this PR that no FIX round resolved
    # because the PR's revision moved out from under them: the round went
    # stale while the reviewer worked, or the HEAD was already past the
    # reviewed one when FIX was entered. Same shape as `open_findings`, but
    # no fixer is asked to resolve them; they are handed to the next REVIEW
    # as findings to re-check at the actual HEAD (`PRIOR_FINDINGS`). When
    # non-empty they are the findings of round `review_round` at
    # `reviewed_head_sha`, published in `last_review_comment_url`. Replaced
    # by every later stale round (a stale clean round leaves none), cleared
    # by the next completed round of the actual revision, and per PR.
    prior_findings: list[dict] = field(default_factory=list)
    # Resolutions reported by the last FIX round (verified by the controller).
    # A REMOTE entry also carries ``acceptance_comment_url``: the comment
    # the controller verified for an ``acceptance_action`` resolution, else
    # "" (#211); and ``operator_evidence``: the operator comments verified
    # for the finding in this round (#243), ``[]`` when none.
    last_fix_resolutions: list[dict] = field(default_factory=list)
    # The outcomes of the current PR's acceptance actions (#211), oldest
    # first: ``action_id``, ``round``, ``finding_id``, ``outcome``
    # (verified | declined | stale), ``at``, ``comment_url`` (the verified
    # comment, else "") and ``reason`` (the operator's, redacted, for a
    # decline; the drift for a stale action). Read by ``status``, by the FIX
    # prompt and by the FIX result check (a declined action is not
    # requested again in its round). Per PR, REMOTE only.
    acceptance_outcomes: list[dict] = field(default_factory=list)
    # The outcomes of the current PR's operator actions (K10, #243), oldest
    # first: ``action_id``, ``round``, ``finding_id``, ``target`` (the issue
    # the comment is for), ``outcome`` (verified | declined | withdrawn |
    # stale), ``evidence`` (exact | bound for a verified one, else ""),
    # ``comment_url`` and ``body_sha256`` (the verified comment, else ""),
    # ``payload_sha256`` (the requested text's rendering), ``at`` and
    # ``reason``. Read by ``status``, the FIX prompt and result check and
    # REVIEW's ``operator-evidence.md``. Per PR, REMOTE only.
    operator_comment_outcomes: list[dict] = field(default_factory=list)
    # One entry per completed review round of the current PR (see
    # loop_guard.review_record): round, reviewed_head_sha, result
    # (needs_fix | clean | stale), finding_count, fingerprint (digest of the
    # round's normalised required_resolution texts), resolutions (one digest
    # per distinct non-empty normalised required_resolution, clipped to
    # loop_guard.MAX_PERSISTED_RESOLUTION_DIGESTS) and resolutions_truncated
    # (True when that clip dropped digests). No review text is persisted.
    # An entry written before per-finding digests existed has no
    # 'resolutions' key at all and keeps the count-only stagnation rule; a
    # present but malformed field is corruption and fails loudly on load.
    # Drives the review-round cap and stagnation detection; cleared per PR.
    review_history: list[dict] = field(default_factory=list)
    # Bounded controller-side verification failures relevant to this issue;
    # supplied to a fresh reimplementation as constraints, not prompt policy.
    verification_failures: list[str] = field(default_factory=list)

    # Per-issue implementation lifecycle. The original attempt is 1; only a
    # verified replacement increments it.
    execution_attempt: int = 1
    escalation_count: int = 0
    superseded_prs: list[dict] = field(default_factory=list)
    # Durable REPLAN_REEXECUTE transaction (see autoforge.replan_txn). Empty
    # when no replan is in flight; a serialised ``ReplanTransaction`` while
    # one is. It is the controller's intent record: recovery replays it
    # rather than re-deriving what the safe disposition should have been.
    replan_transaction: dict = field(default_factory=dict)

    # -- LOCAL mode -----------------------------------------------------
    # Repository-relative path of the frozen feature specification.
    feature_spec_path: str = ""
    # SHA-256 of the specification's bytes at run creation. Re-checked before
    # and after every agent phase: an agent that rewrites its own acceptance
    # criteria must not be able to make the run easier.
    feature_spec_sha256: str = ""
    # The *git anchor* a local run is pinned to: HEAD and the checked-out
    # branch when the run was created ("" for an unborn HEAD, "" for a
    # detached one). A local run never requires HEAD to move and never allows
    # it to: the controller re-reads both around every agent phase and enters
    # BLOCKED when either changed, so an agent that commits, resets or
    # switches branches is caught by the controller rather than only
    # forbidden by the prompt.
    base_head_sha: str = ""
    base_branch: str = ""
    # The run's durable contract (see :mod:`autoforge.run_contract`): the
    # repository root, state root, workspace policy, validation commands,
    # fix-round budget and prompt version the run was *defined* with. Every
    # later invocation compares what it would define against this before it
    # executes anything, and execution reads these values from here -- never
    # from the configuration of the moment -- so a resume can revalidate the
    # contract but cannot redefine it. Persisted as the contract's own dict
    # form and parsed strictly on load; ``{}`` for REMOTE runs.
    local_run_contract: dict = field(default_factory=dict)
    # Workspace fingerprint the controller bound before the current review
    # (the local analogue of ``current_head_sha``).
    workspace_fingerprint: str = ""
    # Fingerprint the last completed review was bound to (the local analogue
    # of ``reviewed_head_sha``).
    reviewed_workspace_fingerprint: str = ""
    # Completed local FIX rounds; bounded by ``local.max_fix_rounds``.
    local_fix_rounds: int = 0
    # Durable checkpoint for a LOCAL agent invocation that may already have
    # written to the working tree. Persisted *before* the agent is launched,
    # so a crash (or a rejected result) cannot make `resume` mistake work that
    # already exists for work that was never done. ``local_pending_phase`` is
    # the phase value, ``local_pending_fingerprint`` the fingerprint from
    # *before the first* attempt of that phase entry, and
    # ``local_pending_attempts`` how many invocations that entry has launched
    # (bounded, so a phase that can never be completed blocks instead of
    # looping over `resume`).
    local_pending_phase: str = ""
    local_pending_fingerprint: str = ""
    local_pending_attempts: int = 0
    # Working-tree paths that were already dirty when the run was created,
    # other than the feature specification itself. Normally empty: `local run`
    # refuses a dirty tree unless the operator passes --allow-dirty, and then
    # these are named in status and in the review prompt rather than silently
    # absorbed into the feature's implementation.
    baseline_dirty_paths: list[str] = field(default_factory=list)

    # The HEAD (and the exact command list) that ``merge.verification_commands``
    # last passed on, so MERGE does not repeat what READY_FOR_MERGE proved
    # for the same commit. Either differing means the commands run again.
    premerge_verified_head_sha: str = ""
    premerge_verified_commands: list[list[str]] = field(default_factory=list)

    merged_since_epic_update: int = 0
    counted_merged_prs: list[str] = field(default_factory=list)
    # Reasons the controller rejected the UPDATE_EPIC agent's next_issue_url
    # (bounded; the last one is rendered into the retry prompt). Cleared once
    # a next issue verified or the EPIC completed.
    next_issue_rejections: list[str] = field(default_factory=list)

    # -- controller-owned external effects (ADR 0004, #160) ---------------
    # The external writes of the current phase entry that the controller
    # performs itself, one record each, in plan order (see
    # :class:`autoforge.effects.EffectRecord`): written as ``intended`` in
    # the same save as the completion context, before the first write, and
    # advanced to ``attempted`` before each issue (D4.1) and to ``observed``
    # or ``conflict`` after its read-back. The phase advances only when every
    # record is observed.
    effect_records: list[dict] = field(default_factory=list)
    # What the phase read before its first launch (refs, base, the objects
    # carrying its identities; D4.4), persisted in the pre-launch save. Empty
    # until a phase records one.
    entry_observation: dict = field(default_factory=dict)
    # The validated result data the phase consumes once its records are
    # observed (D4.6), in the closed schema of the phase it names. Recovery
    # completes from it, never from a rendered payload or the run log.
    # Records, observation and context belong to one phase entry: the save
    # that commits the phase drops all three (D2.4).
    completion_context: dict = field(default_factory=dict)
    # The contract the current entry's last launch ran under (D13.3):
    # ``agent_publishes`` when the agent was told to perform the phase's
    # writes, ``controller_publishes`` when the controller performs them, ""
    # before a launch. Every pre-launch save records it; it is read only while
    # ``attempt >= 1``.
    launch_label: str = ""

    # Agent invocations attempted for the current phase (reset on transition).
    attempt: int = 0
    # Total executed steps across the run (never reset).
    step_count: int = 0
    # Human-readable reason when phase is BLOCKED/FAILED. Redacted and then
    # bounded to ``MAX_BLOCK_REASON_CHARS`` by every engine writer (#88); a
    # longer value in a file is accepted on load (an older controller wrote
    # it) and is never rewritten by the load.
    block_reason: str = ""
    # The operator's explicit exits from BLOCKED (``autoforge unblock``), oldest
    # first, one object per unblock that was applied: ``at`` (UTC timestamp),
    # ``reason`` (the operator's text), ``block_reason`` (the reason that was
    # cleared), ``phase`` (the phase re-entered) and ``detail`` (why the
    # controller chose it). A refused unblock is not recorded here -- nothing
    # changed -- but in the run log. Kept across issues: it is the run's audit
    # trail, not per-PR bookkeeping.
    unblock_history: list[dict] = field(default_factory=list)
    # The configuration a REMOTE run was recorded under (#219): per-profile
    # provider, model, effort and limits, the agent worktree root, the
    # ``--config`` file and how it was recorded, in the closed form of
    # :class:`autoforge.config_fingerprint.RecordedConfiguration`. Written
    # when the run is created; ``resume``, ``step`` and ``unblock`` refuse a
    # configuration that differs from it unless the operator accepts the
    # change, which replaces it. ``{}`` for a LOCAL run (its run contract
    # defines it) and for a run created before the field existed (not
    # checked; the next command records one). Kept across issues.
    config_fingerprint: dict = field(default_factory=dict)

    # -- candidate validation evidence (ADR 0006, #172) -------------------
    # Every field below is strictly parsed, bounded (D10.3), digest-checked
    # and checked for referential integrity on load
    # (:func:`autoforge.evidence.check_evidence`), and cleared when the run
    # moves to the next issue. #173 writes the binding, the counter and the
    # watermark; #174 the certificate.
    #
    # What the controller observed running one trusted definition against
    # one exact candidate under one policy, oldest first, at most 64; the
    # records the binding, the certificate or the counter name are never
    # evicted (D11.4).
    validation_records: list[dict] = field(default_factory=list)
    # The marker written immediately before a validator's launch and cleared
    # in the save that appends its record (D11.2). One left by a crash is
    # turned into an ``INTERRUPTED`` record by the next ``step``/``resume``
    # under the lock, never by a load.
    validation_attempt: dict = field(default_factory=dict)
    # The set and the records a reviewer was presented (D11.2; #173 writes it).
    review_evidence: dict = field(default_factory=dict)
    # The controller's review certificate (D12.1; #174 issues it).
    review_certificate: dict = field(default_factory=dict)
    # A REMOTE producer's validation failures in its current phase entry
    # (D5.2; #173): cleared by every save in another phase.
    producer_validation_failure: dict = field(default_factory=dict)
    # The operator's override watermark (D14.2; #173 writes it): None until
    # an explicit REMOTE unblock sets it, 0 for an unblock that found no
    # record; automatic recovery never does.
    validation_override_seq: int | None = None

    created_at: str = ""
    updated_at: str = ""

    # -- the LOCAL run contract ------------------------------------------
    def local_contract(self) -> LocalRunContract:
        """The persisted contract of this LOCAL run (StateError for a REMOTE one)."""
        if self.mode != WorkflowMode.LOCAL:
            raise StateError("a REMOTE run has no local run contract")
        return LocalRunContract.from_dict(self.local_run_contract)

    # -- serialization -------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["phase"] = self.phase.value
        d["mode"] = self.mode.value
        return d

    @property
    def is_local(self) -> bool:
        return self.mode == WorkflowMode.LOCAL

    @classmethod
    def from_dict(cls, data: dict) -> AutoForgeState:
        if not isinstance(data, dict):
            raise StateError("state file must contain a JSON object")
        try:
            phase = Phase(data["phase"])
        except KeyError:
            raise StateError("state file missing required field 'phase'") from None
        except ValueError:
            raise StateError(f"state file has unknown phase {data.get('phase')!r}") from None
        # A state file written before LOCAL mode existed carries no 'mode'
        # key; it is a REMOTE run and must keep loading unchanged.
        raw_mode = data.get("mode", WorkflowMode.REMOTE.value)
        if isinstance(raw_mode, WorkflowMode):
            mode = raw_mode
        else:
            try:
                mode = WorkflowMode(raw_mode)
            except ValueError:
                raise StateError(f"state file has unknown mode {raw_mode!r}") from None
        # The protocol version decides what "unknown field" even means, so it
        # is checked before the fields are: a file written by a newer
        # controller must be reported as an unsupported protocol, not as a
        # pile of typos.
        #
        # Each protocol step added bindings, and an old file is loaded
        # exactly when nothing in it depends on the binding it lacks:
        #
        # - 1 -> 2: the replan journal records the PR and issue its decision
        #   was made on. A protocol-1 file with no replan in flight is a
        #   protocol-2 file with an old label; one with a replan in flight is
        #   refused with the transaction described, never migrated by filling
        #   the decision from the run's current PR and issue (the rebinding
        #   the fields forbid) and never handed to the journal loader, which
        #   would call it corrupt (:func:`replan_txn.legacy_journal_refusal`).
        # - 2 -> 3: the completed review records the PR it was posted on and
        #   the base branch it was bound to, and the merge gate requires
        #   both; the replan journal records the base its decision was bound
        #   to and the base it checkpointed, and the supersede requires both.
        #   A protocol-2 file with a replan in flight is refused exactly as a
        #   protocol-1 one is, for the base instead of the PR and issue. A
        #   protocol-2 file parked in READY_FOR_MERGE or MERGE holds a clean
        #   review the gate could only accept by binding it to the run's
        #   current PR and base -- the substitution the fields exist to catch
        #   -- so it is refused with the PR and HEAD named
        #   (:func:`_legacy_review_binding_refusal`). In any other phase the
        #   next review writes the binding, and the file is loaded as is.
        # - 3 -> 4: the completed review records the merge base its diff was
        #   computed from, and the merge gate requires it; the replan journal
        #   records the merge base its decision was bound to and the one it
        #   checkpointed, and the supersede requires both (#96). A protocol-3
        #   file with a replan in flight is refused exactly as a protocol-2
        #   one is, for the merge base instead of the base, and never
        #   migrated by reading the merge base GitHub reports now (the base
        #   may have been rewritten since, which is what the field detects);
        #   one parked in READY_FOR_MERGE or MERGE is refused exactly as a
        #   protocol-2 one is, for the merge base instead of the PR and base;
        #   in any other phase the next review writes the binding and the
        #   file is loaded as is.
        # - 4 -> 5: the replan journal records, with the close intent, the
        #   source PR's closed issue-event count and the number of close
        #   attempts, which is what lets a resume that finds the source OPEN
        #   prove the close never ran and retry it once (#69). The review
        #   binding did not change, so a protocol-4 file is loaded in every
        #   phase; only a replan journal at SUPERSEDE_INTENT or later is
        #   refused, and never migrated by reading the count GitHub reports
        #   now (which would record the close the field exists to detect as
        #   if it predated the intent). A journal before the write is written
        #   exactly as protocol 5 writes it and is loaded as such.
        #
        # The label is rewritten on the next save. A file that is refused is
        # left unchanged (``run --force`` moves it aside like any unreadable
        # one).
        # - 5 -> 7: the controller performs a phase's external writes itself
        #   and journals each one (effect records, the entry observation, the
        #   completion context, the launch label; ADR 0004 D13.1). A legacy
        #   file has none of them and is loaded with none (D13.2): there is
        #   nothing to reconstruct, because an agent-performed write was never
        #   journaled and is classified by the next entry from GitHub (D9).
        #   Its launch label is ``agent_publishes`` when the file records a
        #   launch of a publishing phase (``attempt >= 1``), which is what
        #   lets the first re-entry of a phase this version publishes itself
        #   adopt an agent-posted object once (D13.3, D13.7). A legacy file
        #   that carries a non-empty value in any of the new fields is
        #   refused, never merged.
        # - 7 -> 8: a FIX may publish one acceptance comment on the run's
        #   issue (K9, #211), recorded in ``acceptance_outcomes``, a FIX
        #   context resolution's ``acceptance_source``, a REMOTE
        #   ``last_fix_resolutions`` entry's ``acceptance_comment_url`` and
        #   an unblock entry's ``action``. An older file carries none of them
        #   and is loaded in every phase with none: ``acceptance_source``
        #   null and ``acceptance_comment_url`` "" (nothing was published).
        #   An older file that carries any of them is refused, never merged.
        # - 8 -> 9: a REMOTE run records the configuration it was created
        #   under (``config_fingerprint``, #219), which ``resume``, ``step``
        #   and ``unblock`` compare their own with. An older file has none
        #   and is loaded in every phase with none, which is "not checked"
        #   (the next of those commands records one). It is never filled
        #   from the configuration of the moment at load: that is the
        #   rebinding the record exists to catch, and a load (``status``)
        #   runs on no configuration. An older file that carries the field,
        #   even empty, is refused, never merged.
        # - 9 -> 10: candidate validation evidence (#172, ADR 0006 D15.1):
        #   the validation records, the attempt marker, the REVIEW evidence
        #   binding, the review certificate, the producer failure counter and
        #   the override watermark. An older file has none of them and is
        #   loaded in every phase with them empty (D15.4): nothing is
        #   fabricated, and what an empty field means is the gates' to
        #   decide. An older file that carries any of them, even empty, is
        #   refused, never merged.
        # - 10 -> 11: operator acceptance actions (K10, #243): the operator
        #   posts a comment on another issue and the controller verifies it.
        #   ``operator_comment_outcomes``, a K10 effect record, a FIX entry
        #   observation's ``operator_targets``, a FIX context resolution's
        #   ``operator_sources`` and a REMOTE ``last_fix_resolutions``
        #   entry's ``operator_evidence``. An older file has none of them and
        #   is loaded in every phase with none (a FIX entry observed under
        #   protocol 10 therefore offers no operator action). An older file
        #   that carries any of them is refused, never merged.
        raw_protocol = data.get("protocol_version", __protocol_version__)
        written_by = str(data.get("controller_version", ""))
        legacy = isinstance(raw_protocol, str) and raw_protocol in _LEGACY_PROTOCOLS
        if legacy:
            if raw_protocol in LEGACY_JOURNAL_PROTOCOLS:
                refusal = legacy_journal_refusal(
                    data.get("replan_transaction", {}),
                    protocol=raw_protocol,
                    written_by=written_by,
                )
                if refusal:
                    raise StateError(refusal)
            refusal = _legacy_review_binding_refusal(
                data, phase, protocol=str(raw_protocol), written_by=written_by
            )
            if refusal:
                raise StateError(refusal)
            # The label decides, not the shape: a protocol-7 effect field
            # may sit in a legacy file at its empty value, but effect state
            # the labelled controller could never have written is a hand
            # edit or a foreign file, and is refused rather than merged.
            # Protocol 8's acceptance state is refused whatever its value.
            planted = sorted(
                name for name in _EFFECT_FIELDS if data.get(name, "") not in ([], {}, "")
            )
            if planted:
                raise StateError(
                    f"state file is labelled protocol_version {raw_protocol!r}, which never "
                    f"wrote {', '.join(repr(n) for n in planted)}; refusing to load a "
                    "legacy file that carries protocol-7 effect state -- it is a hand edit "
                    "or a foreign file, not an older controller's state"
                )
            _refuse_acceptance_state(data, str(raw_protocol))
            _refuse_config_fingerprint(data, str(raw_protocol))
            _refuse_validation_fields(data, str(raw_protocol))
            _refuse_operator_state(data, str(raw_protocol))
        elif raw_protocol == _PRE_ACCEPTANCE_PROTOCOL:
            # 7 -> 8 (#211): the same shape without the acceptance-action
            # state. Every protocol-7 save wrote the effect fields, exactly
            # as a protocol-8 one does; the acceptance state it never wrote
            # is refused, never merged, and so is a recorded configuration.
            _require_fields(data, raw_protocol, _EFFECT_FIELDS, "effect state")
            _refuse_acceptance_state(data, raw_protocol)
            _refuse_config_fingerprint(data, raw_protocol)
            _refuse_validation_fields(data, raw_protocol)
            _refuse_operator_state(data, raw_protocol)
        elif raw_protocol == _PRE_CONFIG_PROTOCOL:
            # 8 -> 9 (#219): the same shape without the recorded
            # configuration, which it never wrote and which is refused.
            _require_fields(data, raw_protocol, _EFFECT_FIELDS + _ACCEPTANCE_FIELDS, "effect state")
            _refuse_config_fingerprint(data, raw_protocol)
            _refuse_validation_fields(data, raw_protocol)
            _refuse_operator_state(data, raw_protocol)
        elif raw_protocol == _PRE_VALIDATION_PROTOCOL:
            # 9 -> 10 (#172): the same shape without the validation
            # evidence, which it never wrote and which is refused.
            _require_fields(data, raw_protocol, _EFFECT_FIELDS + _ACCEPTANCE_FIELDS, "effect state")
            _require_fields(
                data,
                raw_protocol,
                _CONFIG_FIELDS,
                "recorded configuration",
                "not a run with none recorded",
            )
            _refuse_validation_fields(data, raw_protocol)
            _refuse_operator_state(data, raw_protocol)
        elif raw_protocol == _PREVIOUS_PROTOCOL:
            # 10 -> 11 (#243): the same shape without the operator-action
            # state, which it never wrote and which is refused.
            _require_fields(data, raw_protocol, _EFFECT_FIELDS + _ACCEPTANCE_FIELDS, "effect state")
            _require_fields(
                data,
                raw_protocol,
                _CONFIG_FIELDS,
                "recorded configuration",
                "not a run with none recorded",
            )
            _require_fields(
                data,
                raw_protocol,
                _VALIDATION_FIELDS,
                "validation evidence",
                "not a run with no evidence",
            )
            _refuse_operator_state(data, raw_protocol)
        elif raw_protocol != __protocol_version__:
            raise StateError(
                f"unsupported protocol_version {raw_protocol!r} "
                f"(controller speaks {__protocol_version__!r})"
            )
        else:
            # Only a legacy label loads with no effect state (D13.2). Every
            # protocol-7 save writes all four fields, so one that is absent
            # is a truncation or a hand edit, never "nothing planned": a
            # missing ``effect_records`` read as the empty default would let
            # the phase complete from its context with a pending effect never
            # performed.
            _require_fields(data, raw_protocol, _EFFECT_FIELDS + _ACCEPTANCE_FIELDS, "effect state")
            # Every protocol-9 save writes it, ``{}`` included, so an absent
            # one is not "not recorded": read as the empty default it would
            # switch the configuration check off for the rest of the run.
            _require_fields(
                data,
                raw_protocol,
                _CONFIG_FIELDS,
                "recorded configuration",
                "not a run with none recorded",
            )
            # Every protocol-10 save writes all six, so an absent one is a
            # truncation or a hand edit: a missing ``validation_attempt``
            # read as "no marker" would hide a crash the resume path must
            # record, and a missing ``review_certificate`` would read as one
            # never issued.
            _require_fields(
                data,
                raw_protocol,
                _VALIDATION_FIELDS,
                "validation evidence",
                "not a run with no evidence",
            )
            # Every protocol-11 save writes it, ``[]`` included, so an
            # absent one is a truncation or a hand edit: read as the empty
            # default it would drop a declined action of the round and let
            # the same request through again.
            _require_fields(
                data,
                raw_protocol,
                _OPERATOR_FIELDS,
                "operator-action outcomes",
                "not a PR with none",
            )
        # A LOCAL state written before the run contract existed has no
        # persisted definition of what it reviewed under. It is not migrated:
        # the only source a migration could fill the contract from is the
        # *current* configuration, and "missing field -> fill from current
        # config" is precisely the silent rebinding the contract forbids.
        if mode == WorkflowMode.LOCAL and (
            "local_workspace_policy" in data or "local_run_contract" not in data
        ):
            raise StateError(
                "state file is a LOCAL run created by a pre-release controller that did "
                "not persist the run contract; it is not migrated, because the contract "
                "cannot be reconstructed from the current configuration without "
                "redefining the run -- start a new run"
            )
        kwargs = dict(data)
        kwargs["phase"] = phase
        kwargs["mode"] = mode
        kwargs["protocol_version"] = __protocol_version__
        if raw_protocol not in (
            _PRE_CONFIG_PROTOCOL,
            _PRE_VALIDATION_PROTOCOL,
            _PREVIOUS_PROTOCOL,
            __protocol_version__,
        ) and (mode == WorkflowMode.REMOTE):
            # Before protocol 8 only: a protocol-8 file carries its own
            # acceptance state, which the migration would overwrite.
            kwargs.update(_acceptance_migration(data))
        if raw_protocol != __protocol_version__ and mode == WorkflowMode.REMOTE:
            # Before protocol 11 (#243): over the acceptance migration, so a
            # pre-8 file gains both.
            kwargs.update(_operator_migration(kwargs))
        if legacy and mode == WorkflowMode.REMOTE:
            attempt = data.get("attempt", 0)
            if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 1:
                kwargs["launch_label"] = (
                    LABEL_AGENT_PUBLISHES if phase in PUBLISHING_PHASES else LABEL_NONE
                )
        # Unknown fields are corruption, not forward compatibility: at this
        # protocol version the controller knows every field it writes, so an
        # unexpected key is a hand edit, a truncated merge or a foreign file.
        # Dropping it silently would let `local_fix_round` (say) sit next to a
        # `local_fix_rounds` that quietly kept its dataclass default of 0 —
        # substituting a default for a bound the operator meant to set.
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(kwargs) - known)
        if unknown:
            raise StateError(
                "state file has unknown field(s) "
                + ", ".join(repr(u) for u in unknown)
                + f" for protocol_version {__protocol_version__!r}; refusing to load — "
                "a field the controller does not know is corruption, not state"
            )
        try:
            state = cls(**{k: v for k, v in kwargs.items() if k in known})
        except TypeError as exc:
            raise StateError(f"state file has invalid fields: {exc}") from exc
        # A phase that exists is not a phase this run can be in. `Phase(...)`
        # above only proves the value is one the controller knows; LOCAL and
        # REMOTE are two topologies over that one enum, and the LOCAL one has
        # no REPLAN_REEXECUTE, READY_FOR_MERGE, MERGE or UPDATE_EPIC in it
        # (see transitions.LOCAL_PHASES). Without this a corrupt or hand-edited
        # `mode: LOCAL, phase: READY_FOR_MERGE` loads cleanly and `resume`
        # reads it as the remote merge hold -- printing a merge banner, and
        # with the gate open entering the GitHub pre-merge verification for a
        # run that has no repository, no PR and nothing to merge. A state whose
        # phase its own mode can never execute is corruption.
        if state.mode == WorkflowMode.LOCAL and state.phase not in LOCAL_PHASES:
            raise StateError(
                f"state file is a LOCAL run in phase {state.phase.value}, which belongs to "
                "the GitHub workflow and no local run can reach; refusing to load — "
                "a phase outside the mode's own topology is corruption, not state"
            )
        # Required-field sanity. A LOCAL run has no repository/EPIC at all;
        # its identity is the frozen feature specification instead.
        required = ["run_id", "created_at", "updated_at"]
        if state.mode == WorkflowMode.LOCAL:
            required += ["feature_spec_path", "feature_spec_sha256", "local_run_contract"]
        else:
            required += ["repository", "epic_url"]
        for req in required:
            if not getattr(state, req, None):
                raise StateError(f"state file missing required field {req!r}")
        # `run_id` is not just an identifier: it names `<state_dir>/logs/<run_id>`,
        # the directory every artifact of the run is written into. A hand-edited
        # or truncated state carrying "../escape" (or an absolute path) would
        # redirect those writes out of the state directory entirely, so the
        # shape is checked here — corruption fails on load, naming the state
        # file, rather than at the first log write.
        validate_run_id(state.run_id)
        # Types come from the dataclass, not from a list maintained by hand.
        # A `str` field that arrives as `null`, a list or a number is
        # corruption for the same reason whichever field it is, and a
        # hand-written enumeration only closes the fields somebody thought of:
        # `workspace_fingerprint: null` once loaded cleanly and reached the
        # review binding as a value no reviewer's fingerprint could equal.
        # Declaring the type *is* declaring the contract, so the declaration
        # is what gets checked.
        for name, annotation in _SCALAR_FIELDS.items():
            value = getattr(state, name)
            # `bool` is an `int` in Python and would sail through as 0 or 1.
            if not isinstance(value, annotation) or isinstance(value, bool):
                raise StateError(f"state field {name!r} must be {annotation.__name__}")
        # Range, not just type: `local_fix_rounds: -1` would sail past the
        # `>= max_fix_rounds` budget guard and buy the run unlimited extra fix
        # rounds, and a negative `step_count` does the same to the cumulative
        # step budget. A counter that cannot be trusted is not a counter.
        for name in _NON_NEGATIVE_FIELDS:
            if getattr(state, name) < 0:
                raise StateError(f"state field {name!r} must be a non-negative integer")
        _validate_local_pending(state)
        if not isinstance(state.local_run_contract, dict):
            raise StateError("state field 'local_run_contract' must be an object")
        if state.mode == WorkflowMode.LOCAL:
            try:
                LocalRunContract.from_dict(state.local_run_contract)
            except StateError as exc:
                raise StateError(f"state field 'local_run_contract': {exc}") from None
        elif state.local_run_contract:
            raise StateError("state field 'local_run_contract' must be empty for a REMOTE run")
        if not isinstance(state.baseline_dirty_paths, list) or not all(
            isinstance(path, str) for path in state.baseline_dirty_paths
        ):
            raise StateError("state field 'baseline_dirty_paths' must be a list of strings")
        if not isinstance(state.counted_merged_prs, list) or not all(
            isinstance(url, str) for url in state.counted_merged_prs
        ):
            raise StateError("state field 'counted_merged_prs' must be a list of strings")
        if not isinstance(state.premerge_verified_commands, list) or not all(
            isinstance(argv, list) and all(isinstance(arg, str) for arg in argv)
            for argv in state.premerge_verified_commands
        ):
            raise StateError(
                "state field 'premerge_verified_commands' must be a list of string lists"
            )
        _validate_findings_field("open_findings", state.open_findings)
        _validate_findings_field("prior_findings", state.prior_findings)
        if not isinstance(state.last_fix_resolutions, list) or not all(
            isinstance(resolution, dict) for resolution in state.last_fix_resolutions
        ):
            raise StateError("state field 'last_fix_resolutions' must be a list of objects")
        if not isinstance(state.next_issue_rejections, list) or not all(
            isinstance(reason, str) for reason in state.next_issue_rejections
        ):
            raise StateError("state field 'next_issue_rejections' must be a list of strings")
        try:
            validate_review_history(state.review_history)
        except StateError as exc:
            raise StateError(f"state field {exc}") from None
        if not isinstance(state.verification_failures, list) or not all(
            isinstance(reason, str) for reason in state.verification_failures
        ):
            raise StateError("state field 'verification_failures' must be a list of strings")
        if not isinstance(state.superseded_prs, list) or not all(
            isinstance(item, dict) for item in state.superseded_prs
        ):
            raise StateError("state field 'superseded_prs' must be a list of objects")
        if not isinstance(state.replan_transaction, dict):
            raise StateError("state field 'replan_transaction' must be an object")
        _validate_unblock_history(state.unblock_history)
        # After the shapes it reads (last_fix_resolutions, unblock_history).
        _validate_acceptance_state(state)
        _validate_operator_state(state)
        _validate_config_fingerprint(state)
        for name in _MINIMUM_ONE_FIELDS:
            if getattr(state, name) < 1:
                raise StateError(f"state field {name!r} must be a valid integer")
        if state.last_review_needs_fix is not None and not isinstance(
            state.last_review_needs_fix, bool
        ):
            raise StateError("state field 'last_review_needs_fix' must be a boolean or null")
        # The reviewed PR is compared by GitHub identity at the merge gate,
        # so it must parse as a PR URL of the run's repository whenever it is
        # set; a value the gate could not parse would otherwise surface as a
        # refusal to merge that names no cause. (Empty is "no review bound",
        # which the gate refuses on its own, exactly as for the HEAD.)
        if state.reviewed_pr_url:
            try:
                reviewed = parse_pr_url(state.reviewed_pr_url)
            except ConfigurationError as exc:
                raise StateError(f"state field 'reviewed_pr_url': {exc}") from None
            if state.repository and not reviewed.same_repository(state.repository):
                raise StateError(
                    f"state field 'reviewed_pr_url' names PR {state.reviewed_pr_url}, which "
                    f"is not in repository {state.repository!r}"
                )
        # The merge bases are compared for equality with the one GitHub
        # reports (a lower-case full SHA), so a value of any other shape
        # could only ever differ from it and would send every merge back to
        # REVIEW without naming a cause. (Empty is "nothing bound".)
        for name in ("reviewed_merge_base_sha", "current_merge_base_sha"):
            value = getattr(state, name)
            if value and not _SHA40_RE.fullmatch(value):
                raise StateError(
                    f"state field {name!r} must be a full lower-case commit SHA or empty, "
                    f"got {value!r:.60}"
                )
        state.validation_evidence()
        state.phase_effects()
        return state

    # -- candidate validation evidence (ADR 0006) --------------------------
    def validation_evidence(self) -> list[RecordView]:
        """The six evidence fields, checked together; the records, parsed (D11.1).

        Read-only (D11.3): a leftover marker is reported and validated here,
        never converted or cleared.
        """
        return check_evidence(
            EvidenceFields(
                validation_records=self.validation_records,
                validation_attempt=self.validation_attempt,
                review_evidence=self.review_evidence,
                review_certificate=self.review_certificate,
                producer_validation_failure=self.producer_validation_failure,
                validation_override_seq=self.validation_override_seq,
            ),
            local=self.mode == WorkflowMode.LOCAL,
            repository=self.repository,
            run_id=self.run_id,
        )

    def clear_validation_evidence(self) -> None:
        """All six fields, empty (D11.4): the next issue starts with none."""
        self.validation_records = []
        self.validation_attempt = {}
        self.review_evidence = {}
        self.review_certificate = {}
        self.producer_validation_failure = {}
        self.validation_override_seq = None

    # -- controller-owned effects (ADR 0004) -------------------------------
    def effect_binding(self) -> Binding:
        """What this state's effect records, observation and context must agree with."""
        txn = self.replan_transaction.get("transaction_id", "")
        return Binding(
            run_id=self.run_id,
            phase=self.phase,
            issue_url=self.current_issue_url,
            pr_url=self.current_pr_url,
            review_round=self.review_round,
            transaction_id=txn if isinstance(txn, str) else "",
            open_finding_ids=tuple(
                str(f.get("id", "")) for f in self.open_findings if isinstance(f, dict)
            ),
        )

    def phase_effects(self) -> PhaseEffects:
        """The persisted effect state of the current phase entry, strictly validated.

        Every value is checked against the state it is loaded with (the
        phase, issue, PR, round, open findings and replan transaction it is
        bound to; D4.6) and against the others (one phase, the context saved
        with the records, D2.4's bounds). A value that fails is corruption
        and raises :class:`StateError`; a LOCAL run carries none (D13.2).
        """
        if not isinstance(self.launch_label, str) or self.launch_label not in LAUNCH_LABELS:
            raise StateError(
                f"state field 'launch_label' must be one of {sorted(LAUNCH_LABELS)}, got "
                f"{self.launch_label!r:.60}"
            )
        if self.mode == WorkflowMode.LOCAL:
            present = [name for name in _EFFECT_FIELDS if getattr(self, name) not in ([], {}, "")]
            if present:
                raise StateError(
                    f"state field(s) {', '.join(repr(n) for n in present)} are set on a LOCAL "
                    "run, which performs no external effects; refusing to load"
                )
            return PhaseEffects()
        return load_phase_effects(
            self.effect_records,
            self.entry_observation,
            self.completion_context,
            self.effect_binding(),
        )

    def bound_acceptance_outcomes(self) -> None:
        """Drop the oldest ``acceptance_outcomes`` past the bound, never an active decline.

        A decline of the round being fixed refuses its action again for as
        long as the round lasts, so it is kept (PR #221 R3-F1); the entries
        dropped are earlier rounds' and the round's verified or stale ones.
        The round's declines are at most :data:`MAX_DECLINES_PER_ROUND`,
        below :data:`MAX_ACCEPTANCE_OUTCOMES`, so the bound always holds.
        """
        excess = len(self.acceptance_outcomes) - MAX_ACCEPTANCE_OUTCOMES
        kept: list[dict] = []
        for outcome in self.acceptance_outcomes:
            active = outcome["outcome"] == "declined" and outcome["round"] == self.review_round
            if excess > 0 and not active:
                excess -= 1
                continue
            kept.append(outcome)
        self.acceptance_outcomes = kept

    def round_declines(self) -> int:
        """Declines of the round being fixed, K9's and K10's together (#243).

        The one bound on decline-driven relaunches
        (:data:`MAX_DECLINES_PER_ROUND`) is shared by both kinds; a
        withdrawal is not a decline and is not counted.
        """
        return sum(
            1
            for outcomes in (self.acceptance_outcomes, self.operator_comment_outcomes)
            for o in outcomes
            if o["outcome"] == "declined" and o["round"] == self.review_round
        )

    def bound_operator_comment_outcomes(self) -> None:
        """Drop the oldest ``operator_comment_outcomes`` past the bound, never this round's.

        An entry of the round being fixed is what refuses a declined
        request again, and what tells the fixer and REVIEW what was
        verified, so it is kept (#243). One round fits the bound
        (:data:`MIN_OPERATOR_COMMENT_OUTCOMES`), so the bound always holds.
        """
        excess = len(self.operator_comment_outcomes) - MAX_OPERATOR_COMMENT_OUTCOMES
        kept: list[dict] = []
        for outcome in self.operator_comment_outcomes:
            if excess > 0 and outcome["round"] != self.review_round:
                excess -= 1
                continue
            kept.append(outcome)
        self.operator_comment_outcomes = kept

    def drop_phase_effects(self) -> None:
        """Forget the current entry's effect state: the save that commits the phase (D2.4)."""
        self.effect_records = []
        self.entry_observation = {}
        self.completion_context = {}
        self.launch_label = LABEL_NONE

    def settle_phase_effects(self) -> None:
        """D2.4 at every save: a phase entry's effect state does not outlive it.

        Records, observation and context name the phase they belong to; a
        save in another phase (the transition out of it) drops them, unless
        the run is ``BLOCKED`` or ``FAILED`` in that phase, which keeps them
        for the operator and for ``unblock`` back into it. The launch label
        describes the launches of the current entry, so it is dropped while
        none has been made (``attempt`` is reset on every transition).
        """
        if self.phase in (Phase.BLOCKED, Phase.FAILED):
            return
        named = {
            piece.get("phase")
            for piece in (self.completion_context, self.entry_observation)
            if isinstance(piece, dict) and piece
        }
        for record in self.effect_records if isinstance(self.effect_records, list) else ():
            owner = record.get("owner") if isinstance(record, dict) else None
            if isinstance(owner, dict):
                named.add(owner.get("phase"))
        if named and named != {self.phase.value}:
            self.drop_phase_effects()
        elif self.attempt == 0:
            self.launch_label = LABEL_NONE

    def settle_validation_failure(self) -> None:
        """ADR 0006 D5.2 at every save: a producer's failure counter is its phase's.

        Counted per phase entry, so the write that changes the phase (to
        the next phase, or to ``BLOCKED``) clears it, and the next entry of
        the phase counts from nothing.
        """
        counter = self.producer_validation_failure
        if isinstance(counter, dict) and counter and counter.get("phase") != self.phase.value:
            self.producer_validation_failure = {}

    def touch(self) -> None:
        self.updated_at = utcnow_iso()

    # -- merge accounting (idempotent) ----------------------------------
    def record_merge(self, pr_url: str) -> bool:
        """Record a merged PR. Returns True if newly counted.

        Idempotent: recording the same ``pr_url`` twice only counts once, so
        a retried MERGE step after a crash can never double-count.
        """
        if pr_url and pr_url not in self.counted_merged_prs:
            self.counted_merged_prs.append(pr_url)
            self.merged_since_epic_update += 1
            return True
        return False

    def record_epic_update(self) -> None:
        self.merged_since_epic_update = 0

    # -- per-issue bookkeeping ------------------------------------------
    def reset_for_new_issue(self, issue_url: str) -> None:
        """Clear all PR/review bookkeeping when moving to another issue.

        ``step_count`` is deliberately kept: it is the run's cumulative step
        budget and must survive issue switches and ``resume`` alike.
        """
        self.current_issue_url = issue_url
        self.current_pr_url = ""
        self.current_branch = ""
        self.current_head_sha = ""
        self.current_base_ref = ""
        self.current_merge_base_sha = ""
        self.reviewed_pr_url = ""
        self.reviewed_head_sha = ""
        self.reviewed_base_ref = ""
        self.reviewed_merge_base_sha = ""
        self.review_round = 0
        self.last_review_result = ""
        self.last_review_needs_fix = None
        self.last_review_comment_url = ""
        self.open_findings = []
        self.prior_findings = []
        self.last_fix_resolutions = []
        self.acceptance_outcomes = []
        self.operator_comment_outcomes = []
        self.review_history = []
        self.verification_failures = []
        self.premerge_verified_head_sha = ""
        self.premerge_verified_commands = []
        self.clear_validation_evidence()
        self.execution_attempt = 1
        self.escalation_count = 0
        self.superseded_prs = []
        self.replan_transaction = {}
        self.next_issue_rejections = []
        self.drop_phase_effects()
        self.attempt = 0

    @property
    def last_review_round(self) -> int:
        return self.review_round


_UNBLOCK_ENTRY_FIELDS = ("at", "reason", "block_reason", "phase", "detail")


def _require_fields(
    data: dict,
    protocol: object,
    names: tuple[str, ...],
    what: str,
    absent: str = "not an empty plan",
) -> None:
    """Refuse a file of ``protocol`` that lacks a field every save of it writes.

    ``absent`` is what the missing field must not be read as.
    """
    missing = [name for name in names if name not in data]
    if not missing:
        return
    label = (
        f"is labelled protocol_version {protocol!r}"
        if "protocol_version" in data
        else f"has no protocol_version label (read as {protocol!r})"
    )
    raise StateError(
        f"state file {label} but is missing "
        f"required field(s) {', '.join(repr(n) for n in missing)}; refusing to "
        f"load -- every save of this protocol writes the {what}, so an absent "
        f"field is corruption, {absent}"
    )


def _refuse_config_fingerprint(data: dict, protocol: str) -> None:
    """A file labelled before protocol 9 never recorded a configuration (#219).

    The label decides, not the value: ``config_fingerprint`` is protocol 9's
    own field, so an older file that carries it at all, even ``{}``, was not
    written by the controller its label names.
    """
    if "config_fingerprint" in data:
        raise StateError(
            f"state file is labelled protocol_version {protocol!r}, which never wrote "
            "'config_fingerprint'; refusing to load a file that carries protocol-9 "
            "recorded configuration -- it is a hand edit or a foreign file, not an older "
            "controller's state"
        )


def _refuse_validation_fields(data: dict, protocol: str) -> None:
    """A file labelled before protocol 10 never wrote validation evidence (#172).

    The label decides, not the value: an older file that carries one of the
    six fields at all, even empty, was not written by the controller its
    label names (ADR 0006 D15.4), and nothing it holds is merged.
    """
    planted = [name for name in _VALIDATION_FIELDS if name in data]
    if planted:
        raise StateError(
            f"state file is labelled protocol_version {protocol!r}, which never wrote "
            f"{', '.join(repr(n) for n in planted)}; refusing to load a file that carries "
            "protocol-10 validation evidence -- it is a hand edit or a foreign file, not an "
            "older controller's state"
        )


def _acceptance_state_in(data: dict) -> list[str]:
    """What of protocol 8's acceptance-action state (#211) ``data`` carries, named.

    The label decides, not the value: ``acceptance_outcomes`` is protocol
    8's own field, so an older file that carries it at all, even empty, was
    not written by the controller its label names.
    """
    found: list[str] = []
    if "acceptance_outcomes" in data:
        found.append("'acceptance_outcomes'")
    records = data.get("effect_records")
    if isinstance(records, list) and any(
        isinstance(r, dict) and r.get("kind") == EffectKind.ACCEPTANCE_COMMENT.value
        for r in records
    ):
        found.append("an 'acceptance_comment' effect record")
    context = data.get("completion_context")
    resolutions = context.get("resolutions") if isinstance(context, dict) else None
    if isinstance(resolutions, list) and any(
        isinstance(r, dict) and "acceptance_source" in r for r in resolutions
    ):
        found.append("'acceptance_source' in 'completion_context'")
    last = data.get("last_fix_resolutions")
    if isinstance(last, list) and any(
        isinstance(r, dict) and "acceptance_comment_url" in r for r in last
    ):
        found.append("'acceptance_comment_url' in 'last_fix_resolutions'")
    history = data.get("unblock_history")
    if isinstance(history, list) and any(isinstance(e, dict) and "action" in e for e in history):
        found.append("an 'action' in 'unblock_history'")
    return found


def _refuse_acceptance_state(data: dict, protocol: str) -> None:
    """A file labelled before protocol 8 never wrote acceptance-action state (#211)."""
    planted = _acceptance_state_in(data)
    if planted:
        raise StateError(
            f"state file is labelled protocol_version {protocol!r}, which never wrote "
            f"{', '.join(planted)}; refusing to load a file that carries protocol-8 "
            "acceptance-action state -- it is a hand edit or a foreign file, not an older "
            "controller's state"
        )


def _operator_state_in(data: dict) -> list[str]:
    """What of protocol 11's operator-action state (#243) ``data`` carries, named.

    The label decides, not the value: ``operator_comment_outcomes`` is
    protocol 11's own field, so an older file that carries it at all, even
    empty, was not written by the controller its label names.
    """
    found: list[str] = []
    if "operator_comment_outcomes" in data:
        found.append("'operator_comment_outcomes'")
    records = data.get("effect_records")
    if isinstance(records, list) and any(
        isinstance(r, dict) and r.get("kind") == EffectKind.OPERATOR_COMMENT.value for r in records
    ):
        found.append("an 'operator_comment' effect record")
    observation = data.get("entry_observation")
    if isinstance(observation, dict) and "operator_targets" in observation:
        found.append("'operator_targets' in 'entry_observation'")
    context = data.get("completion_context")
    resolutions = context.get("resolutions") if isinstance(context, dict) else None
    if isinstance(resolutions, list) and any(
        isinstance(r, dict) and "operator_sources" in r for r in resolutions
    ):
        found.append("'operator_sources' in 'completion_context'")
    last = data.get("last_fix_resolutions")
    if isinstance(last, list) and any(
        isinstance(r, dict) and "operator_evidence" in r for r in last
    ):
        found.append("'operator_evidence' in 'last_fix_resolutions'")
    return found


def _refuse_operator_state(data: dict, protocol: str) -> None:
    """A file labelled before protocol 11 never wrote operator-action state (#243)."""
    planted = _operator_state_in(data)
    if planted:
        raise StateError(
            f"state file is labelled protocol_version {protocol!r}, which never wrote "
            f"{', '.join(planted)}; refusing to load a file that carries protocol-11 "
            "operator-action state -- it is a hand edit or a foreign file, not an older "
            "controller's state"
        )


def _operator_migration(data: dict) -> dict:
    """The fields of an older REMOTE file as protocol 11 writes them (#243).

    A FIX context resolution gains ``operator_sources: null`` and a
    ``last_fix_resolutions`` entry ``operator_evidence: []``: an older
    controller never recorded an operator action. Malformed values are
    passed through for validation to refuse.
    """
    out: dict = {"operator_comment_outcomes": []}
    last = data.get("last_fix_resolutions")
    if isinstance(last, list):
        out["last_fix_resolutions"] = [
            {**r, "operator_evidence": []} if isinstance(r, dict) else r for r in last
        ]
    context = data.get("completion_context")
    if isinstance(context, dict) and isinstance(context.get("resolutions"), list):
        out["completion_context"] = {
            **context,
            "resolutions": [
                {**r, "operator_sources": None} if isinstance(r, dict) else r
                for r in context["resolutions"]
            ],
        }
    return out


def _acceptance_migration(data: dict) -> dict:
    """The fields of an older REMOTE file as protocol 8 writes them (#211).

    A FIX context resolution gains ``acceptance_source: null`` and a
    ``last_fix_resolutions`` entry ``acceptance_comment_url: ""``: an older
    controller never published an acceptance comment. Malformed values are
    passed through for validation to refuse.
    """
    out: dict = {}
    last = data.get("last_fix_resolutions")
    if isinstance(last, list):
        out["last_fix_resolutions"] = [
            {**r, "acceptance_comment_url": ""} if isinstance(r, dict) else r for r in last
        ]
    context = data.get("completion_context")
    if isinstance(context, dict) and isinstance(context.get("resolutions"), list):
        out["completion_context"] = {
            **context,
            "resolutions": [
                {**r, "acceptance_source": None} if isinstance(r, dict) else r
                for r in context["resolutions"]
            ],
        }
    return out


def _validate_config_fingerprint(state: AutoForgeState) -> None:
    """``config_fingerprint`` (#219): ``{}`` or a whole recorded configuration.

    A LOCAL run is defined by its run contract and records none. A REMOTE
    record that does not parse is corruption, never "not recorded": read as
    none, it would switch the check off.
    """
    recorded = state.config_fingerprint
    if not isinstance(recorded, dict):
        raise StateError("state field 'config_fingerprint' must be an object")
    if not recorded:
        return
    if state.mode == WorkflowMode.LOCAL:
        raise StateError(
            "state field 'config_fingerprint' is set on a LOCAL run, which its run contract "
            "defines; refusing to load"
        )
    try:
        RecordedConfiguration.from_dict(recorded)
    except StateError as exc:
        raise StateError(f"state field 'config_fingerprint': {exc}") from None


def _validate_acceptance_state(state: AutoForgeState) -> None:
    """``acceptance_outcomes`` and ``acceptance_comment_url`` (#211), strictly.

    A LOCAL run publishes nothing, so it carries neither, nor an unblock
    ``action``. A REMOTE ``last_fix_resolutions`` entry carries the URL of
    the comment the controller verified for an ``acceptance_action``
    resolution, and "" for any other.
    """
    outcomes = state.acceptance_outcomes
    if not isinstance(outcomes, list) or not all(isinstance(o, dict) for o in outcomes):
        raise StateError("state field 'acceptance_outcomes' must be a list of objects")
    if state.mode == WorkflowMode.LOCAL:
        if outcomes:
            raise StateError(
                "state field 'acceptance_outcomes' is set on a LOCAL run, which publishes "
                "nothing; refusing to load"
            )
        if any("acceptance_comment_url" in r for r in state.last_fix_resolutions):
            raise StateError(
                "state field 'last_fix_resolutions' carries 'acceptance_comment_url' on a "
                "LOCAL run, which publishes nothing; refusing to load"
            )
        if any(isinstance(e, dict) and "action" in e for e in state.unblock_history):
            raise StateError(
                "state field 'unblock_history' carries an acceptance 'action' on a LOCAL run, "
                "which has no acceptance actions; refusing to load"
            )
        return
    if len(outcomes) > MAX_ACCEPTANCE_OUTCOMES:
        raise StateError(
            f"state field 'acceptance_outcomes' holds {len(outcomes)} entries, over "
            f"{MAX_ACCEPTANCE_OUTCOMES}"
        )
    declines: dict[int, int] = {}
    for i, entry in enumerate(outcomes):
        what = f"state field 'acceptance_outcomes'[{i}]"
        if sorted(entry) != sorted(_ACCEPTANCE_OUTCOME_KEYS):
            raise StateError(f"{what} must hold exactly {', '.join(_ACCEPTANCE_OUTCOME_KEYS)}")
        if not isinstance(entry["action_id"], str) or not ACTION_ID_RE.match(entry["action_id"]):
            raise StateError(f"{what}.action_id must be an action id of the form A-<16 hex>")
        round_ = entry["round"]
        if not isinstance(round_, int) or isinstance(round_, bool) or round_ < 1:
            raise StateError(f"{what}.round must be an integer >= 1")
        fid = entry["finding_id"]
        match = (
            FINDING_ID_RE.match(fid)
            if isinstance(fid, str) and len(fid) <= MAX_FINDING_ID_CHARS
            else None
        )
        if match is None or int(match.group("round")) != round_:
            raise StateError(f"{what}.finding_id must be a finding id of its round")
        if entry["outcome"] not in ACCEPTANCE_OUTCOMES:
            raise StateError(f"{what}.outcome must be one of {ACCEPTANCE_OUTCOMES}")
        if not isinstance(entry["at"], str) or not entry["at"]:
            raise StateError(f"{what}.at must be a non-empty timestamp")
        reason = entry["reason"]
        if not isinstance(reason, str) or len(reason) > MAX_CONFLICT_REASON_CHARS:
            raise StateError(
                f"{what}.reason must be a string of at most {MAX_CONFLICT_REASON_CHARS} characters"
            )
        _comment_url_field(
            state, entry["comment_url"], f"{what}.comment_url", entry["outcome"] == "verified"
        )
        if entry["outcome"] == "declined":
            declines[round_] = declines.get(round_, 0) + 1
    for round_, count in declines.items():
        if count > MAX_DECLINES_PER_ROUND:
            raise StateError(
                f"state field 'acceptance_outcomes' holds {count} declines of review round "
                f"{round_}, over {MAX_DECLINES_PER_ROUND}, the most one round records"
            )
    for i, entry in enumerate(state.last_fix_resolutions):
        what = f"state field 'last_fix_resolutions'[{i}].acceptance_comment_url"
        if "acceptance_comment_url" not in entry:
            raise StateError(f"{what} is missing")
        _comment_url_field(
            state,
            entry["acceptance_comment_url"],
            what,
            entry.get("resolution") == "acceptance_action",
        )


def _operator_target_field(state: AutoForgeState, value: object, what: str) -> str:
    """``value`` is the canonical URL of another issue of the run's repository (#243)."""
    if not isinstance(value, str):
        raise StateError(f"{what} must be an issue URL string")
    try:
        target = parse_issue_url(value)
    except ConfigurationError as exc:
        raise StateError(f"{what}: {exc}") from None
    if target.canonical != value:
        raise StateError(f"{what} must be stored in its canonical form")
    if state.repository and not target.same_repository(state.repository):
        raise StateError(f"{what} is not an issue of repository {state.repository!r}")
    try:
        own = parse_issue_url(state.current_issue_url) if state.current_issue_url else None
    except ConfigurationError as exc:
        raise StateError(f"{what}: the run's issue URL is malformed ({exc})") from None
    if own is not None and target.same_target(own):
        raise StateError(f"{what} is the run's own issue, which an operator action never targets")
    return value


def _operator_comment_field(value: object, target: str, what: str, required: bool) -> None:
    """``value`` is a comment URL on ``target`` when ``required``, else ""."""
    if not isinstance(value, str):
        raise StateError(f"{what} must be a string")
    if not required:
        if value:
            raise StateError(f"{what} must be empty: no operator comment was verified")
        return
    try:
        comment = parse_comment_url(value)
    except ConfigurationError as exc:
        raise StateError(f"{what}: {exc}") from None
    if comment.canonical != value or not comment.on(parse_issue_url(target)):
        raise StateError(f"{what} is not a canonical comment URL on its target {target}")


def _digest_field(value: object, what: str, required: bool) -> None:
    if not isinstance(value, str) or not (
        _HEX64_RE.fullmatch(value) if required else value in ("",)
    ):
        raise StateError(
            f"{what} must be a 64-character lowercase SHA-256 hex digest"
            if required
            else f"{what} must be empty"
        )


def _validate_operator_evidence(state: AutoForgeState, value: object, what: str) -> None:
    """One ``last_fix_resolutions`` entry's ``operator_evidence`` (#243)."""
    if not isinstance(value, list) or len(value) > MAX_OPERATOR_COMMENT_OUTCOMES:
        raise StateError(
            f"{what} must be a list of at most {MAX_OPERATOR_COMMENT_OUTCOMES} objects"
        )
    for j, item in enumerate(value):
        where = f"{what}[{j}]"
        if not isinstance(item, dict) or sorted(item) != sorted(_OPERATOR_EVIDENCE_KEYS):
            raise StateError(f"{where} must hold exactly {', '.join(_OPERATOR_EVIDENCE_KEYS)}")
        action_id = item["action_id"]
        if not isinstance(action_id, str) or not OPERATOR_ACTION_ID_RE.match(action_id):
            raise StateError(f"{where}.action_id must be an action id of the form X-<16 hex>")
        target = _operator_target_field(state, item["target"], f"{where}.target")
        _operator_comment_field(item["comment_url"], target, f"{where}.comment_url", True)
        if item["evidence"] not in EVIDENCE_CLASSES:
            raise StateError(f"{where}.evidence must be one of {EVIDENCE_CLASSES}")
        _digest_field(item["body_sha256"], f"{where}.body_sha256", True)
        _digest_field(item["payload_sha256"], f"{where}.payload_sha256", True)


def _validate_operator_state(state: AutoForgeState) -> None:
    """``operator_comment_outcomes`` and ``operator_evidence`` (#243), strictly.

    A LOCAL run has no GitHub and no operator actions, so it carries
    neither. A REMOTE ``last_fix_resolutions`` entry carries the operator
    comments verified for its finding, ``[]`` when none. A ``verified``
    outcome is unique per (action id, comment), and the round's declines,
    K9's and K10's together, stay within :data:`MAX_DECLINES_PER_ROUND`.
    """
    outcomes = state.operator_comment_outcomes
    if not isinstance(outcomes, list) or not all(isinstance(o, dict) for o in outcomes):
        raise StateError("state field 'operator_comment_outcomes' must be a list of objects")
    if state.mode == WorkflowMode.LOCAL:
        if outcomes:
            raise StateError(
                "state field 'operator_comment_outcomes' is set on a LOCAL run, which has no "
                "operator actions; refusing to load"
            )
        if any("operator_evidence" in r for r in state.last_fix_resolutions):
            raise StateError(
                "state field 'last_fix_resolutions' carries 'operator_evidence' on a LOCAL run, "
                "which has no operator actions; refusing to load"
            )
        return
    if len(outcomes) > MAX_OPERATOR_COMMENT_OUTCOMES:
        raise StateError(
            f"state field 'operator_comment_outcomes' holds {len(outcomes)} entries, over "
            f"{MAX_OPERATOR_COMMENT_OUTCOMES}"
        )
    verified: set[tuple[str, int]] = set()
    for i, entry in enumerate(outcomes):
        what = f"state field 'operator_comment_outcomes'[{i}]"
        if sorted(entry) != sorted(_OPERATOR_OUTCOME_KEYS):
            raise StateError(f"{what} must hold exactly {', '.join(_OPERATOR_OUTCOME_KEYS)}")
        action_id = entry["action_id"]
        if not isinstance(action_id, str) or not OPERATOR_ACTION_ID_RE.match(action_id):
            raise StateError(f"{what}.action_id must be an action id of the form X-<16 hex>")
        round_ = entry["round"]
        if not isinstance(round_, int) or isinstance(round_, bool) or round_ < 1:
            raise StateError(f"{what}.round must be an integer >= 1")
        fid = entry["finding_id"]
        match = (
            FINDING_ID_RE.match(fid)
            if isinstance(fid, str) and len(fid) <= MAX_FINDING_ID_CHARS
            else None
        )
        if match is None or int(match.group("round")) != round_:
            raise StateError(f"{what}.finding_id must be a finding id of its round")
        target = _operator_target_field(state, entry["target"], f"{what}.target")
        outcome = entry["outcome"]
        if outcome not in OPERATOR_COMMENT_OUTCOMES:
            raise StateError(f"{what}.outcome must be one of {OPERATOR_COMMENT_OUTCOMES}")
        is_verified = outcome == "verified"
        if is_verified:
            if entry["evidence"] not in EVIDENCE_CLASSES:
                raise StateError(f"{what}.evidence must be one of {EVIDENCE_CLASSES}")
        elif entry["evidence"] != "":
            raise StateError(f"{what}.evidence must be empty for a {outcome} action")
        _operator_comment_field(entry["comment_url"], target, f"{what}.comment_url", is_verified)
        _digest_field(entry["body_sha256"], f"{what}.body_sha256", is_verified)
        _digest_field(entry["payload_sha256"], f"{what}.payload_sha256", True)
        if not isinstance(entry["at"], str) or not entry["at"]:
            raise StateError(f"{what}.at must be a non-empty timestamp")
        reason = entry["reason"]
        if not isinstance(reason, str) or len(reason) > MAX_CONFLICT_REASON_CHARS:
            raise StateError(
                f"{what}.reason must be a string of at most {MAX_CONFLICT_REASON_CHARS} characters"
            )
        if is_verified:
            key = (action_id, parse_comment_url(entry["comment_url"]).comment_id)
            if key in verified:
                raise StateError(f"{what} verifies action {action_id} on one comment twice")
            verified.add(key)
    declines: dict[int, int] = {}
    for entry in (*state.acceptance_outcomes, *outcomes):
        if entry["outcome"] == "declined":
            declines[entry["round"]] = declines.get(entry["round"], 0) + 1
    for round_, count in declines.items():
        if count > MAX_DECLINES_PER_ROUND:
            raise StateError(
                f"state fields 'acceptance_outcomes' and 'operator_comment_outcomes' hold "
                f"{count} declines of review round {round_}, over {MAX_DECLINES_PER_ROUND}, the "
                "most one round records"
            )
    for i, entry in enumerate(state.last_fix_resolutions):
        what = f"state field 'last_fix_resolutions'[{i}].operator_evidence"
        if "operator_evidence" not in entry:
            raise StateError(f"{what} is missing")
        _validate_operator_evidence(state, entry["operator_evidence"], what)


def _comment_url_field(state: AutoForgeState, value: object, what: str, required: bool) -> None:
    """``value`` is a comment URL on the current issue when ``required``, else ""."""
    if not isinstance(value, str):
        raise StateError(f"{what} must be a string")
    if not required:
        if value:
            raise StateError(f"{what} must be empty: no acceptance comment was verified")
        return
    try:
        comment = parse_comment_url(value)
        issue = parse_issue_url(state.current_issue_url)
    except ConfigurationError as exc:
        raise StateError(f"{what}: {exc}") from None
    if not comment.on(issue):
        raise StateError(f"{what} is not a comment on the current issue")


def _validate_unblock_history(history: object) -> None:
    """``unblock_history`` is a list of complete, string-valued entries.

    Each entry names the phase the operator re-entered, so a value that is
    not a phase (or an entry missing a field) is corruption: the trail would
    otherwise claim a transition the topology has no name for.
    """
    if not isinstance(history, list):
        raise StateError("state field 'unblock_history' must be a list of objects")
    for entry in history:
        if not isinstance(entry, dict):
            raise StateError("state field 'unblock_history' must be a list of objects")
        for key in _UNBLOCK_ENTRY_FIELDS:
            if not isinstance(entry.get(key), str):
                raise StateError(
                    f"state field 'unblock_history': every entry needs a string {key!r}"
                )
        if not entry["at"] or not entry["phase"]:
            raise StateError("state field 'unblock_history': 'at' and 'phase' must be non-empty")
        if "action" in entry and (not isinstance(entry["action"], str) or not entry["action"]):
            # #211: the acceptance action an unblock approved or declined.
            raise StateError("state field 'unblock_history': 'action' must be a non-empty string")
        try:
            Phase(entry["phase"])
        except ValueError:
            raise StateError(
                f"state field 'unblock_history': {entry['phase']!r} is not a phase"
            ) from None


# Every field the dataclass declares as a plain `str` or `int`, derived from
# the declaration itself so that adding a field cannot forget to validate it.
# Fields with richer types (the enums, the lists, the dicts) are checked
# individually in `from_dict`, because "is a list" is rarely the whole
# contract for them.
_SCALAR_FIELDS: dict[str, type] = {
    f.name: {"str": str, "int": int}[f.type]
    for f in fields(AutoForgeState)
    if f.type in ("str", "int")
}

# The counters that bound the run. A negative one does not merely look wrong:
# it disables the bound it is compared against.
_NON_NEGATIVE_FIELDS = (
    "local_fix_rounds",
    "local_pending_attempts",
    "step_count",
    "review_round",
    "attempt",
    "merged_since_epic_update",
    "escalation_count",
)

# `execution_attempt` counts from one: attempt zero never happened.
_MINIMUM_ONE_FIELDS = ("execution_attempt",)


# -- paths ---------------------------------------------------------------
@dataclass(frozen=True)
class StatePaths:
    """Where a run's state and logs live, and the directory they are reached from.

    ``anchor`` is the one directory in the chain that AutoForge did not
    create and therefore resolves by pathname; ``relative`` is the path from
    it to the state directory, and every component of it is opened
    descriptor-relative with ``O_NOFOLLOW`` (see :mod:`autoforge.safefs`).
    That split is the whole of the trust statement: for a LOCAL run the
    anchor is the repository's git directory and the components below it are
    AutoForge's own, so replacing one of them with a symbolic link cannot
    move a controller write.

    The controller lock is *not* here: it is keyed by the repository
    identity, not by the caller-selectable state directory (see
    :func:`autoforge.locking.repository_lock_path`).
    """

    state_dir: Path
    state_file: Path
    logs_dir: Path
    anchor: Path
    relative: tuple[str, ...]

    @classmethod
    def from_state_dir(
        cls, state_dir: str | Path, *, anchor: str | Path | None = None
    ) -> StatePaths:
        d = Path(state_dir)
        absolute = Path(os.path.abspath(d))
        if anchor is None:
            anchor_path = absolute.parent
            relative: tuple[str, ...] = (absolute.name,)
        else:
            anchor_path = Path(os.path.abspath(anchor))
            try:
                relative = absolute.relative_to(anchor_path).parts
            except ValueError:
                raise StateError(
                    f"state directory {absolute} is not inside its anchor {anchor_path}"
                ) from None
        return cls(
            state_dir=d,
            state_file=d / STATE_FILENAME,
            logs_dir=d / LOGS_DIRNAME,
            anchor=anchor_path,
            relative=tuple(relative),
        )

    def canonical_state_dir(self) -> str:
        """The state directory's canonical absolute pathname (contract form)."""
        return os.path.realpath(os.path.abspath(self.state_dir))

    def open_root(self, *, create: bool = True) -> SafeRoot:
        """Open the state directory as a capability (the caller closes it).

        This is a *pathname* operation and is therefore performed once per
        run by the engine, which then holds the capability for its lifetime
        (see ``Engine.state_root``): every later write goes through the held
        descriptor and ``SafeRoot.verify_identity`` proves the pathname still
        names it. Opening a second time would bind whatever the pathname
        names *now*, which is the rebinding the held capability exists to
        prevent, so nothing in the controller calls this twice for one run.
        """
        root = SafeRoot.open(self.anchor, create=create)
        if not self.relative:
            return root
        try:
            return root.subroot("/".join(self.relative), create=create)
        finally:
            root.close()


# -- persistence ----------------------------------------------------------
def _root_for(
    path: Path, root: SafeRoot | None, *, create: bool = True
) -> tuple[SafeRoot, str, bool]:
    """The capability a state-file operation runs through, and the entry name.

    Callers that already hold a root (the engine does, for the whole run)
    pass it and keep the stronger anchor described on :class:`StatePaths`.
    Callers with only a pathname get a root on the file's own directory:
    weaker in that the directory itself is resolved by pathname, identical in
    every other respect -- the entry, its temporary and the rename that
    publishes it are all named relative to one descriptor.

    ``create`` is false for reads: looking for state must never bring the
    directory that would hold it into existence.
    """
    if root is not None:
        return root, Path(path).name, False
    dest = Path(os.path.abspath(path))
    return SafeRoot.open(dest.parent, create=create), dest.name, True


def save_state(state: AutoForgeState, path: str | Path, *, root: SafeRoot | None = None) -> None:
    """Atomically persist state: temp file + fsync + atomic replace + fsync dir.

    The replace publishes a *name*, never a truncation of whatever inode the
    name happened to reach, so a hard link planted at ``state.json`` keeps its
    contents and a concurrent reader sees one whole version or the other.
    """
    state.touch()
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    fs, name, owned = _root_for(Path(path), root)
    try:
        fs.write_bytes(name, payload.encode("utf-8"), mode=0o600)
    finally:
        if owned:
            fs.close()


def _not_regular(p: Path, kind: str) -> StateError:
    """The state file's name holds something the controller did not write.

    ``kind`` comes from ``lstat``, so a symbolic link is reported as a
    symbolic link and never resolved.  What it points at is deliberately not
    part of the message: the controller refuses the link itself, dangling or
    not, so classifying the target would only invite the reader to think some
    targets would have been acceptable.
    """
    # A directory cannot be archived by quarantine_state_file (no hard links
    # to directories), so 'run --force' is no way out of it.
    hint = (
        "move it out of the way by hand"
        if kind == "directory"
        else "move it aside or use 'run --force'"
    )
    return StateError(
        f"corrupted state file {p}: it is a {kind}, not a regular file; "
        f"refusing to overwrite — {hint}"
    )


def no_state_error(path: str | Path) -> StateError:
    """There is no run at ``path`` (and a read never creates one)."""
    return StateError(
        f"no state file at {Path(path)} — run 'autoforge run --epic ... --issue ...' first; "
        "'resume' never creates a new run silently"
    )


def load_state(path: str | Path, *, root: SafeRoot | None = None) -> AutoForgeState:
    """Load state; raises StateError (never silently re-inits) on problems."""
    p = Path(path)
    missing = no_state_error(p)
    try:
        fs, name, owned = _root_for(p, root, create=False)
    except FileNotFoundError:
        raise missing from None
    try:
        st = fs.lstat(name)
        if st is None:
            raise missing
        kind = entry_kind(st.st_mode)
        if kind is not None:
            # A symbolic link is refused as such, dangling or not: the
            # controller reads and replaces its own regular file, and
            # following a link would put state where the operator is not
            # looking for it.
            raise _not_regular(p, kind)
        try:
            raw_bytes = fs.read_bytes(name, limit=MAX_STATE_FILE_BYTES)
        except ReadLimitExceeded as exc:
            # Refused before it is held: the bounded read stops one byte past
            # the limit, so a sparse or oversized file costs at most that.
            raise StateError(
                f"corrupted state file {p}: larger than {MAX_STATE_FILE_BYTES} bytes, "
                "which no controller state can be; "
                "refusing to overwrite — restore from backup or re-run"
            ) from exc
        except OSError as exc:
            raise StateError(f"cannot read state file {p}: {exc}") from exc
        if raw_bytes is None:  # pragma: no cover - lstat above just found it
            raise StateError(f"no state file at {p}")
    finally:
        if owned:
            fs.close()
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Invalid UTF-8 is a corrupt file, not a read failure: it must take
        # the same fail-loud / quarantine path as unparseable JSON.
        raise StateError(
            f"corrupted state file {p}: not valid UTF-8 ({exc}); "
            "refusing to overwrite — restore from backup or re-run"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"corrupted state file {p}: invalid JSON ({exc}); "
            "refusing to overwrite — restore from backup or re-run"
        ) from exc
    except (ValueError, RecursionError) as exc:
        # The decoder's own limits, which are not JSONDecodeError: an integer
        # past ``sys.get_int_max_str_digits()`` and nesting past the recursion
        # limit or the C stack, both inside MAX_STATE_FILE_BYTES. Corrupt like
        # unparseable JSON, reported by type only; an interrupt still propagates.
        raise StateError(
            f"corrupted state file {p}: exceeds the JSON decoder's integer or nesting "
            f"limit ({type(exc).__name__}); "
            "refusing to overwrite — restore from backup or re-run"
        ) from exc
    return AutoForgeState.from_dict(data)


_QUARANTINE_MAX_ATTEMPTS = 1000


def quarantine_state_file(
    path: str | Path, *, root: SafeRoot | None = None, what: str = "corrupted state file"
) -> Path:
    """Move an unreadable state file aside instead of deleting it.

    Renames ``<path>`` to ``<path>.corrupt-<UTC timestamp>`` (a numeric
    suffix is appended if that name is already taken) and returns the new
    path.  Never overwrites an existing file: the destination is reserved
    with :meth:`~autoforge.safefs.SafeRoot.link`, which fails atomically with
    ``EEXIST`` when the name is already taken (a plain rename would silently
    replace a file created between the existence check and the move).  The
    directory entry itself is moved: a symbolic link (dangling or not) is
    archived as a link and the file it points to is never followed, modified
    or removed; a FIFO, socket or device entry is archived as that entry
    without being opened.  A directory cannot be hard-linked and is refused:
    it stays untouched and must be moved aside by hand.  Raises StateError
    when the move fails; the original entry is left untouched in that case.

    Where the filesystem makes no hard links (vfat/exFAT, some FUSE, SMB and
    overlay mounts; :class:`~autoforge.safefs.HardLinksUnavailable`) the
    name is reserved with
    :meth:`~autoforge.safefs.SafeRoot.copy_entry_exclusive` instead, which
    keeps the no-replace guarantee: a regular file is copied into a name
    created ``O_CREAT | O_EXCL`` and fsynced, a symbolic link is recreated
    with ``symlink(2)``.  A FIFO, socket or device cannot be reproduced that
    way and is refused there, untouched, like a directory is everywhere.

    Reservation and removal of the source are two syscalls on either path.
    A crash between them leaves ``path`` in place beside a complete archive
    of it; the next ``run --force`` archives it again under a new name, so
    the outcome is a duplicate copy and never a lost one.  (On the copy path
    a crash *during* the copy leaves a partial archive under a name later
    reservations will not reuse, beside the untouched source.)  The caller
    must hold the controller lock (the CLI does, via
    ``ControllerEngine.locked()``): a writer replacing ``path`` between the
    two syscalls would see the replacement removed.

    ``what`` names the file in every refusal: the scheduler quarantines its
    own ``scheduler.json`` with this primitive (``epic run --force``, ADR
    0008 D6.3, :func:`autoforge.scheduler_state.quarantine_scheduler_state`).
    """
    return _move_state_file_aside(
        path, root=root, suffix=CORRUPT_SUFFIX, what=what, verb="quarantined"
    )


def archive_state_file(
    path: str | Path, *, root: SafeRoot | None = None, what: str = "state file"
) -> Path:
    """Move a *readable* state file aside as ``<path>.archived-<UTC timestamp>``.

    The same primitive as :func:`quarantine_state_file`, with its own
    suffix: the name is reserved by ``link(2)`` (or an exclusive copy where
    the filesystem makes no hard links) and only then is ``path`` unlinked,
    so a crash between the two leaves both names and a re-run moves
    ``path`` aside again, under a new name; a state is never lost. Used by a
    scheduler namespace's archive (ADR 0008 D17.4,
    :func:`autoforge.run_namespace.archive_issue_state`), whose caller holds
    the issue's exclusive authority as quarantining's holds the controller
    lock. Raises StateError when there is nothing at ``path`` or the move
    fails; the original entry is left untouched in that case. ``what`` names
    the file in every refusal: ``epic archive`` moves a corrupt ``claim.json``
    aside with this primitive (:func:`autoforge.scheduler_state.archive_claim`).
    """
    return _move_state_file_aside(
        path, root=root, suffix=ARCHIVED_SUFFIX, what=what, verb="archived"
    )


def _move_state_file_aside(
    path: str | Path, *, root: SafeRoot | None, suffix: str, what: str, verb: str
) -> Path:
    """Rename ``path`` to ``<path><suffix><UTC timestamp>`` without replacing anything.

    The link-then-unlink primitive behind :func:`quarantine_state_file` (its
    docstring is the contract) and :func:`archive_state_file`. ``what`` names
    the file and ``verb`` the operation in every refusal.
    """
    src = Path(path)
    fs, name, owned = _root_for(src, root)
    try:
        st = fs.lstat(name)
        if st is None:
            raise StateError(f"cannot move {what} {src} aside: it is gone")
        if stat.S_ISDIR(st.st_mode):
            raise StateError(
                f"cannot move {what} {src} aside: it is a directory; "
                "move it out of the way by hand and re-run"
            )
        source_identity = (st.st_dev, st.st_ino, st.st_mode)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = f"{name}{suffix}{stamp}"
        candidate = base
        hard_links = True

        def reserve(candidate: str) -> None:
            # link(2) reserves the name and the bytes in one syscall.  Where
            # the filesystem has none, the entry is reproduced under a name
            # created O_EXCL (or by symlink(2)), which is equally no-replace;
            # once that has been learned, later attempts skip the link.
            nonlocal hard_links
            if hard_links:
                try:
                    fs.link(name, candidate)
                    return
                except HardLinksUnavailable:
                    hard_links = False
            fs.copy_entry_exclusive(name, candidate)

        for n in range(1, _QUARANTINE_MAX_ATTEMPTS + 1):
            try:
                reserve(candidate)
            except FileExistsError:
                candidate = f"{base}.{n}"
                continue
            except StateError as exc:
                raise StateError(f"cannot move {what} {src} aside: {exc}") from exc
            current = fs.lstat(name)
            current_identity = (
                None if current is None else (current.st_dev, current.st_ino, current.st_mode)
            )
            if current_identity != source_identity:
                try:
                    fs.unlink(candidate)
                except StateError:  # pragma: no cover - cleanup best effort
                    pass
                raise StateError(
                    f"cannot move {what} {src} aside: it changed while being "
                    f"{verb}; refusing to remove the replacement"
                )
            try:
                fs.unlink(name)
            except StateError as exc:
                # Drop the reservation so the original is the only copy again,
                # and report the operation that failed rather than the syscall:
                # the caller asked to quarantine a file, not to unlink a name.
                try:
                    fs.unlink(candidate)
                except StateError:  # pragma: no cover - cleanup best effort
                    pass
                raise StateError(f"cannot move {what} {src} aside: {exc}") from exc
            return src.with_name(candidate)
        raise StateError(
            f"cannot move {what} {src} aside: "
            f"no free name after {_QUARANTINE_MAX_ATTEMPTS} attempts (last tried {candidate})"
        )
    finally:
        if owned:
            fs.close()
