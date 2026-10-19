"""An EPIC's native dependency graph: snapshot, ready set and admission re-read (ADR 0008; #179).

The parallel EPIC scheduler decides what is admissible from GitHub's native
graph alone, read through four typed reads of
:class:`~autoforge.github.GitHubClient` (the :class:`GraphReader` protocol):
the repository id, one issue's node, an issue's direct sub-issues and an
issue's ``blocked_by`` list. Prose (``Depends on #n``, checklists, the EPIC
roadmap) is never read, ``blocking`` is never read, and nested EPICs are
never traversed (D3.1, D3.2).

* :func:`build_epic_snapshot` reads the EPIC, its direct sub-issues and the
  blockers of each open child into an immutable :class:`EpicSnapshot`. It
  is complete or it is refused with a typed
  :class:`~autoforge.errors.SnapshotRefused`, never partial (D3.3, D3.4):
  the repository id must match, the EPIC must be open, the child listing
  must hold exactly ``sub_issues_summary.total`` distinct issues and each
  queried blocker listing exactly ``total_blocked_by``, within GitHub's
  documented bounds, and the open children must be acyclic. Entries are
  de-duplicated by issue ``id`` (D3.6). A child whose summary reports no
  blocker is not queried, nor is a closed child, whose blockers decide
  nothing.
* :func:`evaluate` is pure (D4.1, D4.2, D2.5): a snapshot and the local
  facts give one :class:`ChildVerdict` per child, from the closed
  vocabulary ``ready``, ``closed``, ``blocked``, ``unsupported:<shape>``,
  ``owned`` and ``held:<reason>``, in an order that depends on the
  snapshot alone, never on API order, dictionary or set iteration, or time.
* A blocker is satisfied only when it is closed as ``completed``
  (:func:`blocker_satisfaction`); any other closed reason, an open or
  reopened blocker and one the token may not read block (D3.6).
* The snapshot digest and the node digests are SHA-256 over canonical JSON
  of exactly the eligibility facts of D4.4; titles, bodies, labels,
  assignees, milestones and timestamps are never part of them.
* :func:`reread_node` is admission's re-read (D2.2 step 4): one child and
  its complete blocker list, compared with the snapshot's node digest,
  ``unchanged`` or ``stale`` with the differing facts. It never mutates the
  snapshot.

Nothing here writes to GitHub, takes a lock, writes a file or launches a
process: the only client methods it calls are :data:`GRAPH_READS`. A
snapshot lives in memory; #181 records its digests in a claim. Diagnostics
name issue numbers, states and reasons, never a title or a body, and are
redacted and bounded.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from functools import partial
from typing import Protocol, TypeVar

from .errors import GitHubError, GitHubUnavailableError, SnapshotRefused
from .github import MAX_BLOCKED_BY, MAX_SUB_ISSUES, BlockerNode, IssueNode
from .redaction import redact
from .validation import GitHubIssueRef, parse_issue_url

# The version of the digested document: a change to the fact set is a new one.
DIGEST_VERSION = "autoforge.epic-graph/1"
# The bound on a diagnostic or an explanation (a verdict's, a refusal's).
MAX_DIAGNOSTIC_CHARS = 2000


class GraphReader(Protocol):
    """The GitHub reads a snapshot and a re-read use: four reads, no write."""

    def get_repository_id(self, repo: str) -> int: ...

    def get_issue_node(self, url: str) -> IssueNode: ...

    def list_sub_issues(self, url: str) -> tuple[IssueNode, ...]: ...

    def list_blocked_by(self, url: str) -> tuple[BlockerNode, ...]: ...


# Every client method this module may call (a structural test holds it to this).
GRAPH_READS = frozenset(
    {"get_repository_id", "get_issue_node", "list_sub_issues", "list_blocked_by"}
)


# -- vocabulary (ADR 0008 D3.6, D4.1) ------------------------------------------------------


class LocalFact(Enum):
    """What the scheduler knows locally about one issue (D4.1): ``owned`` or ``held:<reason>``.

    #181's reconciliation and #182's supervisor supply them; an issue with no
    local fact (none, ``fresh`` or ``resumable`` at startup) is evaluated
    from the graph alone. ``held:orphaned`` names a namespace bound to this
    EPIC whose issue the snapshot no longer lists, so :func:`evaluate`,
    which gives a verdict per listed child, never meets it.
    """

    OWNED = "owned"
    HELD_SETTLED_NAMESPACE = "held:settled-namespace"
    HELD_STANDALONE_STATE = "held:standalone-state"
    HELD_OTHER_EPIC = "held:other-epic"
    HELD_UNCERTAIN = "held:uncertain"
    HELD_RESUMABLE = "held:resumable"
    HELD_SPAWN_FAILED = "held:spawn-failed"
    HELD_ORPHANED = "held:orphaned"


class VerdictKind(Enum):
    """A child's verdict, in precedence order: the first that applies is the verdict."""

    CLOSED = "closed"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    OWNED = "owned"
    HELD = "held"
    READY = "ready"


