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
put with an error).  The entry is inspected with ``lstat``/``fstat`` and
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
from .errors import ConfigurationError, StateError
from .loop_guard import validate_review_history
from .replan_txn import LEGACY_JOURNAL_PROTOCOLS, legacy_journal_refusal
from .result_parser import FINDING_ID_RE, MAX_FINDING_ID_CHARS
from .run_contract import LocalRunContract
from .runlog import validate_run_id
from .safefs import ReadLimitExceeded, SafeRoot, entry_kind
from .transitions import LOCAL_PHASES, LOCAL_WRITE_PHASES, Phase, WorkflowMode
from .validation import parse_pr_url

STATE_FILENAME = "state.json"
LOGS_DIRNAME = "logs"
CORRUPT_SUFFIX = ".corrupt-"
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
# :meth:`AutoForgeState.from_dict`). Every step so far changed the replan
# journal, so the set is the journal's; protocols 2 and 3 also changed the
# review binding (``_REVIEW_BINDING_GAPS``).
_LEGACY_PROTOCOLS = LEGACY_JOURNAL_PROTOCOLS
# The phases in which the persisted clean review is consumed by the merge
# gate, and so the phases a state without the review's full binding cannot
# be loaded in.
_MERGE_PHASES = (Phase.READY_FOR_MERGE, Phase.MERGE)
# What each pre-protocol-4 label failed to record about the completed
# review, for the refusal text: the binding the merge gate requires that
# the file cannot supply.
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
    if phase not in _MERGE_PHASES:
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
    last_fix_resolutions: list[dict] = field(default_factory=list)
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

    # Agent invocations attempted for the current phase (reset on transition).
    attempt: int = 0
    # Total executed steps across the run (never reset).
    step_count: int = 0
    # Human-readable reason when phase is BLOCKED/FAILED.
    block_reason: str = ""
    # The operator's explicit exits from BLOCKED (``autoforge unblock``), oldest
    # first, one object per unblock that was applied: ``at`` (UTC timestamp),
    # ``reason`` (the operator's text), ``block_reason`` (the reason that was
    # cleared), ``phase`` (the phase re-entered) and ``detail`` (why the
    # controller chose it). A refused unblock is not recorded here -- nothing
    # changed -- but in the run log. Kept across issues: it is the run's audit
    # trail, not per-PR bookkeeping.
    unblock_history: list[dict] = field(default_factory=list)

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
        #
        # The label is rewritten on the next save. A file that is refused is
        # left unchanged (``run --force`` moves it aside like any unreadable
        # one).
        raw_protocol = data.get("protocol_version", __protocol_version__)
        written_by = str(data.get("controller_version", ""))
        if isinstance(raw_protocol, str) and raw_protocol in _LEGACY_PROTOCOLS:
            refusal = legacy_journal_refusal(
                data.get("replan_transaction", {}), protocol=raw_protocol, written_by=written_by
            )
            if refusal:
                raise StateError(refusal)
            refusal = _legacy_review_binding_refusal(
                data, phase, protocol=str(raw_protocol), written_by=written_by
            )
            if refusal:
                raise StateError(refusal)
        elif raw_protocol != __protocol_version__:
            raise StateError(
                f"unsupported protocol_version {raw_protocol!r} "
                f"(controller speaks {__protocol_version__!r})"
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
        return state

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
        self.review_history = []
        self.verification_failures = []
        self.premerge_verified_head_sha = ""
        self.premerge_verified_commands = []
        self.execution_attempt = 1
        self.escalation_count = 0
        self.superseded_prs = []
        self.replan_transaction = {}
        self.next_issue_rejections = []
        self.attempt = 0

    @property
    def last_review_round(self) -> int:
        return self.review_round


_UNBLOCK_ENTRY_FIELDS = ("at", "reason", "block_reason", "phase", "detail")


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
    return AutoForgeState.from_dict(data)


_QUARANTINE_MAX_ATTEMPTS = 1000


def quarantine_state_file(path: str | Path, *, root: SafeRoot | None = None) -> Path:
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

    The caller must hold the controller lock (the CLI does, via
    ``ControllerEngine.locked()``): link and unlink are two syscalls, and a
    writer replacing ``path`` in between would see the replacement removed.
    """
    src = Path(path)
    fs, name, owned = _root_for(src, root)
    try:
        st = fs.lstat(name)
        if st is None:
            raise StateError(f"cannot move corrupted state file {src} aside: it is gone")
        if stat.S_ISDIR(st.st_mode):
            raise StateError(
                f"cannot move corrupted state file {src} aside: it is a directory; "
                "move it out of the way by hand and re-run"
            )
        source_identity = (st.st_dev, st.st_ino, st.st_mode)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = f"{name}{CORRUPT_SUFFIX}{stamp}"
        candidate = base
        for n in range(1, _QUARANTINE_MAX_ATTEMPTS + 1):
            try:
                fs.link(name, candidate)
            except FileExistsError:
                candidate = f"{base}.{n}"
                continue
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
                    f"cannot move corrupted state file {src} aside: it changed while being "
                    "quarantined; refusing to remove the replacement"
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
                raise StateError(f"cannot move corrupted state file {src} aside: {exc}") from exc
            return src.with_name(candidate)
        raise StateError(
            f"cannot move corrupted state file {src} aside: "
            f"no free name after {_QUARANTINE_MAX_ATTEMPTS} attempts (last tried {candidate})"
        )
    finally:
        if owned:
            fs.close()
