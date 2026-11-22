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
  (D9.8, D4.6). A refused, corrected or lost reply leaves no proposal.

Nothing here reads a file, the network or a model, and nothing here decides a
phase, a write, a budget or a merge: the engine supplies every value and
saves what comes back. Memory stays advisory (D8): a store that refuses a
write leaves the step as it would have been without Memory, the refusal only
counted.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

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
    """One launch's ``memory.md``, what it was rendered from and how long that took."""

    request: ProjectionRequest
    projection: ContextProjection
    render_ms: float

    @property
    def text(self) -> str:
        return self.projection.text

    def metadata(self) -> dict:
        """What the run log records of the projection: counts, sizes, digests, never its text.

        ``render_ms`` is the controller's own measurement of the render, the
        number ADR 0015 D12 holds to the evaluation's 1,000 ms ceiling.
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
    return LaunchProjection(request, projection, (clock() - started) * 1000.0)


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
    """One entry of a result's ``memory`` list, as the controller parsed it."""

    action: Action
    kind: Kind | None = None
    record: int = 0
    text: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class Proposals:
    """A result's proposals, and how many entries were refused before the store saw them."""

    entries: tuple[Proposal, ...] = ()
    refused: int = 0


def _entry(raw: object) -> Proposal | None:
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
        known = {k.value: k for k in AGENT_KINDS}
        if kind not in known:
            return None
        return Proposal(Action.ADD, kind=known[kind], text=text)
    record, reason = raw["record"], raw["reason"]
    if type(record) is not int or record < 1 or not isinstance(reason, str):
        return None
    text = raw.get("text") if action is Action.CORRECT else None
    if action is Action.CORRECT and not isinstance(text, str):
        return None
    return Proposal(action, record=record, text=text, reason=reason)


def parse_proposals(raw: object) -> Proposals:
    """A result's optional ``memory`` field; never raises (D9.8).

    Absent (``None``) is no proposal. Anything that is not a list is one
    refusal. Each entry is ``add`` (``kind``, ``text``), ``correct``
    (``record``, ``text``, ``reason``) or ``retract`` (``record``,
    ``reason``) with exactly those keys; any other entry, and every entry
    past :data:`MAX_PROPOSALS`, is refused and counted. Text bounds and
    redaction are the store's (D9.6, D9.9): an agent's text the run-log
    redactor would change is refused there, never kept redacted.
    """
    if raw is None:
        return Proposals()
    if not isinstance(raw, list):
        return Proposals(refused=1)
    entries: list[Proposal] = []
    refused = 0
    for index, item in enumerate(raw):
        entry = _entry(item) if index < MAX_PROPOSALS else None
        if entry is None:
            refused += 1
        else:
            entries.append(entry)
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
    accepts it. Each ``add`` is a proposal of the step's producer, keyed by
    its launch ordinal (``attempt``), the input it was launched on
    (``input_sha``) and its position in the list, so the same result
    admitted again is a replay, never a second record. A ``correct`` or
    ``retract`` names a record of the projection; the store refuses one of a
    record that is not a proposal, that changed after this launch started,
    or of another issue. With Memory off nothing is admitted or counted.
    """
    if not store.enabled:
        return Admission(store)
    scope = MemoryScope(step)
    applied = replayed = refused = 0
    for slot, proposal in enumerate(proposals.entries):
        if proposal.action is Action.ADD:
            assert proposal.kind is not None and proposal.text is not None
            out = store.add(
                actor=Actor.AGENT,
                kind=proposal.kind,
                scope=scope,
                text=proposal.text,
                attempt=attempt,
                key=ProposalKey(PRODUCER_TASK, attempt, PRODUCER_ROLE, input_sha, slot),
                accepted=True,
            )
        else:
            expected = store.revision_of(proposal.record)
            out = store.revise(
                proposal.action,
                proposal.record,
                actor=Actor.AGENT,
                reason=proposal.reason,
                step=step,
                attempt=attempt,
                text=proposal.text,
                expected=expected if expected is not None else 0,
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
    "observe_validation_failure",
    "parse_proposals",
    "projection_request",
    "render_launch_projection",
    "supported",
    "validation_failure_text",
]