class Shape(Enum):
    """A child the scheduler never admits, whatever its blockers (D3.6)."""

    FOREIGN_REPOSITORY = "foreign-repository"
    NESTED_PARENT = "nested-parent"


class Satisfaction(Enum):
    """Whether one blocker still blocks its child (D3.6)."""

    SATISFIED = "satisfied"
    OPEN = "open"
    CLOSED_UNSATISFIED = "closed-unsatisfied"
    UNREADABLE = "unreadable"


def blocker_satisfaction(blocker: BlockerNode) -> Satisfaction:
    """A blocker is satisfied only when it is closed with ``state_reason`` ``completed``.

    Closed as ``not_planned``, as a duplicate, with no reason or with a
    reason GitHub adds later, it is ``closed-unsatisfied``; open (reopened
    included) it is ``open``; one the token may not read is ``unreadable``.
    A blocker in another EPIC or another repository counts by the same live
    state.
    """
    if not blocker.readable:
        return Satisfaction.UNREADABLE
    if blocker.state != "closed":
        return Satisfaction.OPEN
    if blocker.state_reason == "completed":
        return Satisfaction.SATISFIED
    return Satisfaction.CLOSED_UNSATISFIED


# -- the snapshot ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    """``child_id`` is blocked by ``blocker`` (a native ``blocked_by`` dependency)."""

    child_id: int
    blocker: BlockerNode


@dataclass(frozen=True)
class EpicSnapshot:
    """One complete, validated reading of an EPIC's native graph (D3).

    ``children`` are the EPIC's distinct direct sub-issues in ascending
    number; ``edges`` are the blockers of every open child that reports
    one, by child number then blocker. ``observed_at`` is when the reading
    started (diagnostics only, never digested). ``digest`` is
    :func:`snapshot_digest` of the facts. Build it with
    :func:`build_epic_snapshot`.
    """

    repository: str
    repository_id: int
    epic: IssueNode
    children: tuple[IssueNode, ...]
    edges: tuple[Edge, ...]
    observed_at: str
    digest: str

    def child(self, number: int) -> IssueNode:
        """The child of this repository numbered ``number``; ValueError for no such child."""
        for child in self.children:
            if child.number == number and _same_repository(child.repository, self.repository):
                return child
        raise ValueError(f"#{number} is not a child of EPIC #{self.epic.number} in this snapshot")

    def blockers_of(self, child_id: int) -> tuple[BlockerNode, ...]:
        """The blockers recorded for the child whose issue id is ``child_id``."""
        return tuple(edge.blocker for edge in self.edges if edge.child_id == child_id)

    def node_digest(self, number: int) -> str:
        """The node digest of this repository's child ``number`` (D4.4)."""
        child = self.child(number)
        return node_digest(self.repository_id, child, self.blockers_of(child.id))


def _same_repository(left: str | None, right: str | None) -> bool:
    return (left or "").lower() == (right or "").lower()


def _issue_label(repository: str | None, number: int | None, home: str) -> str:
    """``#n`` in the snapshot's repository, ``owner/repo#n`` elsewhere."""
    if _same_repository(repository, home):
        return f"#{number}"
    return f"{repository}#{number}"


