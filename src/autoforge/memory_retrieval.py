"""Optional controller-side retrieval over one launch's Issue Working Memory.

The store authorizes prose. An index supplies only lexical membership, never
text, scope, revision or rank. No launcher or agent transport calls this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from .auto_recovery import StepKey
from .errors import StateError
from .memory import (
    MAX_EVENTS,
    MAX_FACTS,
    MAX_TEXT_BYTES,
    Actor,
    EvidenceRef,
    Kind,
    MemoryScope,
    MemoryStore,
    ProjectionRequest,
    Retention,
    RetrievalRecord,
)
from .redaction import redact

MAX_QUERY_BYTES = 512
MAX_HITS = 16
TAIL_RECORDS = 8
SUMMARY_BYTES = 512
OMISSION = "\n[omitted]"
PLACEHOLDER = "[details omitted]"


class BackendError(RuntimeError):
    """An index operation failed; message contents are not reported."""


class IndexUnavailable(BackendError):
    """Temporary unavailability, or this interpreter does not provide FTS5."""


class IntegrityError(BackendError):
    """The projection does not agree with the generation that was built."""


class IndexState(StrEnum):
    EMPTY = "empty"
    CURRENT = "current"
    STALE = "stale"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class Backend(StrEnum):
    FTS5 = "fts5"
    OPTIONAL = "optional"


class IndexProblem(StrEnum):
    """A diagnosis, reported on one of two independent channels (R5-F1 of PR #538).

    ``problem`` is the published generation's condition, from its last lookup
    or ``verify()``: ``unavailable`` (transient, retried by the next search),
    or ``integrity`` or ``search-failed`` (known damaged, unreadable until
    replaced). ``refresh`` is the last refresh's failure: ``build-failed``,
    or ``unavailable`` when the backend could not build and nothing is
    published. Neither channel ever writes the other.
    """

    INTEGRITY = "integrity"
    SEARCH = "search-failed"
    BUILD = "build-failed"
    UNAVAILABLE = "unavailable"


def _failures(problem: IndexProblem | None, refresh: IndexProblem | None) -> int:
    # Unavailability is not a failure; a damaged generation and a failed
    # refresh are independent, and each counts.
    return sum(p not in (None, IndexProblem.UNAVAILABLE) for p in (problem, refresh))


@dataclass(frozen=True)
class IndexStatus:
    """The controller's maintenance diagnostic, in the run ledger's audit positions.

    ``source_seq`` is the ledger's audit position (its row count) and
    ``indexed_through_seq`` the position through which the published
    generation holds this binding's corpus: ``None`` when none is published
    or the published one is known damaged, which verifies no coverage.
    ``pending`` is the number of audit rows after it (all of them when it is
    ``None``), whichever records they touch. Only ``state`` says whether the
    generation is current. ``problem`` is the published generation's
    condition and ``refresh`` the last refresh's failure, independently
    (:class:`IndexProblem`); ``failed`` counts those that are failures, not
    unavailability. Only ``status``, ``sync``, ``rebuild`` and ``verify``
    return one; launch answers carry :class:`ScopeFreshness`, which has no
    audit position.
    """

    backend: Backend
    state: IndexState
    source_seq: int
    indexed_through_seq: int | None
    pending: int
    generation: int | None
    problem: IndexProblem | None
    refresh: IndexProblem | None

    @property
    def is_current(self) -> bool:
        return self.state is IndexState.CURRENT

    @property
    def lag(self) -> int | None:
        if self.indexed_through_seq is None:
            return None
        return self.source_seq - self.indexed_through_seq

    @property
    def failed(self) -> int:
        return _failures(self.problem, self.refresh)


@dataclass(frozen=True)
class ScopeFreshness:
    """A launch answer's index freshness over the binding's authorized corpus.

    Search, fetch and catalog carry the same one. It holds no audit position
    and no record ID. ``fingerprint`` is a digest of the authorized records'
    kinds and current text, as a multiset: it changes whenever the kinds or
    text the binding can read change (a record added, corrected, withdrawn,
    or superseded by one of another kind or text), and it is the same for
    every checkpoint that presents the same kinds and text, whatever withheld
    history (withdrawn, superseded, inapplicable or other issues' records)
    led to it (R3-F1 of PR #538). It does not identify records: a same-text
    supersession leaves it unchanged. The answers that show IDs, search and
    fetch, also carry the identity revision for that (:class:`SearchResult`).
    Citation retention is resolved afresh for each answer and is not part of
    it. ``pending`` counts the work to make the index current: the
    authorized records the answering generation does not verifiably hold at
    their current version, plus the records it holds that are no longer
    authorized. A generation known damaged verifies nothing, so every
    authorized record is pending. A write to a record the binding cannot
    read changes neither field. Only ``state`` says whether the index is
    current: a current one has nothing pending, but nothing pending does not
    make one current (an index never built, or damaged, over an empty corpus
    has nothing pending). ``generation`` counts this reader's publications
    and is not comparable across readers. ``problem`` and ``refresh`` are
    the answering generation's condition and its last refresh's failure, as
    in :class:`IndexStatus`.
    """

    backend: Backend
    state: IndexState
    fingerprint: str
    pending: int
    generation: int | None
    problem: IndexProblem | None
    refresh: IndexProblem | None

    @property
    def is_current(self) -> bool:
        return self.state is IndexState.CURRENT

    @property
    def failed(self) -> int:
        return _failures(self.problem, self.refresh)


@dataclass(frozen=True)
class ReadScope:
    repository: str
    run_id: str
    step: StepKey
    launch: int
    candidate: str
    environment: str


@dataclass(frozen=True)
class MemoryItem:
    id: int
    kind: Kind
    author: Actor
    applicability: MemoryScope
    evidence: EvidenceRef | None
    retention: Retention | None
    text: str


@dataclass(frozen=True)
class MemoryHit:
    item: MemoryItem
    score: float
    scorer: str = "okapi-bm25/authorized-corpus"


@dataclass(frozen=True)
class SearchResult:
    """Ranked hits, with the identity revision of the binding's authorized records.

    ``revision`` is a digest of each authorized record's ID, kind and current
    text: what search and fetch can show of the corpus (fetch answers any
    authorized ID). It changes whenever an authorized record is added,
    corrected, withdrawn or superseded, a same-text supersession included,
    since the replacement has an ID of its own, so a consumer that caches
    IDs can tell when one is obsolete. A write the binding cannot read
    never changes it (IDs are assigned once), and any reader of the same
    checkpoint and binding reports the same one. Because IDs are audit
    positions, two histories presenting the same kinds and text can number
    their records differently; the revision then differs, as the IDs do,
    and only :class:`ScopeFreshness`'s ``fingerprint`` is the same. The
    catalog shows no ID and carries no revision.
    """

    scope: ReadScope
    hits: tuple[MemoryHit, ...]
    index: ScopeFreshness
    revision: str
    complete: bool
    degraded: bool
    tail_used: bool


@dataclass(frozen=True)
class FetchResult:
    """The current item or ``None``, with the same identity revision as search."""

    scope: ReadScope
    item: MemoryItem | None
    index: ScopeFreshness
    revision: str
    degraded: bool


@dataclass(frozen=True)
class MemoryCatalog:
    scope: ReadScope
    topics: tuple[tuple[Kind, int], ...]
    index: ScopeFreshness
    degraded: bool

    def render(self) -> str:
        """Static labels only; no record titles, IDs, text, receipts or internal proofs.

        Its length has a fixed bound, whatever the records: one digest (the
        content fingerprint), and one count per kind and a pending count,
        each at most ``MAX_FACTS``.
        """
        topics = ", ".join(f"{kind.value}: {count}" for kind, count in self.topics) or "(none)"
        return (
            "Issue Working Memory catalog (advisory, untrusted)\n"
            f"Topics: {topics}\n"
            f"Fingerprint: {self.index.fingerprint}\n"
            f"Index: {self.index.state.value}; pending: {self.index.pending}; "
            f"degraded: {str(self.degraded).lower()}\n"
        )


@dataclass(frozen=True)
class ReadMetrics:
    """Bounded controller counters; deliberately no arguments, identifiers or prose."""

    searches: int = 0
    fetches: int = 0
    hits: int = 0
    degraded_reads: int = 0


@dataclass(frozen=True)
class Checkpoint:
    """One committed store and its current citation inputs from the same authority read."""

    store: MemoryStore
    validation_records: tuple[dict, ...] = ()
    retained: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class IndexDocument:
    id: int
    terms: tuple[str, ...]


@dataclass(frozen=True)
class DocumentSeal:
    id: int
    size: int
    digest: bytes


class CandidateIndex(Protocol):
    """One immutable generation; implementations must bound work to the supplied corpus.

    Return an exact tuple of IDs (at most MAX_FACTS), for OR of exact terms.
    The controller checks the answer, and never consumes backend properties or
    rankings. Raise BackendError for operational failures, and IndexUnavailable
    for transient query failure. Adapters are trusted controller code, not plugins.
    """

    def search(self, terms: tuple[str, ...]) -> tuple[int, ...]: ...

    def verify(self, manifest: tuple[DocumentSeal, ...]) -> None: ...


IndexFactory = Callable[[tuple[IndexDocument, ...]], CandidateIndex]


def _words(text: str) -> tuple[str, ...]:
    # One tokenizer, used for FTS keys, in-store ranking and the coverage proof.
    normalized = unicodedata.normalize("NFC", unicodedata.normalize("NFKC", text).casefold())
    words: list[str] = []
    current: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)[0]
        if category in "LN" or (category == "M" and current):
            current.append(char)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return tuple(words)


def _key(word: str) -> str:
    return "t" + word.encode().hex()


def _query(text: str, k: int) -> tuple[str, ...]:
    if type(k) is not int or not 1 <= k <= MAX_HITS:
        raise ValueError("invalid memory result limit")
    if type(text) is not str or len(text) > MAX_QUERY_BYTES:
        raise ValueError("invalid memory query")
    try:
        if len(text.encode()) <= MAX_QUERY_BYTES:
            return tuple(dict.fromkeys(_words(text)))
    except UnicodeError:
        pass
    raise ValueError("invalid memory query")


def _excerpt(text: str, chars: int, size: int) -> str:
    """``text`` redacted, in at most ``chars`` characters and ``size`` UTF-8 bytes.

    The omission marker and the placeholder are ASCII, so each counts the same
    in both bounds.
    """
    text = redact(text)
    if len(text) > chars or len(text.encode()) > size:
        if min(chars, size) < len(OMISSION):
            return ""
        cut = text[: chars - len(OMISSION)].encode()[: size - len(OMISSION)]
        text = cut.decode(errors="ignore") + OMISSION
    if redact(text) == text:
        return text
    return PLACEHOLDER if min(chars, size) >= len(PLACEHOLDER) else ""


def _item(record: RetrievalRecord, chars: int, size: int) -> MemoryItem:
    fact = record.fact
    return MemoryItem(
        fact.id,
        fact.kind,
        fact.author,
        fact.scope,
        fact.evidence,
        record.retention,
        _excerpt(record.text, chars, size),
    )


def _fts5(documents: tuple[IndexDocument, ...]) -> CandidateIndex:
    from .memory_fts5 import Fts5Index

    return Fts5Index(documents)


@dataclass(frozen=True)
class _View:
    store: MemoryStore
    records: tuple[RetrievalRecord, ...]
    scope: ReadScope | None

    @property
    def versions(self) -> frozenset[tuple[int, int]]:
        # A readable record changes revision only by a correction, which
        # changes its text: so these also identify what an index must hold.
        return frozenset((r.fact.id, r.fact.revision) for r in self.records)

    @property
    def fingerprint(self) -> str:
        # Kinds and text digests of the authorized records alone: no ID and
        # no count of other records, both of which withheld history moves.
        return _digest_rows(
            "fingerprint", [[r.fact.kind.value, r.fact.content] for r in self.records]
        )

    @property
    def revision(self) -> str:
        # IDs too, which search and fetch show: a same-text replacement has
        # a new one. Never on the catalog, which shows no ID.
        return _digest_rows(
            "revision", [[r.fact.id, r.fact.kind.value, r.fact.content] for r in self.records]
        )


def _digest_rows(label: str, rows: list[list]) -> str:
    # The label keeps the two digests apart, even over an empty corpus.
    return hashlib.sha256(json.dumps([label, sorted(rows)]).encode()).hexdigest()


@dataclass(frozen=True)
class _Generation:
    index: CandidateIndex
    versions: frozenset[tuple[int, int]]
    manifest: tuple[DocumentSeal, ...]
    audit: int
    serial: int


@dataclass(frozen=True)
class _Slot:
    """One backend's published generation and its two diagnostics (R5-F1 of PR #538).

    ``problem`` belongs to the published generation: a lookup or
    ``verify()`` sets it, and a lookup that succeeds clears only its
    unavailability. ``refresh`` belongs to the refresh: a failed one sets it
    and keeps the generation, ``problem`` included, so it neither makes an
    unavailable generation answer nor repairs a damaged one; and ``sync()``
    retries while it is set, whatever lookups do meanwhile. Only a published
    replacement clears both.
    """

    backend: Backend
    factory: IndexFactory
    published: _Generation | None = None
    problem: IndexProblem | None = None
    refresh: IndexProblem | None = None

    @property
    def broken(self) -> bool:
        # Known damaged: unreadable until a replacement is published.
        return self.problem in (IndexProblem.INTEGRITY, IndexProblem.SEARCH)

    def usable(self, view: _View) -> bool:
        # Do not even query stale postings of a now-unauthorized document.
        return (
            self.published is not None
            and not self.broken
            and self.published.versions <= view.versions
        )

    def answers(self, view: _View) -> bool:
        return self.usable(view) and self.problem is not IndexProblem.UNAVAILABLE

    def coverage(self) -> int:
        # The authorized records a usable generation holds at their current
        # version: all it holds, since a usable one holds nothing else.
        return 0 if self.published is None else len(self.published.versions)


def _manifest(documents: tuple[IndexDocument, ...]) -> tuple[DocumentSeal, ...]:
    result = []
    for document in documents:
        body = " ".join(document.terms).encode("ascii")
        result.append(DocumentSeal(document.id, len(body), hashlib.sha256(body).digest()))
    return tuple(result)


def _state(slot: _Slot, view: _View, *, current: bool) -> IndexState:
    if not view.store.enabled:
        return IndexState.DISABLED
    if slot.broken:
        return IndexState.FAILED
    if slot.published is None:
        # Nothing to answer from: the last refresh says why.
        if slot.refresh is IndexProblem.BUILD:
            return IndexState.FAILED
        if slot.refresh is IndexProblem.UNAVAILABLE:
            return IndexState.UNAVAILABLE
        return IndexState.EMPTY
    # A failed refresh kept this generation; only its own lookup says
    # whether it can answer.
    if slot.problem is IndexProblem.UNAVAILABLE:
        return IndexState.UNAVAILABLE
    return IndexState.CURRENT if current else IndexState.STALE


def _status(slot: _Slot, view: _View) -> IndexStatus:
    """Maintenance status: the ledger's audit positions, never a launch's answer."""
    generation = slot.published
    source = view.store.source_seq
    # A generation known damaged verifies no coverage, whatever it was built from.
    through = None if generation is None or slot.broken else generation.audit
    # A generation's audit position is the ledger length of the view it holds
    # (moved on only by rows that leave the binding's corpus as it was), and a
    # continued checkpoint of the same length has the same rows: equal
    # positions mean the generation holds the corpus exactly.
    return IndexStatus(
        slot.backend,
        _state(slot, view, current=source == through),
        source,
        through,
        source - (through or 0),
        None if generation is None else generation.serial,
        slot.problem,
        slot.refresh,
    )


def _freshness(slot: _Slot, view: _View) -> ScopeFreshness:
    """A launch answer's status: scoped to the authorized corpus, no audit position."""
    generation = slot.published
    # Coverage is what an intact generation was verified to hold, never the
    # manifest of one known damaged: that one holds no record verifiably.
    held = dict(generation.versions) if generation is not None and not slot.broken else {}
    now = dict(view.versions)
    # Each record whose verified indexed version differs counts once: added,
    # corrected, no longer authorized, or not verifiably held.
    pending = sum(held.get(i) != now.get(i) for i in held.keys() | now.keys())
    return ScopeFreshness(
        slot.backend,
        _state(slot, view, current=not pending),
        view.fingerprint,
        pending,
        None if generation is None else generation.serial,
        slot.problem,
        slot.refresh,
    )


class MemoryRetriever:
    """An index and continuity guard for one controller-owned next-launch binding.

    The loader returns the current validated checkpoint (not a caller-supplied
    snapshot). As with launch projection, the controller holds the run lock and
    serializes calls. Recreate this object on a new binding or an explicit
    opt-in. Nothing writes to Memory or renders prose into the launch itself.
    """

    def __init__(
        self,
        load: Callable[[], Checkpoint],
        request: ProjectionRequest,
        *,
        backend: IndexFactory | None = None,
        fallback_to_fts5: bool = False,
    ):
        self._load = load
        self._request = request
        self._view: _View | None = None
        self._opted_out = False
        self._primary = (
            _Slot(Backend.FTS5, _fts5) if backend is None else _Slot(Backend.OPTIONAL, backend)
        )
        self._fallback = (
            _Slot(Backend.FTS5, _fts5) if backend is not None and fallback_to_fts5 else None
        )
        self._metrics = ReadMetrics()

    @property
    def metrics(self) -> ReadMetrics:
        return self._metrics

    def _read(self, *, serving: bool = True) -> _View:
        checkpoint = self._load()
        store = checkpoint.store
        old = self._view
        if old is not None and store is not old.store and not store.follows(old.store):
            raise StateError("memory retrieval checkpoint did not continue this run")
        if store.enabled and self._opted_out:
            raise StateError("memory retrieval needs an explicit binding after opt-out")
        if not store.enabled:
            self._opted_out = True
            view = _View(store, (), None)
        else:
            # Remember even a newer checkpoint that refuses this launch binding.
            self._view = _View(store, (), None)
            records = store.retrieval_records(
                self._request,
                validation_records=checkpoint.validation_records,
                retained=checkpoint.retained,
            )
            request = self._request
            scope = ReadScope(
                store.repository,
                request.run_id,
                request.step,
                request.attempt,
                request.candidate,
                request.environment,
            )
            view = _View(store, records, scope)
        self._view = view
        if serving and view.scope is None:
            raise StateError("memory retrieval is disabled")
        return view

    def status(self) -> IndexStatus:
        """Controller diagnostic for the primary, with global audit positions."""
        return _status(self._primary, self._read(serving=False))

    def _build(self, slot: _Slot, view: _View, *, force: bool) -> _Slot:
        old = slot.published
        if (
            not force
            and slot.refresh is None
            and not slot.broken
            and old is not None
            and old.versions == view.versions
        ):
            # Hidden writes don't create a new generation or trigger adapter
            # work, and neither does a lookup's unavailability: the next
            # search retries that lookup. A failed refresh is retried.
            return replace(slot, published=replace(old, audit=view.store.source_seq))
        documents = tuple(
            IndexDocument(r.fact.id, tuple(_key(w) for w in _words(r.text))) for r in view.records
        )
        manifest = _manifest(documents)
        try:
            index = slot.factory(documents)
            index.verify(manifest)
        except IndexUnavailable:
            failure = IndexProblem.UNAVAILABLE if old is None else IndexProblem.BUILD
            # The old generation keeps its own condition, unavailable or not.
            return replace(slot, refresh=failure)
        except BackendError:
            return replace(slot, refresh=IndexProblem.BUILD)
        generation = _Generation(
            index,
            view.versions,
            manifest,
            view.store.source_seq,
            1 if old is None else old.serial + 1,
        )
        return _Slot(slot.backend, slot.factory, generation)

    def _sync(self, *, force: bool) -> IndexStatus:
        view = self._read(serving=False)
        if view.store.enabled:
            self._primary = self._build(self._primary, view, force=force)
            if self._fallback is not None:
                self._fallback = self._build(self._fallback, view, force=force)
            view = self._read(serving=False)
        return _status(self._primary, view)

    def sync(self) -> IndexStatus:
        """Publish the complete bounded batch; retry a failed attempt even without writes."""
        return self._sync(force=False)

    def rebuild(self) -> IndexStatus:
        """Replace the generation without mutating the current one."""
        return self._sync(force=True)

    @staticmethod
    def _verify(slot: _Slot) -> _Slot:
        if slot.published is not None:
            try:
                slot.published.index.verify(slot.published.manifest)
            except BackendError:
                return replace(slot, problem=IndexProblem.INTEGRITY)
        return slot

    def verify(self) -> IndexStatus:
        """Check the original generation, then report against the latest checkpoint."""
        view = self._read(serving=False)
        if view.store.enabled:
            self._primary = self._verify(self._primary)
            if self._fallback is not None:
                self._fallback = self._verify(self._fallback)
            view = self._read(serving=False)
        return _status(self._primary, view)

    def _prefers_fallback(self, view: _View, *, searching: bool = False) -> bool:
        """Whether the fallback answers in the primary's place (R4-F1 of PR #538).

        It does when it can answer and the primary either cannot or holds
        fewer of the authorized records: a failed refresh of the primary
        keeps its last generation, which a successful refresh of the
        fallback overtakes. Both are refreshed from the same view, so
        nothing else leaves them apart. On equal coverage the primary
        answers. A search also asks a generation whose last lookup was
        unavailable, to retry it.
        """

        def ready(slot: _Slot) -> bool:
            return slot.usable(view) if searching else slot.answers(view)

        fallback = self._fallback
        if fallback is None or not ready(fallback):
            return False
        primary = self._primary
        return not ready(primary) or fallback.coverage() > primary.coverage()

    def _answers(self, view: _View) -> tuple[ScopeFreshness, bool]:
        if self._prefers_fallback(view):
            assert self._fallback is not None
            return _freshness(self._fallback, view), True
        return _freshness(self._primary, view), not self._primary.answers(view)

    def fetch_memory(self, record: int) -> FetchResult:
        view = self._read()
        assert view.scope is not None
        found = next((r for r in view.records if type(record) is int and r.fact.id == record), None)
        index, degraded = self._answers(view)
        self._count(fetch=True, hits=int(found is not None), degraded=degraded)
        # The store's record bound, not the search budget (a fetch is one record).
        item = _item(found, MAX_TEXT_BYTES, MAX_TEXT_BYTES) if found else None
        return FetchResult(view.scope, item, index, view.revision, degraded)

    def catalog(self) -> MemoryCatalog:
        view = self._read()
        assert view.scope is not None
        kinds = Counter(r.fact.kind for r in view.records)
        topics = tuple(sorted(kinds.items()))
        index, degraded = self._answers(view)
        # No record IDs, global positions, source ancestry, or withdrawn hashes:
        # the freshness's fingerprint digests authorized kinds and text alone,
        # and the identity revision, which digests IDs, stays off the catalog.
        return MemoryCatalog(view.scope, topics, index, degraded)

    def _lookup(
        self, slot: _Slot, view: _View, terms: tuple[str, ...]
    ) -> tuple[_Slot, _View, frozenset[tuple[int, int]], set[int]]:
        generation = slot.published
        if generation is None or not slot.usable(view):
            return slot, view, frozenset(), set()
        problem: IndexProblem | None = None
        answer: object = ()
        try:
            answer = generation.index.search(tuple(_key(term) for term in terms))
        except IndexUnavailable:
            problem = IndexProblem.UNAVAILABLE
        except IntegrityError:
            problem = IndexProblem.INTEGRITY
        except BackendError:
            problem = IndexProblem.SEARCH
        # Do not interpret even a raised backend error until we know all
        # documents it searched remain authorized. Nothing calls it after this.
        view = self._read()
        if not slot.usable(view):
            return slot, view, frozenset(), set()
        if problem is None:
            ids = {record for record, _ in generation.versions}
            if (
                type(answer) is not tuple
                or len(answer) > MAX_FACTS
                or any(
                    type(i) is not int or not 1 <= i <= MAX_EVENTS or i not in ids for i in answer
                )
            ):
                problem = IndexProblem.INTEGRITY
            else:
                matched = set(answer)
                query = set(terms)
                expected = {
                    r.fact.id
                    for r in view.records
                    if (r.fact.id, r.fact.revision) in generation.versions
                    and not query.isdisjoint(_words(r.text))
                }
                if matched == expected:
                    # The lookup is available again; a failed refresh is
                    # still failed, and still to be retried.
                    if slot.problem is IndexProblem.UNAVAILABLE:
                        slot = replace(slot, problem=None)
                    return slot, view, generation.versions, matched
                problem = IndexProblem.INTEGRITY
        return replace(slot, problem=problem), view, frozenset(), set()

    def search_memory(self, query: str, *, k: int = 5) -> SearchResult:
        """At most ``k`` ranked hits; their excerpts together fit the binding's budget.

        Each of ``n`` hits is cut to ``max_chars // n`` characters and
        ``min(512, max_bytes // n)`` UTF-8 bytes of the request, its omission
        marker included. No metadata is rendered or counted here.
        """
        terms = _query(query, k)
        view = self._read()
        if not terms:
            assert view.scope is not None
            index, degraded = self._answers(view)
            self._count(fetch=False, hits=0, degraded=degraded)
            return SearchResult(view.scope, (), index, view.revision, True, degraded, False)
        primary, fallback = self._primary, self._fallback
        order = [primary] if fallback is None else [primary, fallback]
        if self._prefers_fallback(view, searching=True):
            # The other slot is still asked if the preferred one cannot
            # answer: an older generation is better than the tail alone.
            order.reverse()
        diagnosed: list[tuple[_Slot, _Slot]] = []
        used: _Slot | None = None
        covered: frozenset[tuple[int, int]] = frozenset()
        matches: set[int] = set()
        for slot in order:
            after, view, held, found = self._lookup(slot, view, terms)
            diagnosed.append((slot, after))
            # A successful empty generation has no covered rows either.
            if after.answers(view):
                used, covered, matches = after, held, found
                break
        for slot, after in diagnosed:
            # A later lookup's authority read can invalidate an earlier one's
            # (so far unrecorded) diagnosis as well as its candidates.
            kept = after if slot.usable(view) else slot
            if slot is primary:
                self._primary = kept
            else:
                self._fallback = kept
        tail = sorted(
            (r for r in view.records if (r.fact.id, r.fact.revision) not in covered),
            key=lambda r: r.fact.revision,
            reverse=True,
        )
        selected = tail[:TAIL_RECORDS]
        hits = _rank(view.records, matches, selected, terms, k, self._request)
        assert view.scope is not None
        degraded = used is None or used is not self._primary
        self._count(fetch=False, hits=len(hits), degraded=degraded)
        return SearchResult(
            view.scope,
            hits,
            _freshness(self._primary if used is None else used, view),
            view.revision,
            len(tail) <= TAIL_RECORDS,
            degraded,
            bool(selected),
        )

    def _count(self, *, fetch: bool, hits: int, degraded: bool) -> None:
        cap = 2**31 - 1
        old = self._metrics
        self._metrics = ReadMetrics(
            min(cap, old.searches + (not fetch)),
            min(cap, old.fetches + fetch),
            min(cap, old.hits + hits),
            min(cap, old.degraded_reads + degraded),
        )


def _rank(
    records: tuple[RetrievalRecord, ...],
    matches: set[int],
    tail: list[RetrievalRecord],
    terms: tuple[str, ...],
    k: int,
    budget: ProjectionRequest,
) -> tuple[MemoryHit, ...]:
    if not terms or (not matches and not tail):
        return ()
    counts = {r.fact.id: Counter(_words(r.text)) for r in records}
    lengths = {i: sum(c.values()) for i, c in counts.items()}
    size = len(counts)
    mean = sum(lengths.values()) / size or 1
    df = {term: sum(term in c for c in counts.values()) for term in terms}
    weights = {term: math.log1p((size - n + 0.5) / (n + 0.5)) for term, n in df.items()}
    candidates = matches | {r.fact.id for r in tail}
    ranked = []
    for r in records:
        i = r.fact.id
        if i not in candidates:
            continue
        norm = 1.2 * (0.25 + 0.75 * lengths[i] / mean)
        score = sum(weights[t] * f * 2.2 / (f + norm) for t in terms if (f := counts[i][t]))
        if score:
            ranked.append((score, r))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].fact.id))
    if not ranked:
        return ()
    # The excerpts share the binding's whole budget, characters and UTF-8
    # bytes alike (bytes bound a byte-level tokenizer's tokens): together they
    # fit both. Nothing here renders the typed metadata, so none of it is
    # counted; a consumer that renders hits must fit its metadata and the
    # excerpts together into the launch's budget (docs/memory-retrieval.md).
    shown = min(k, len(ranked))
    chars = budget.max_chars // shown
    size = min(SUMMARY_BYTES, budget.max_bytes // shown)
    return tuple(MemoryHit(_item(r, chars, size), score) for score, r in ranked[:k])
