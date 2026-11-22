"""Issue Working Memory's single-agent handoff (#333; ADR 0015 D9, D11 to D13, D20).

What the controller does with the store around a launch of the one phase this
release supports, ``ANALYZE_EXECUTE`` (D13), and nothing more:

- before the launch, the projection of the committed snapshot, bound to the
  launch ordinal the intent save spends next and to the worktree's candidate,
  which the engine writes into the launch's read context as ``memory.md``
  (D12): rendered once, never changed while the launch runs, never read back.
  Which range-bound records apply to the candidate is proved from the local
  store's own commits, never asked of GitHub (R3-F1 of PR #536), and the
  whole preparation is timed, not the render alone;
- after it, the controller's own observation of a validation failure, added
  in the save of the validation record it cites (D11 stage 4), and the
  proposals of an accepted result, added in the save that accepts it: the
  phase's plan save, or a ``blocked`` or ``failure`` result's stop save
  (D9.8, D4.6). A refused, corrected or lost reply leaves no proposal. The
  result's list is bound to its launch as soon as the reply is parsed
  (:func:`bind_proposals`): a revision only of a record the launch was
  shown, at the revision it was shown, no text past the store's bounds or
  that the redactor would change, and every reason redacted and bounded
  (R3-F4), beside the commit the launch started from, which keys its adds
  (R3-F2). The binding is the controller's and lives in the process that
  parsed the reply, never in the result: the ``memory`` field stays the
  agent's data, so a reply kept across a restart (#479) admits none of it
  (R2-F1 of PR #536), and what is kept of a reply is what Memory off keeps
  (R2-F2).

Nothing here reads a file, the network or a model, and nothing here decides a
phase, a write, a budget or a merge: the engine supplies every value, and
the readers of the candidate and of a commit's parents, and saves what comes
back. Memory stays advisory (D8): a store that refuses a
write leaves the step as it would have been without Memory, the refusal only
counted.
"""

from __future__ import annotations