def _bounded(text: str) -> str:
    text = redact(text)
    if len(text) <= MAX_DIAGNOSTIC_CHARS:
        return text
    marker = " [... truncated]"
    return text[: MAX_DIAGNOSTIC_CHARS - len(marker)] + marker


def _refuse(
    epic: GitHubIssueRef,
    reason: str,
    detail: str,
    *,
    cycle: tuple[int, ...] = (),
    transient: bool = False,
) -> SnapshotRefused:
    message = _bounded(f"snapshot of EPIC {epic.canonical} refused ({reason}): {detail}")
    return SnapshotRefused(message, reason, cycle=cycle, transient=transient)


R = TypeVar("R")


def _read(epic: GitHubIssueRef, what: str, read: Callable[[], R]) -> R:
    """One GitHub read of the snapshot: a failure refuses it, with the failure as its cause."""
    try:
        return read()
    except GitHubError as exc:
        raise _refuse(
            epic,
            "read-failed",
            f"{what} could not be read: {exc}",
            transient=isinstance(exc, GitHubUnavailableError),
        ) from exc


N = TypeVar("N", IssueNode, BlockerNode)


def _distinct(rows: Iterable[N], epic: GitHubIssueRef, what: str) -> tuple[N, ...]:
    """``rows`` de-duplicated by issue ``id`` (D3.6), first occurrence kept.

    A repeated ``id`` whose facts differ, or two ids naming one issue, is
    an answer that contradicts itself: the snapshot is refused rather than
    one of the two believed.
    """
    by_id: dict[int, N] = {}
    by_name: dict[tuple[str, int], int] = {}
    for row in rows:
        seen = by_id.get(row.id)
        if seen is not None:
            if seen != row:
                raise _refuse(
                    epic, "inconsistent", f"{what} lists issue id {row.id} twice, differently"
                )
            continue
        if row.number is not None and row.repository is not None:
            name = (row.repository.lower(), row.number)
            other = by_name.setdefault(name, row.id)
            if other != row.id:
                raise _refuse(
                    epic,
                    "inconsistent",
                    f"{what} lists {row.repository}#{row.number} under ids {other} and {row.id}",
                )
        by_id[row.id] = row
    return tuple(by_id.values())


def _utc_timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    offset = moment.utcoffset() if moment is not None else None
    if offset is None or offset.total_seconds() != 0 or "T" not in value:
        raise ValueError(f"observed_at must be an ISO 8601 UTC time, got {value!r:.80}")
    return value


def _child_order(node: IssueNode) -> tuple[int, str]:
    return (node.number, node.repository.lower())


def _blocker_order(blocker: BlockerNode) -> tuple[int, str, int, int]:
    """Readable blockers by repository and number, then unreadable ones by id."""
    if blocker.readable:
        return (0, (blocker.repository or "").lower(), blocker.number or 0, blocker.id)
    return (1, "", 0, blocker.id)


