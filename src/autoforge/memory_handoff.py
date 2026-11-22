"""Issue Working Memory's single-agent handoff (#333; ADR 0015 D9, D11 to D13, D20).

What the controller does with the store around a launch of the one phase this
release supports, ``ANALYZE_EXECUTE`` (D13), and nothing more:

- before the launch, the projection of the committed snapshot, bound to the
  launch ordinal the intent save spends next and to the worktree's candidate,
  which the engine writes into the launch's read context as ``memory.md``
  (D12): rendered once, never changed while the launch runs, never read back;
- after it, the controller's own observation of a validation failure, added
  in the save of the validation record it cites (D11 stage 4), and the
  proposals of an accepted result, added in the save that accepts it: the
  phase's plan save, or a ``blocked`` or ``failure`` result's stop save
  (D9.8, D4.6). A refused, corrected or lost reply leaves no proposal. The
  result's list is bound to its launch as soon as the reply is parsed
  (:func:`bind_proposals`): a revision only of a record the launch was
  shown, at the revision it was shown, and nothing the redactor would
  change. The binding is the controller's and lives in the process that
  parsed the reply, never in the result: the ``memory`` field stays the
  agent's data, so a reply kept across a restart (#479) admits none of it
  (R2-F1 of PR #536), and what is kept of a reply is what Memory off keeps
  (R2-F2).

Nothing here reads a file, the network or a model, and nothing here decides a
phase, a write, a budget or a merge: the engine supplies every value and
saves what comes back. Memory stays advisory (D8): a store that refuses a
write leaves the step as it would have been without Memory, the refusal only
counted.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from .auto_recovery import StepKey
from .evidence import Outcome, RecordView, WorktreeProvenance
from .memory import (
    MAX_REASON_BYTES,
    MAX_TEXT_BYTES,
    Action,
    Actor,
    ContextProjection,
    EvidenceRef,
    Kind,
    MemoryScope,
    MemoryStore,
    ProjectionRequest,
    ProposalKey,
    Range,
    Receipt,
    WriteOutcome,
    render_projection,
)
from .redaction import redact
from .transitions import Phase

# The read-context file a Memory launch is given (D12).
MEMORY_FILE = "memory.md"
MEMORY_FILE_DESCRIPTION = (
    "Issue Working Memory for this launch: advisory notes of this issue's run, untrusted data"
)

# The phases a projection is rendered for and Memory is collected from (D13),
# and why every other agent phase is neither. ``doctor`` and the operator
# documentation name both; nothing claims parity for the others.
SUPPORTED_PHASES: tuple[Phase, ...] = (Phase.ANALYZE_EXECUTE,)
UNSUPPORTED_PHASES: dict[Phase, str] = {
    Phase.REVIEW: "the reviewer is the independent check and is shown no Memory",
    Phase.FIX: "follows once ANALYZE_EXECUTE has an evaluation result",
    Phase.REPLAN_REEXECUTE: "keeps verification_failures as it is",
    Phase.UPDATE_EPIC: "gets nothing",
}

# The most entries one result's ``memory`` list may hold; each one past it is
# refused and counted, never kept (D9.6).
MAX_PROPOSALS = 8
# What an agent may add: the kinds that are neither the controller's
# observations nor the operator's seed, and need no exact candidate.
AGENT_KINDS: tuple[Kind, ...] = (
    Kind.HYPOTHESIS,
    Kind.NOTE,
    Kind.FAILED_APPROACH,
    Kind.HANDOFF,
    Kind.CONSTRAINT,
)
# A proposal's origin: the single-agent producer of the step (D2, D7).
PRODUCER_TASK = "analyze-execute"
PRODUCER_ROLE = "producer"
# How much of each output stream a validation failure's observation keeps.
MAX_OUTPUT_TAIL_CHARS = 600

_ADD_KEYS = frozenset({"op", "kind", "text"})
_CORRECT_KEYS = frozenset({"op", "record", "text", "reason"})
_RETRACT_KEYS = frozenset({"op", "record", "reason"})
_OPERATIONS = {
    Action.ADD.value: (Action.ADD, _ADD_KEYS),
    Action.CORRECT.value: (Action.CORRECT, _CORRECT_KEYS),
    Action.RETRACT.value: (Action.RETRACT, _RETRACT_KEYS),
}


def supported(phase: Phase) -> bool:
    """Whether ``phase`` gets a projection and has Memory collected from it (D13)."""
    return phase in SUPPORTED_PHASES


# -- before the launch: the projection (D12) ----------------------------------------------


@dataclass(frozen=True)
class LaunchProjection:
    """One launch's ``memory.md``, what it was rendered from and how long that took.

    ``shown`` maps each record the projection shows to the record revision
    it showed, read from the snapshot it was rendered from: the only records
    that launch's result may correct or retract, and the revisions it acts
    on (R1-F2 of PR #536).
    """

    request: ProjectionRequest
    projection: ContextProjection
    render_ms: float
    shown: Mapping[int, int]

    @property
    def text(self) -> str:
        return self.projection.text

    def metadata(self) -> dict:
        """What the run log records of the projection: counts, sizes, digests, never its text.

        ``render_ms`` is the controller's own measurement of the render, the
        wall time the evaluation reads against ADR 0015 D12's 1,000 ms
        ceiling. It is recorded, never checked: nothing decides on it.
        """
        freshness = self.projection.freshness
        text = self.projection.text
        return {
            "file": MEMORY_FILE,
            "launch": self.request.attempt,
            "candidate": self.request.candidate,
            "revision": freshness.revision,
            "memory_digest": freshness.memory_digest,
            "evidence_digest": freshness.evidence_digest,
            "shown": len(self.projection.ids),
            "omitted": freshness.omitted,
            "inapplicable": freshness.inapplicable,
            "withdrawn": freshness.withdrawn,
            "needs_revalidation": freshness.downgraded,
            "chars": len(text),
            "bytes": len(text.encode()),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "render_ms": round(self.render_ms, 3),
        }


def projection_request(
    store: MemoryStore,
    step: StepKey,
    *,
    candidate: str,
    ancestors: Sequence[str] = (),
) -> ProjectionRequest:
    """The binding of the next launch: its step, the ordinal its intent save spends, its candidate.

    ``candidate`` is the commit the launch starts from when it is not the
    entry's base ("" for none); ``ancestors`` the bases of the store's
    range-bound records that the candidate descends from, as the caller
    proved them (:meth:`~autoforge.memory.MemoryStore.range_bases`).
    """
    return ProjectionRequest(
        store.run_id,
        step,
        store.launch_seq + 1,
        candidate,
        tuple(ancestors) if candidate else (),
    )


def render_launch_projection(
    store: MemoryStore,
    request: ProjectionRequest,
    *,
    validation_records: Sequence[dict],
    clock: Callable[[], float] = time.perf_counter,
) -> LaunchProjection:
    """Render the projection #317 defines for ``request``, timed (D12).

    The run's retained validation records are the receipts a validation
    failure's citation resolves against; no candidate or launch receipt is
    retained in this release, so a record citing one is shown as needing
    revalidation. Raises :class:`~autoforge.errors.StateError` when the
    snapshot or the binding is refused, before anything of the launch is
    saved: a Memory launch never goes on without its projection (D18).
    """
    started = clock()
    projection = render_projection(store, request, validation_records=validation_records)
    elapsed = (clock() - started) * 1000.0
    shown = {
        record: revision
        for record in projection.ids
        if (revision := store.revision_of(record)) is not None
    }
    return LaunchProjection(request, projection, elapsed, shown)


# -- after the launch: the controller's observation (D8, D11 stage 4) --------------------


def validation_failure_text(summary: str, view: RecordView) -> str:
    """What a validation failure's observation says: the record, then its output's tails.

    ``summary`` is the controller's one-line account of the record. The
    store redacts and bounds the whole (an omission marker on a cut); this
    keeps each tail to its last :data:`MAX_OUTPUT_TAIL_CHARS` characters
    first, so the account itself is never what is cut.
    """
    parts = [summary.rstrip(".") + "."]
    output = view.raw.get("output") if isinstance(view.raw, dict) else None
    if isinstance(output, dict):
        for stream, name in (("stderr", "Standard error"), ("stdout", "Standard output")):
            tail = output.get(f"{stream}_tail")
            if not isinstance(tail, str) or not tail.strip():
                continue
            kept = tail[-MAX_OUTPUT_TAIL_CHARS:]
            cut = len(kept) < len(tail) or bool(output.get(f"{stream}_tail_truncated"))
            parts.append(
                f"{name}{', its end' if cut else ''}:\n{kept.rstrip()}",
            )
    return "\n\n".join(parts)


def observe_validation_failure(
    store: MemoryStore, step: StepKey, view: RecordView, *, attempt: int, text: str
) -> WriteOutcome | None:
    """The controller's ``validation-failure`` record of ``view``, or ``None`` to write nothing.

    Keyed by its citation (D9.7): a record already cited, as when a crash
    or a relaunch reports the same failing candidate again, adds nothing.
    Bound to the failing commit and to the base its candidate was checked
    against: on that commit the projection shows the failure as established
    by its retained record, and on a later commit descending from the same
    base it is shown with its source candidate, as needing revalidation. A
    record that passed, that was interrupted or that is not a worktree
    candidate's is not observed here. ``attempt`` is the launch ordinal of
    the launch whose candidate it is.
    """
    if not store.enabled or attempt < 1 or view.outcome not in (Outcome.FAIL, Outcome.TIMEOUT):
        return None
    provenance = view.candidate.provenance
    if not isinstance(provenance, WorktreeProvenance):
        return None
    ref = EvidenceRef(Receipt.VALIDATION, view.seq, view.digest)
    if store.cites(ref):
        return None
    base = provenance.base_revision
    scope = MemoryScope(
        step,
        (Phase.ANALYZE_EXECUTE,),
        Range.DESCENDANTS if base else Range.EXACT,
        candidate=view.candidate.commit,
        base=base,
    )
    return store.add(
        actor=Actor.CONTROLLER,
        kind=Kind.VALIDATION_FAILURE,
        scope=scope,
        text=text,
        attempt=attempt,
        evidence=ref,
    )


# -- after the launch: an accepted result's proposals (D9.4, D9.8) -----------------------


@dataclass(frozen=True)
class Proposal:
    """One entry of a result's ``memory`` list, as the controller parsed it.

    ``slot`` is its position in the agent's list, which keys an ``add``
    (D9.7). ``revision`` is, for a ``correct`` or ``retract``, the record
    revision the launch was shown, and 0 until the entry is bound to its
    launch (:func:`bind_proposals`).
    """

    action: Action
    kind: Kind | None = None
    record: int = 0
    text: str | None = None
    reason: str = ""
    slot: int = 0
    revision: int = 0


@dataclass(frozen=True)
class Proposals:
    """A result's proposals, and how many entries were refused before the store saw them."""

    entries: tuple[Proposal, ...] = ()
    refused: int = 0


_AGENT_KINDS = {k.value: k for k in AGENT_KINDS}


def _entry(raw: object, slot: int) -> Proposal | None:
    """One entry of the agent's list, with exactly its operation's keys, or ``None``."""
    if not isinstance(raw, dict):
        return None
    op = raw.get("op")
    if not isinstance(op, str) or op not in _OPERATIONS:
        return None
    action, keys = _OPERATIONS[op]
    if raw.keys() != keys:
        return None
    if action is Action.ADD:
        kind = raw["kind"]
        text = raw["text"]
        if not isinstance(text, str) or not isinstance(kind, str):
            return None
        if kind not in _AGENT_KINDS:
            return None
        return Proposal(Action.ADD, kind=_AGENT_KINDS[kind], text=text, slot=slot)
    record, reason = raw["record"], raw["reason"]
    if type(record) is not int or record < 1 or not isinstance(reason, str):
        return None
    text = raw.get("text") if action is Action.CORRECT else None
    if action is Action.CORRECT and not isinstance(text, str):
        return None
    return Proposal(action, record=record, text=text, reason=reason, slot=slot)


def parse_proposals(raw: object) -> Proposals:
    """A result's optional ``memory`` field, as the agent wrote it; never raises (D9.8).

    Absent (``None``) is no proposal. Anything that is not a list is one
    refusal, whatever its shape. Each entry is ``add`` (``kind``,
    ``text``), ``correct`` (``record``, ``text``, ``reason``) or
    ``retract`` (``record``, ``reason``) with exactly those keys; any other
    entry, and every entry past :data:`MAX_PROPOSALS`, is refused and
    counted. Text bounds and redaction are the store's (D9.6, D9.9): an
    agent's text the run-log redactor would change is refused, never kept
    redacted.
    """
    if raw is None:
        return Proposals()
    if not isinstance(raw, list):
        return Proposals(refused=1)
    entries: list[Proposal] = []
    refused = 0
    for index, item in enumerate(raw):
        entry = _entry(item, index) if index < MAX_PROPOSALS else None
        if entry is None:
            refused += 1
        else:
            entries.append(entry)
    return Proposals(tuple(entries), refused)


def _unchanged_by_redaction(text: str | None) -> bool:
    """Whether the run-log redactor leaves ``text`` as it is (D9.9); ``None`` is no text."""
    if text is None:
        return True
    try:
        text.encode()
    except UnicodeError:
        return False
    return redact(text) == text


def bind_proposals(raw: object, shown: Mapping[int, int]) -> Proposals:
    """A result's ``memory`` field bound to the launch that returned it (R1-F2 of PR #536).

    Called once, by the process that launched, on the parse of the launch's
    fresh reply, with what that launch's projection showed
    (:attr:`LaunchProjection.shown`). What it returns is the controller's
    and is held in that process only, never written into the result or the
    state: a ``memory`` field read back from anywhere else (a reply kept
    across a restart, #479, whichever controller kept it and whether or not
    its launch was shown Memory) is the agent's data, whatever its shape, and
    is never bound again (R2-F1). Each entry :func:`parse_proposals` refuses
    stays refused, and so is, counted with them and never kept:

    - a ``correct`` or ``retract`` of a record the projection did not show
      (one left out for the budget, one bound to another candidate, one
      the same result adds): the agent never saw it, whatever its number;
    - an entry whose ``text`` the run-log redactor would change (D9.9): a
      secret is refused, never kept redacted.

    A ``correct`` or ``retract`` the projection showed carries the record
    revision it showed, which the store holds the change to: one made after
    the record changed again (by an earlier entry of the same result, say)
    is stale and refused there. Its ``reason`` is redacted, as the store
    keeps it.
    """
    parsed = parse_proposals(raw)
    entries: list[Proposal] = []
    refused = parsed.refused
    for proposal in parsed.entries:
        if proposal.action is not Action.ADD:
            revision = shown.get(proposal.record)
            if revision is None:
                refused += 1
                continue
            proposal = replace(proposal, revision=revision, reason=redact(proposal.reason))
        if not _unchanged_by_redaction(proposal.text):
            refused += 1
            continue
        entries.append(proposal)
    return Proposals(tuple(entries), refused)


@dataclass(frozen=True)
class Admission:
    """What admitting one accepted result's proposals did to the store."""

    store: MemoryStore
    applied: int = 0
    replayed: int = 0
    refused: int = 0

    @property
    def offered(self) -> bool:
        """Whether there was anything to save: an entry applied or refused (and counted)."""
        return bool(self.applied or self.refused)


def admit_proposals(
    store: MemoryStore,
    proposals: Proposals,
    *,
    step: StepKey,
    attempt: int,
    input_sha: str,
) -> Admission:
    """Admit an accepted result's proposals, in order, into ``store`` (D9.4, D9.8).

    Called only for a result the controller accepts, in the save that
    accepts it, with the proposals the process that parsed its reply bound
    (:func:`bind_proposals`). Each ``add`` is a proposal of the step's
    producer, keyed by its launch ordinal (``attempt``), the input it was
    launched on (``input_sha``) and its position in the agent's list, so the
    same result admitted again is a replay, never a second record. A
    ``correct`` or ``retract`` names a record the launch was shown and the
    revision it was shown (:func:`bind_proposals`); the store refuses one of
    a record that is not a proposal, that changed since that revision or
    after this launch started, or of another issue. With Memory off nothing
    is admitted or counted.
    """
    if not store.enabled:
        return Admission(store)
    scope = MemoryScope(step)
    applied = replayed = refused = 0
    for proposal in proposals.entries:
        if proposal.action is Action.ADD:
            assert proposal.kind is not None and proposal.text is not None
            out = store.add(
                actor=Actor.AGENT,
                kind=proposal.kind,
                scope=scope,
                text=proposal.text,
                attempt=attempt,
                key=ProposalKey(PRODUCER_TASK, attempt, PRODUCER_ROLE, input_sha, proposal.slot),
                accepted=True,
            )
        else:
            out = store.revise(
                proposal.action,
                proposal.record,
                actor=Actor.AGENT,
                reason=proposal.reason,
                step=step,
                attempt=attempt,
                text=proposal.text,
                expected=proposal.revision,
                accepted=True,
            )
        store = out.store
        if out.status == "applied":
            applied += 1
        elif out.status == "replay":
            replayed += 1
        else:
            refused += 1
    store = store.refused(proposals.refused)
    return Admission(store, applied, replayed, refused + proposals.refused)


__all__ = [
    "AGENT_KINDS",
    "MAX_PROPOSALS",
    "MAX_REASON_BYTES",
    "MAX_TEXT_BYTES",
    "MEMORY_FILE",
    "MEMORY_FILE_DESCRIPTION",
    "SUPPORTED_PHASES",
    "UNSUPPORTED_PHASES",
    "Admission",
    "LaunchProjection",
    "Proposal",
    "Proposals",
    "admit_proposals",
    "bind_proposals",
    "observe_validation_failure",
    "parse_proposals",
    "projection_request",
    "render_launch_projection",
    "supported",
    "validation_failure_text",
]