import hashlib
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from .auto_recovery import StepKey
from .errors import AutoForgeError
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
    admissible_text,
    bounded_reason,
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
# The most commits a projection's ancestry walk reads (:func:`local_ancestors`,
# R3-F1 of PR #536). A candidate is the base plus the commits the step's
# launches made on it, a handful; each read is one local git process, so the
# bound is also what bounds the walk's share of a launch's latency. A base
# further below is not proved, and a record bound to it is omitted.
MAX_ANCESTRY_COMMITS = 64
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
    on (R1-F2 of PR #536). ``input_sha`` is the commit the launch starts
    from, as the controller read it (:func:`prepare_launch_projection`):
    what its proposals are keyed by (R3-F2). ``render_ms`` times the render
    alone; ``projection_ms`` everything Memory adds before the launch, the
    candidate's read and the ancestry walk included (R3-F1), and
    ``ancestry_reads`` counts the commits that walk read.
    """

    request: ProjectionRequest
    projection: ContextProjection
    render_ms: float
    shown: Mapping[int, int]
    input_sha: str = ""
    projection_ms: float = 0.0
    ancestry_reads: int = 0

    @property
    def text(self) -> str:
        return self.projection.text

    def bind(self, raw: object, launch: int) -> LaunchBinding:
        """The reply's ``memory`` field bound to this launch, ordinal ``launch`` (R3-F2 of PR #536).

        Its proposals as :func:`bind_proposals` binds them to what this
        projection showed, beside the launch input they are keyed by.
        """
        return LaunchBinding(launch, self.input_sha, bind_proposals(raw, self.shown))

    def metadata(self) -> dict:
        """What the run log records of the projection: counts, sizes, digests, never its text.

        ``projection_ms`` is the controller's own measurement of Memory's
        whole work before the launch (the candidate's read, the ancestry
        walk and the render), the wall time the evaluation reads against
        ADR 0015 D12's 1,000 ms ceiling per launch; ``render_ms`` is the
        render's share of it. Both are recorded, never checked: nothing
        decides on them.
        """
        freshness = self.projection.freshness
        text = self.projection.text
        return {
            "file": MEMORY_FILE,
            "launch": self.request.attempt,
            "input": self.input_sha,
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
            "ancestry_reads": self.ancestry_reads,
            "projection_ms": round(self.projection_ms, 3),
            "render_ms": round(self.render_ms, 3),
        }


@dataclass(frozen=True)
class LaunchBinding:
    """What the process that parsed a launch's reply holds of it for Memory (R2-F1 of PR #536).

    ``launch`` is the launch's ordinal, ``input_sha`` the commit it started
    from (:attr:`LaunchProjection.input_sha`, R3-F2) and ``proposals`` its
    result's list as :func:`bind_proposals` bound it: every text already
    within the store's bounds and every reason redacted and bounded
    (R3-F4), so nothing the store would not keep is held. Never written
    into the result or the state.
    """

    launch: int
    input_sha: str
    proposals: Proposals


def local_ancestors(
    candidate: str,
    bases: Sequence[str],
    read_parents: Callable[[str], Sequence[str] | None],
    *,
    max_commits: int = MAX_ANCESTRY_COMMITS,
) -> tuple[tuple[str, ...], int]:
    """The ``bases`` that ``candidate`` descends from, proved in the local store alone.

    Returns them, in ``bases``' order, with how many commits were read.
    Breadth-first from the candidate through each commit's parents as
    ``read_parents`` reads them from the commit's own bytes in the shared
    store (``None`` when the store does not hold it): the engine's reader
    re-hashes each commit, never fetches one and never asks GitHub
    (R3-F1 of PR #536), so Memory adds no network call to a launch and a
    retry on the commit that failed sees its failure whatever GitHub
    answers. A base is proved when it is a parent of a commit read, so one
    at most ``max_commits`` commits below the candidate can be; the walk
    ends when every base is reached, after ``max_commits`` reads, or when
    nothing is left to read, and a commit the store does not hold ends its
    path. A read that fails ends the walk. A base not reached is not
    proved, so a record bound to it is omitted as not applicable, never
    shown as current; the bases reached before the walk ended stay proved,
    each by a chain of parents read from bytes that hash to their ids.
    """
    wanted = set(bases) - {candidate}
    if not candidate or not wanted:
        return (), 0
    reached: set[str] = set()
    seen = {candidate}
    pending = deque([candidate])
    reads = 0
    while pending and reached != wanted and reads < max_commits:
        sha = pending.popleft()
        reads += 1
        try:
            parents = read_parents(sha)
        except (AutoForgeError, OSError):
            break
        for parent in parents or ():
            # A parent read from authenticated bytes proves its ancestry; a
            # base reached is walked on while another may lie below it.
            if parent in wanted:
                reached.add(parent)
            if parent not in seen:
                seen.add(parent)
                pending.append(parent)
    return tuple(b for b in bases if b in reached), reads


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
    proved them (:meth:`~autoforge.memory.MemoryStore.range_bases`,
    :func:`local_ancestors`).
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
    The launch input and the whole preparation's time are
    :func:`prepare_launch_projection`'s; rendered alone, the render is all
    of it.
    """
    started = clock()
    projection = render_projection(store, request, validation_records=validation_records)
    elapsed = (clock() - started) * 1000.0
    shown = {
        record: revision
        for record in projection.ids
        if (revision := store.revision_of(record)) is not None
    }
    return LaunchProjection(
        request, projection, elapsed, shown, input_sha=request.candidate, projection_ms=elapsed
    )


def prepare_launch_projection(
    store: MemoryStore,
    step: StepKey,
    *,
    base: str,
    read_head: Callable[[], str],
    read_parents: Callable[[str], Sequence[str] | None],
    validation_records: Sequence[dict],
    clock: Callable[[], float] = time.perf_counter,
    max_commits: int = MAX_ANCESTRY_COMMITS,
) -> LaunchProjection:
    """The next launch's projection, from its candidate's read to its render, timed whole (D12).

    ``read_head`` reads the commit the launch starts from ("" when it
    cannot be read); it is the projection's candidate when it is not the
    entry's ``base``, and none otherwise. The bases of the store's
    range-bound records it descends from are proved by
    :func:`local_ancestors` through ``read_parents``, never through GitHub
    (R3-F1 of PR #536). ``input_sha`` is that commit, or ``base`` when none
    could be read, as the projection then binds none (R3-F2): the input the
    launch's proposals are keyed by. ``projection_ms`` covers all of it, so
    the per-launch latency the run log records is Memory's whole cost
    before the launch, never the render's alone. A store that cannot be
    rendered raises as :func:`render_launch_projection` does.
    """
    started = clock()
    head = read_head()
    candidate = head if head != base else ""
    ancestors, reads = (
        local_ancestors(
            candidate,
            store.range_bases(step.issue_url),
            read_parents,
            max_commits=max_commits,
        )
        if candidate
        else ((), 0)
    )
    request = projection_request(store, step, candidate=candidate, ancestors=ancestors)
    launch = render_launch_projection(
        store, request, validation_records=validation_records, clock=clock
    )
    return replace(
        launch,
        input_sha=head or base,
        projection_ms=(clock() - started) * 1000.0,
        ancestry_reads=reads,
    )


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
    counted. The store's text bounds and redaction (D9.6, D9.9) are applied
    when the list is bound (:func:`bind_proposals`): an agent's text the
    run-log redactor would change is refused, never kept redacted.
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

    - an entry whose ``text`` the store would not keep as it is
      (:func:`~autoforge.memory.admissible_text`): past
      :data:`MAX_TEXT_BYTES`, blank, holding a NUL, or one the run-log
      redactor would change (D9.6, D9.9), so a secret is refused, never kept
      redacted, and an overlong note is never held (R3-F4);
    - a ``correct`` or ``retract`` of a record the projection did not show
      (one left out for the budget, one bound to another candidate, one
      the same result adds): the agent never saw it, whatever its number;
    - a ``correct`` or ``retract`` whose ``reason`` is blank, which the
      store refuses.

    A ``correct`` or ``retract`` the projection showed carries the record
    revision it showed, which the store holds the change to: one made after
    the record changed again (by an earlier entry of the same result, say)
    is stale and refused there. Its ``reason`` is held redacted and bounded
    to :data:`MAX_REASON_BYTES`, as the store keeps it
    (:func:`~autoforge.memory.bounded_reason`, R3-F4). Nothing of this
    touches the reply itself, so whether a reply is kept is decided as with
    Memory off.
    """
    parsed = parse_proposals(raw)
    entries: list[Proposal] = []
    refused = parsed.refused
    for proposal in parsed.entries:
        if proposal.text is not None and not admissible_text(proposal.text):
            refused += 1
            continue
        if proposal.action is not Action.ADD:
            revision = shown.get(proposal.record)
            if revision is None or not proposal.reason.strip():
                refused += 1
                continue
            proposal = replace(proposal, revision=revision, reason=bounded_reason(proposal.reason))
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
    producer, keyed by its launch ordinal (``attempt``), the commit that
    launch started from (``input_sha``, :attr:`LaunchBinding.input_sha`:
    R3-F2 of PR #536) and its position in the agent's list, so the same
    result admitted again is a replay, never a second record. The key is
    provenance only: what the record applies to is its scope, the whole
    issue's launches of the phase, whichever commit they start from. A
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
    "MAX_ANCESTRY_COMMITS",
    "MAX_PROPOSALS",
    "MAX_REASON_BYTES",
    "MAX_TEXT_BYTES",
    "MEMORY_FILE",
    "MEMORY_FILE_DESCRIPTION",
    "SUPPORTED_PHASES",
    "UNSUPPORTED_PHASES",
    "Admission",
    "LaunchBinding",
    "LaunchProjection",
    "Proposal",
    "Proposals",
    "admit_proposals",
    "bind_proposals",
    "local_ancestors",
    "observe_validation_failure",
    "parse_proposals",
    "prepare_launch_projection",
    "projection_request",
    "render_launch_projection",
    "supported",
    "validation_failure_text",
]