def build_epic_snapshot(
    client: GraphReader,
    epic_url: str,
    *,
    repository_id: int,
    observed_at: str | None = None,
) -> EpicSnapshot:
    """Read the EPIC's native graph into a complete :class:`EpicSnapshot`, or refuse.

    ``repository_id`` is the verified repository's numeric id (the RunKey's,
    #178); the EPIC URL's repository must have it now. ``observed_at``
    defaults to the current UTC time. Every way the reading can be less
    than complete raises :class:`~autoforge.errors.SnapshotRefused` with
    its reason: ``read-failed`` (the read's error is the cause;
    ``transient`` when it may succeed later), ``repository-mismatch``,
    ``epic-not-open``, ``children-bound``, ``children-count``,
    ``blockers-bound``, ``blockers-count``, ``inconsistent`` and ``cycle``.
    """
    epic_ref = parse_issue_url(epic_url)
    if isinstance(repository_id, bool) or not isinstance(repository_id, int) or repository_id < 1:
        raise ValueError(f"repository_id must be a positive integer, got {repository_id!r:.40}")
    moment = _utc_timestamp(observed_at)

    live_id = _read(
        epic_ref, "the repository id", lambda: client.get_repository_id(epic_ref.repository)
    )
    if live_id != repository_id:
        raise _refuse(
            epic_ref,
            "repository-mismatch",
            f"{epic_ref.repository} has repository id {live_id}, not the verified {repository_id}",
        )
    epic = _read(epic_ref, "the EPIC", lambda: client.get_issue_node(epic_ref.canonical))
    if not epic.is_open:
        raise _refuse(
            epic_ref,
            "epic-not-open",
            f"the EPIC is {epic.state} ({epic.state_reason or 'no reason'})",
        )

    if epic.sub_issues_total > MAX_SUB_ISSUES:
        raise _refuse(
            epic_ref,
            "children-bound",
            f"the EPIC reports {epic.sub_issues_total} sub-issues, over the bound of "
            f"{MAX_SUB_ISSUES}",
        )
    rows = _read(epic_ref, "the sub-issue listing", lambda: client.list_sub_issues(epic.url))
    children = _distinct(rows, epic_ref, "the sub-issue listing")
    if len(children) > MAX_SUB_ISSUES:
        raise _refuse(
            epic_ref,
            "children-bound",
            f"the sub-issue listing holds {len(children)} issues, over the bound of "
            f"{MAX_SUB_ISSUES}",
        )
    if len(children) != epic.sub_issues_total:
        raise _refuse(
            epic_ref,
            "children-count",
            f"the sub-issue listing holds {len(children)} distinct issues but the EPIC reports "
            f"{epic.sub_issues_total}",
        )
    for child in children:
        if child.id == epic.id or child.parent is None or not child.parent.same_target(epic.ref):
            label = _issue_label(child.repository, child.number, epic.repository)
            parent = child.parent.canonical if child.parent is not None else "none"
            raise _refuse(
                epic_ref,
                "inconsistent",
                f"the sub-issue listing holds {label}, whose parent is {parent}",
            )
    children = tuple(sorted(children, key=_child_order))

    edges: list[Edge] = []
    for child in children:
        if not child.is_open or child.blocked_by_total == 0:
            continue
        label = _issue_label(child.repository, child.number, epic.repository)
        if child.blocked_by_total > MAX_BLOCKED_BY:
            raise _refuse(
                epic_ref,
                "blockers-bound",
                f"{label} reports {child.blocked_by_total} blockers, over the bound of "
                f"{MAX_BLOCKED_BY}",
            )
        listed = _read(
            epic_ref, f"the blockers of {label}", partial(client.list_blocked_by, child.url)
        )
        blockers = _distinct(listed, epic_ref, f"the blockers of {label}")
        if len(blockers) > MAX_BLOCKED_BY:
            raise _refuse(
                epic_ref,
                "blockers-bound",
                f"the blockers of {label} hold {len(blockers)} issues, over the bound of "
                f"{MAX_BLOCKED_BY}",
            )
        if len(blockers) != child.blocked_by_total:
            raise _refuse(
                epic_ref,
                "blockers-count",
                f"the blockers of {label} hold {len(blockers)} distinct issues but {label} "
                f"reports {child.blocked_by_total}",
            )
        edges.extend(Edge(child.id, blocker) for blocker in sorted(blockers, key=_blocker_order))

    cycle = _find_cycle(children, edges)
    if cycle:
        raise _refuse(
            epic_ref,
            "cycle",
            _describe_cycle(cycle, epic.repository),
            cycle=tuple(node.number for node in cycle),
        )

    return EpicSnapshot(
        repository=epic.repository,
        repository_id=repository_id,
        epic=epic,
        children=children,
        edges=tuple(edges),
        observed_at=moment,
        digest=snapshot_digest(repository_id, epic, children, edges),
    )


# -- cycles ----------------------------------------------------------------------------------


def _find_cycle(children: tuple[IssueNode, ...], edges: Iterable[Edge]) -> tuple[IssueNode, ...]:
    """One cycle among the open children, canonically; empty when they are acyclic.

    An edge goes from a child to an open child that blocks it; a child
    blocked by itself is a cycle of one. The cycle reported starts at the
    lowest-numbered open child that lies on any cycle and is the shortest
    one back to it, neighbours taken in ascending number, so it starts at
    its own lowest number and is the same whatever order GitHub answered
    in. A closed child decides nothing, so a cycle through one is none.
    """
    open_children = {child.id: child for child in children if child.is_open}
    graph: dict[int, list[IssueNode]] = {}
    for edge in edges:
        target = open_children.get(edge.blocker.id)
        if edge.child_id in open_children and target is not None:
            graph.setdefault(edge.child_id, []).append(target)
    for targets in graph.values():
        targets.sort(key=_child_order)
    for start in sorted(open_children.values(), key=_child_order):
        previous: dict[int, IssueNode | None] = {}
        queue: deque[IssueNode] = deque()
        for target in graph.get(start.id, ()):
            if target.id not in previous:
                previous[target.id] = None
                queue.append(target)
        while queue:
            node = queue.popleft()
            if node.id == start.id:
                path: list[IssueNode] = []
                step: IssueNode | None = previous[start.id]
                while step is not None:
                    path.append(step)
                    step = previous[step.id]
                return (start, *reversed(path))
            for target in graph.get(node.id, ()):
                if target.id not in previous:
                    previous[target.id] = node
                    queue.append(target)
    return ()


def _describe_cycle(cycle: tuple[IssueNode, ...], home: str) -> str:
    labels = [_issue_label(node.repository, node.number, home) for node in cycle]
    if len(labels) == 1:
        return f"a dependency cycle among open children: {labels[0]} is blocked by itself"
    chain = " -> ".join([*labels, labels[0]])
    return f"a dependency cycle among open children: {chain} (each is blocked by the next)"


# -- digests (ADR 0008 D4.4) -----------------------------------------------------------------


def _child_facts(child: IssueNode) -> dict[str, object]:
    return {
        "id": child.id,
        "number": child.number,
        "state": child.state,
        "state_reason": child.state_reason,
        "repository": child.repository.lower(),
        "parent": (
            None
            if child.parent is None
            else f"{child.parent.repository.lower()}#{child.parent.number}"
        ),
        "sub_issues": child.sub_issues_total,
    }


def _edge_facts(child_id: int, blocker: BlockerNode) -> dict[str, object]:
    return {
        "child": child_id,
        "blocker": blocker.id,
        "state": blocker.state if blocker.readable else "unreadable",
        "state_reason": blocker.state_reason,
        "repository": None if blocker.repository is None else blocker.repository.lower(),
    }


def _sha256(document: dict[str, object]) -> str:
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _facts_list(items: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    """``items`` sorted by their canonical JSON: a list whose order no input decides."""
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))


def snapshot_digest(
    repository_id: int, epic: IssueNode, children: Iterable[IssueNode], edges: Iterable[Edge]
) -> str:
    """SHA-256 over canonical JSON of the snapshot's eligibility facts (D4.4).

    The repository id; the EPIC's id, number and state; every child's id,
    number, state, ``state_reason``, repository, parent and sub-issue count;
    every edge as (child id, blocker id, blocker state, ``state_reason``,
    repository). Keys and lists are sorted; nothing else is included.
    """
    return _sha256(
        {
            "digest": DIGEST_VERSION,
            "repository_id": repository_id,
            "epic": {"id": epic.id, "number": epic.number, "state": epic.state},
            "children": _facts_list(_child_facts(child) for child in children),
            "edges": _facts_list(_edge_facts(edge.child_id, edge.blocker) for edge in edges),
        }
    )


def node_digest(repository_id: int, child: IssueNode, blockers: Iterable[BlockerNode]) -> str:
    """The digest of one child and its blockers: :func:`snapshot_digest`'s facts, restricted."""
    return _sha256(
        {
            "digest": DIGEST_VERSION,
            "repository_id": repository_id,
            "child": _child_facts(child),
            "edges": _facts_list(_edge_facts(child.id, blocker) for blocker in blockers),
        }
    )


# -- evaluation (ADR 0008 D4.1, D4.2) --------------------------------------------------------


@dataclass(frozen=True)
class BlockerStatus:
    """One blocker of a child with whether it is satisfied."""

    blocker: BlockerNode
    satisfaction: Satisfaction

    def describe(self, home: str) -> str:
        blocker = self.blocker
        if not blocker.readable:
            return f"a blocker the token may not read (issue id {blocker.id})"
        label = _issue_label(blocker.repository, blocker.number, home)
        if self.satisfaction is Satisfaction.OPEN:
            return f"{label} (open{', reopened' if blocker.state_reason == 'reopened' else ''})"
        if self.satisfaction is Satisfaction.CLOSED_UNSATISFIED:
            return f"{label} (closed-unsatisfied: {blocker.state_reason or 'no reason'})"
        return f"{label} (closed: completed)"


@dataclass(frozen=True)
class ChildVerdict:
    """The verdict on one child (D4.1) and what explains it.

    ``dependents`` is how many open EPIC children transitively depend on the
    child, the first ordering key (D4.2). ``state_reason`` explains
    ``closed``, ``shape`` ``unsupported``, ``blockers`` (the unsatisfied
    ones, sorted, with state and reason) ``blocked``, and ``fact`` ``owned``
    and ``held``.
    """

    number: int
    issue_id: int
    repository: str
    kind: VerdictKind
    dependents: int
    state_reason: str | None = None
    shape: Shape | None = None
    blockers: tuple[BlockerStatus, ...] = ()
    fact: LocalFact | None = None

    @property
    def reason(self) -> str:
        """The verdict in D4.1's vocabulary: ``unsupported:<shape>``, ``held:<reason>``, ..."""
        if self.kind is VerdictKind.UNSUPPORTED and self.shape is not None:
            return f"unsupported:{self.shape.value}"
        if self.kind is VerdictKind.HELD and self.fact is not None:
            return self.fact.value
        return self.kind.value

    @property
    def ready(self) -> bool:
        return self.kind is VerdictKind.READY

    def explain(self) -> str:
        """A bounded, redacted line: the verdict and what it rests on, never a title."""
        label = f"#{self.number}"
        if self.shape is Shape.FOREIGN_REPOSITORY:
            label = f"{self.repository}#{self.number}"
        if self.kind is VerdictKind.CLOSED:
            detail = f"closed ({self.state_reason or 'no reason'})"
        elif self.kind is VerdictKind.UNSUPPORTED and self.shape is Shape.NESTED_PARENT:
            detail = f"{self.reason}: it has sub-issues of its own; run that EPIC separately"
        elif self.kind is VerdictKind.UNSUPPORTED:
            detail = f"{self.reason}: it lives in {self.repository}"
        elif self.kind is VerdictKind.BLOCKED:
            # Only a child of the snapshot's own repository is ever blocked.
            described = (status.describe(self.repository) for status in self.blockers)
            detail = "blocked by " + ", ".join(described)
        elif self.kind is VerdictKind.READY:
            detail = f"ready ({self.dependents} open children depend on it)"
        else:
            detail = self.reason
        return _bounded(f"{label}: {detail}")


def _check_local_facts(local_facts: Mapping[int, LocalFact] | None) -> dict[int, LocalFact]:
    facts: dict[int, LocalFact] = {}
    for number, fact in (local_facts or {}).items():
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError(f"a local fact names {number!r:.40}, not an issue number")
        if not isinstance(fact, LocalFact):
            raise ValueError(f"the local fact of #{number} is {fact!r:.80}, not a LocalFact")
        facts[number] = fact
    return facts


def _dependents(snapshot: EpicSnapshot) -> dict[int, int]:
    """For each child id, how many open children transitively depend on it (D4.2)."""
    children = {child.id: child for child in snapshot.children}
    dependants: dict[int, set[int]] = {}
    for edge in snapshot.edges:
        if edge.blocker.id in children and edge.child_id in children:
            dependants.setdefault(edge.blocker.id, set()).add(edge.child_id)
    counts: dict[int, int] = {}
    for child_id in children:
        seen: set[int] = set()
        stack = [child_id]
        while stack:
            for dependant in dependants.get(stack.pop(), ()):
                if dependant not in seen and dependant != child_id:
                    seen.add(dependant)
                    stack.append(dependant)
        counts[child_id] = sum(1 for issue_id in seen if children[issue_id].is_open)
    return counts


def _verdict(
    snapshot: EpicSnapshot, child: IssueNode, facts: Mapping[int, LocalFact], dependents: int
) -> ChildVerdict:
    verdict = partial(
        ChildVerdict,
        number=child.number,
        issue_id=child.id,
        repository=child.repository,
        dependents=dependents,
    )
    if not child.is_open:
        return verdict(kind=VerdictKind.CLOSED, state_reason=child.state_reason)
    if not _same_repository(child.repository, snapshot.repository):
        return verdict(kind=VerdictKind.UNSUPPORTED, shape=Shape.FOREIGN_REPOSITORY)
    if child.sub_issues_total > 0:
        return verdict(kind=VerdictKind.UNSUPPORTED, shape=Shape.NESTED_PARENT)
    statuses = (
        BlockerStatus(blocker, blocker_satisfaction(blocker))
        for blocker in sorted(snapshot.blockers_of(child.id), key=_blocker_order)
    )
    unsatisfied = tuple(s for s in statuses if s.satisfaction is not Satisfaction.SATISFIED)
    if unsatisfied:
        return verdict(kind=VerdictKind.BLOCKED, blockers=unsatisfied)
    fact = facts.get(child.number)
    if fact is LocalFact.OWNED:
        return verdict(kind=VerdictKind.OWNED, fact=fact)
    if fact is not None:
        return verdict(kind=VerdictKind.HELD, fact=fact)
    return verdict(kind=VerdictKind.READY)


def evaluate(
    snapshot: EpicSnapshot, local_facts: Mapping[int, LocalFact] | None = None
) -> tuple[ChildVerdict, ...]:
    """One verdict per child of ``snapshot``, in admission order (D4.1, D4.2). Pure.

    Precedence: ``closed`` > ``unsupported`` > ``blocked`` > ``owned`` >
    ``held`` > ``ready``. ``local_facts`` maps an issue number of the
    snapshot's repository to its :class:`LocalFact`; a number the snapshot
    does not list is not a child and gets no verdict. The tuple is ordered
    by descending ``dependents``, then ascending issue number (then
    repository), so its ``ready`` verdicts, in order, are the ready set as
    admission takes it (:func:`ready_set`). The same snapshot and facts give
    the identical tuple whatever order GitHub answered in.
    """
    facts = _check_local_facts(local_facts)
    dependents = _dependents(snapshot)
    verdicts = (
        _verdict(snapshot, child, facts, dependents[child.id]) for child in snapshot.children
    )
    return tuple(sorted(verdicts, key=lambda v: (-v.dependents, v.number, v.repository.lower())))


def ready_set(verdicts: Iterable[ChildVerdict]) -> tuple[ChildVerdict, ...]:
    """The ``ready`` verdicts of :func:`evaluate`'s tuple, in its order."""
    return tuple(verdict for verdict in verdicts if verdict.ready)


# -- the admission re-read (ADR 0008 D2.2 step 4) --------------------------------------------


class RereadStatus(Enum):
    UNCHANGED = "unchanged"
    STALE = "stale"


@dataclass(frozen=True)
class NodeReread:
    """What admission's re-read of one child found.

    ``stale`` when any eligibility fact differs from the snapshot, or the
    blocker listing could not be read completely; ``differences`` names each
    (bounded, numbers and states only). ``node_digest`` is the digest of
    the facts re-read.
    """

    number: int
    status: RereadStatus
    node_digest: str
    differences: tuple[str, ...] = ()

    @property
    def unchanged(self) -> bool:
        return self.status is RereadStatus.UNCHANGED


def _child_differences(old: IssueNode, new: IssueNode, epic: IssueNode) -> list[str]:
    differences: list[str] = []
    if new.id != old.id:
        differences.append(f"issue id {old.id} -> {new.id}")
    if new.state != old.state or new.state_reason != old.state_reason:
        if new.is_open:
            differences.append(
                f"state {old.state} ({old.state_reason or 'no reason'}) -> open "
                f"({new.state_reason or 'no reason'})"
            )
        else:
            differences.append(f"no longer open: closed ({new.state_reason or 'no reason'})")
    if _child_facts(new)["parent"] != _child_facts(old)["parent"]:
        now = "no parent" if new.parent is None else f"parent {new.parent.canonical}"
        if old.parent is not None and old.parent.same_target(epic.ref):
            differences.append(f"{now}, no longer the EPIC #{epic.number}")
        else:
            differences.append(now)
    if new.sub_issues_total != old.sub_issues_total:
        differences.append(f"sub-issues {old.sub_issues_total} -> {new.sub_issues_total}")
    if new.repository.lower() != old.repository.lower():
        differences.append(f"repository {old.repository} -> {new.repository}")
    return differences


def _blocker_state(blocker: BlockerNode) -> str:
    if not blocker.readable:
        return "unreadable"
    return f"{blocker.state} ({blocker.state_reason or 'no reason'})"


def _blocker_label(blocker: BlockerNode, home: str) -> str:
    if not blocker.readable:
        return f"blocker id {blocker.id}"
    return f"blocker {_issue_label(blocker.repository, blocker.number, home)}"


def _edge_differences(
    old: tuple[BlockerNode, ...], new: tuple[BlockerNode, ...], home: str
) -> list[str]:
    before = {blocker.id: blocker for blocker in old}
    after = {blocker.id: blocker for blocker in new}
    differences: list[str] = []
    for blocker in sorted(after.values(), key=_blocker_order):
        previous = before.get(blocker.id)
        label = _blocker_label(blocker, home)
        if previous is None:
            differences.append(f"{label} added ({_blocker_state(blocker)})")
        elif _edge_facts(0, previous) != _edge_facts(0, blocker):  # one child: its facts alone
            differences.append(f"{label}: {_blocker_state(previous)} -> {_blocker_state(blocker)}")
    for blocker in sorted(before.values(), key=_blocker_order):
        if blocker.id not in after:
            differences.append(f"{_blocker_label(blocker, home)} removed")
    return differences


def reread_node(client: GraphReader, snapshot: EpicSnapshot, issue: int) -> NodeReread:
    """Re-read child ``issue`` and its complete blocker list; compare with the snapshot.

    ``unchanged`` only when the node digest of the re-read facts equals the
    snapshot's. Otherwise ``stale``, with the differences: not open, a
    different ``id``, a parent that is no longer the EPIC, sub-issues now
    present, a blocker added, removed or in another state, or a blocker
    listing that is not complete (short of, or over, its count or bound).
    The blockers of a child that is no longer open are not read. A read
    that fails raises its GitHubError: it is never ``unchanged``. The
    snapshot is not changed.
    """
    old = snapshot.child(issue)
    node = client.get_issue_node(old.url)
    blockers: tuple[BlockerNode, ...] = ()
    problems: list[str] = []
    if node.is_open and node.blocked_by_total > 0:
        if node.blocked_by_total > MAX_BLOCKED_BY:
            problems.append(
                f"{node.blocked_by_total} blockers reported, over the bound of {MAX_BLOCKED_BY}"
            )
        else:
            listed = client.list_blocked_by(old.url)
            by_id: dict[int, BlockerNode] = {}
            for blocker in listed:
                if by_id.setdefault(blocker.id, blocker) != blocker:
                    problems.append(f"the blocker listing names issue id {blocker.id} twice")
            blockers = tuple(sorted(by_id.values(), key=_blocker_order))
            if len(blockers) != node.blocked_by_total:
                problems.append(
                    f"the blocker listing holds {len(blockers)} distinct issues but "
                    f"{node.blocked_by_total} are reported"
                )
    digest = node_digest(snapshot.repository_id, node, blockers)
    differences = _child_differences(old, node, snapshot.epic)
    if node.is_open:
        old_blockers = tuple(sorted(snapshot.blockers_of(old.id), key=_blocker_order))
        differences.extend(_edge_differences(old_blockers, blockers, snapshot.repository))
    differences.extend(problems)
    if digest == snapshot.node_digest(issue) and not problems:
        return NodeReread(issue, RereadStatus.UNCHANGED, digest)
    if not differences:
        differences.append("the node digest differs from the snapshot's")
    return NodeReread(
        issue, RereadStatus.STALE, digest, tuple(_bounded(item) for item in differences)
    )
