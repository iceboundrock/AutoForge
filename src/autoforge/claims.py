"""Durable GitHub claims: the one identity contract for controller-adoptable writes.

An agent's GitHub write (an implementation PR, a review comment, an EPIC
progress comment, a follow-up issue) is something a later controller entry
must be able to *find again* and something the post-agent read-back must be
able to *hold the agent to*. Both need the same answer to the same question:
"which object is this phase's write?". This module owns that answer.

The identity of such a write is a **marker**: an HTML comment
``<!-- <name>: <json object> -->`` that the controller renders into the
agent's prompt and the agent copies verbatim into the object. Each marker
kind has exactly one typed payload (:class:`ImplementationClaim`,
:class:`ReviewClaim`, :class:`ProgressClaim`, :class:`FollowUpClaim`), one
exact-schema decoder, one renderer, and one ``key``: the value two claims
are compared by. Nothing outside this module parses a marker.

The contract every consumer gets from :func:`collect`:

- **Every marker is classified, none is skipped.** A marker of the kind
  whose payload is not exactly the documented shape is a *defect* of the
  object carrying it, not "no marker". A defect anywhere in a collection
  makes every question about that collection inconclusive
  (:class:`ClaimConflictError`), because "no object claims this key" cannot
  be proven while an object carries a claim that could not be read.
- **One object, one compatible identity.** A PR carries at most one
  implementation marker, a comment at most one review or progress marker,
  an issue any number of follow-up markers with distinct keys. An object
  that publishes more is defective, whatever the extra marker names: a
  provenance that names two things proves neither.
- **Cardinality is explicit.** :meth:`Claimants.at_most_one` (an entry:
  nothing yet, or the one write to adopt) and :meth:`Claimants.exactly_one`
  (a read-back: the write the agent claims must exist and be the only one).
  There is no "not more than one".
- **Identity is GitHub's.** URLs inside payloads are parsed with the typed
  parsers and compared by :attr:`GitHubRef.identity` (owner and repository
  case-insensitive, plus number), never as strings. They are bounded by
  ``MAX_URL_CHARS`` before they are parsed, exactly as ``CONTROL_RESULT``
  URLs are: a marker is untrusted text, and the defect it produces is
  persisted as a block reason, so an oversized value is refused by its
  length and never quoted.

``replan_txn`` keeps its own transaction marker: that one is bound to a
controller-generated transaction id and a PR-number watermark, and its
acceptance rules (source PR, pre-existing PRs) are a different protocol.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from .errors import ClaimConflictError, ConfigurationError
from .result_parser import FINDING_ID_RE, MAX_FINDING_ID_CHARS, MAX_URL_CHARS
from .validation import GitHubIssueRef, GitHubPullRequestRef, parse_issue_url, parse_pr_url

_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# A git refname component is at most 255 bytes and a branch name a few of
# them; the bound only keeps an oversized marker value out of a block reason.
MAX_BASE_REF_CHARS = 512
_BASE_REF_FORBIDDEN_RE = re.compile(r"[\s\x00-\x1f\x7f]")


class Marked(Protocol):
    """A GitHub object a marker can live in: it has a URL and a body."""

    @property
    def url(self) -> str: ...

    @property
    def body(self) -> str: ...


C = TypeVar("C", bound="Claim")
O = TypeVar("O", bound=Marked)  # noqa: E741 - the object a claim is found in


class Claim(Protocol):
    @property
    def key(self) -> Hashable: ...


# -- payloads ------------------------------------------------------------------


@dataclass(frozen=True)
class ImplementationClaim:
    """``ai-implementation``: the PR body names the issue it implements."""

    issue: GitHubIssueRef

    @property
    def key(self) -> Hashable:
        return self.issue.identity


@dataclass(frozen=True)
class ReviewClaim:
    """``ai-review-result``: a review comment names the revision its round decided on.

    The revision is the PR diff: the reviewed HEAD against the base branch
    the PR targeted when the round was bound. Both are part of the key, so
    a comment posted while the PR targeted another base is not this
    round's, exactly as one posted at another HEAD is not, and is never
    adopted by a later entry as a review of the base the PR targets now.

    ``reviewed_base_ref`` is ``None`` for a marker written before the base
    was part of the schema. Such a comment reviewed a base nobody recorded,
    so it matches no bound key and is never adopted; it is not a defect
    either, because a mid-flight PR carries one from every earlier round
    and a defect anywhere would block every later entry on that PR.
    """

    round: int
    reviewed_head_sha: str  # lowercased
    needs_fix_round: bool
    finding_ids: tuple[str, ...] | None = None
    reviewed_base_ref: str | None = None

    @property
    def key(self) -> Hashable:
        return (self.round, self.reviewed_head_sha, self.reviewed_base_ref)


@dataclass(frozen=True)
class ProgressClaim:
    """``ai-epic-progress``: an EPIC comment names the finished issue and merged PR."""

    issue: GitHubIssueRef
    pr: GitHubPullRequestRef

    @property
    def key(self) -> Hashable:
        return (self.issue.identity, self.pr.identity)


@dataclass(frozen=True)
class FollowUpClaim:
    """``ai-follow-up``: a follow-up issue names the PR and finding it defers."""

    pr: GitHubPullRequestRef
    finding_id: str

    @property
    def key(self) -> Hashable:
        return (self.pr.identity, self.finding_id)


# -- exact-schema decoding ---------------------------------------------------------


def _exact_keys(payload: dict, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    keys = set(payload)
    missing = sorted(set(required) - keys)
    unknown = sorted(keys - set(required) - set(optional))
    if missing:
        raise ValueError(f"missing key(s) {', '.join(missing)}")
    if unknown:
        raise ValueError(f"unknown key(s) {', '.join(unknown)}")


def _url(payload: dict, key: str, what: str) -> str:
    """The URL string under ``key``, bounded before any parser can quote it.

    The ``MAX_URL_CHARS`` rule of ``result_parser``: the typed parsers quote
    the value in their error, and a marker's error reaches a block reason
    and the run log, so an oversized value is refused by its length alone
    and never quoted. A marker is GitHub-authored text like a
    ``CONTROL_RESULT`` is agent-authored text; both cross the same boundary.
    """
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a GitHub {what} URL string")
    if len(value) > MAX_URL_CHARS:
        raise ValueError(
            f"{key} is {len(value)} characters; a GitHub {what} URL is at most {MAX_URL_CHARS}"
        )
    return value


def _issue_ref(payload: dict, key: str) -> GitHubIssueRef:
    value = _url(payload, key, "issue")
    try:
        return parse_issue_url(value)
    except ConfigurationError as exc:
        raise ValueError(f"{key} is not a GitHub issue URL ({exc})") from exc


def _pr_ref(payload: dict, key: str) -> GitHubPullRequestRef:
    value = _url(payload, key, "pull request")
    try:
        return parse_pr_url(value)
    except ConfigurationError as exc:
        raise ValueError(f"{key} is not a GitHub pull request URL ({exc})") from exc


def _base_ref(value: object) -> str:
    """The base branch a review marker names, as GitHub reports it.

    Compared for equality with the ``baseRefName`` the controller bound
    right before the round, never interpreted, so the only shape rule is
    the one git imposes on every refname (non-empty, no whitespace, no
    control character) plus a bound before the value can be quoted. A
    comment delimiter inside the name is not refused here: git allows it,
    and :func:`marker_json` keeps it out of the rendered marker text.
    """
    if not isinstance(value, str):
        raise ValueError("reviewed_base_ref must be a branch name string")
    if not value or len(value) > MAX_BASE_REF_CHARS:
        raise ValueError(
            f"reviewed_base_ref is {len(value)} characters; a branch name is 1 to "
            f"{MAX_BASE_REF_CHARS}"
        )
    if _BASE_REF_FORBIDDEN_RE.search(value):
        raise ValueError(
            f"reviewed_base_ref {value!r} is not a branch name (whitespace or control character)"
        )
    return value


def _finding_id(value: object, what: str, round_: int | None = None) -> str:
    """The finding id rule of ``result_parser``, applied to marker data.

    Length is checked before shape so the reason never quotes an unbounded
    string; the id is quoted with ``repr`` so control characters cannot
    reach a block reason or a prompt unescaped.
    """
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    if len(value) > MAX_FINDING_ID_CHARS:
        raise ValueError(f"{what} is longer than {MAX_FINDING_ID_CHARS} characters")
    match = FINDING_ID_RE.match(value)
    if match is None:
        raise ValueError(f"{what} {value!r} is not a finding id of the form R<round>-F<n>")
    if round_ is not None and int(match.group("round")) != round_:
        raise ValueError(f"{what} {value!r} does not belong to round {round_}")
    return value


def _decode_implementation(payload: dict) -> ImplementationClaim:
    _exact_keys(payload, ("issue",))
    return ImplementationClaim(issue=_issue_ref(payload, "issue"))


def _decode_review(payload: dict) -> ReviewClaim:
    _exact_keys(
        payload,
        ("round", "reviewed_head_sha", "needs_fix_round"),
        ("finding_ids", "reviewed_base_ref"),
    )
    round_ = payload["round"]
    if not isinstance(round_, int) or isinstance(round_, bool) or round_ < 1:
        raise ValueError(f"round must be a JSON integer >= 1, got {round_!r}")
    sha = payload["reviewed_head_sha"]
    if not isinstance(sha, str) or not _FULL_SHA_RE.match(sha):
        raise ValueError(f"reviewed_head_sha must be a 40-character git SHA, got {sha!r}")
    needs_fix = payload["needs_fix_round"]
    if not isinstance(needs_fix, bool):
        raise ValueError(f"needs_fix_round must be a JSON boolean, got {needs_fix!r}")
    finding_ids: tuple[str, ...] | None = None
    if "finding_ids" in payload:
        raw = payload["finding_ids"]
        if not isinstance(raw, list):
            raise ValueError("finding_ids must be a JSON array")
        ids = [_finding_id(v, "finding_ids entry", round_) for v in raw]
        if len(set(ids)) != len(ids):
            raise ValueError("finding_ids repeats an id")
        finding_ids = tuple(ids)
    base_ref: str | None = None
    if "reviewed_base_ref" in payload:
        base_ref = _base_ref(payload["reviewed_base_ref"])
    return ReviewClaim(
        round=round_,
        reviewed_head_sha=sha.lower(),
        needs_fix_round=needs_fix,
        finding_ids=finding_ids,
        reviewed_base_ref=base_ref,
    )


def _decode_progress(payload: dict) -> ProgressClaim:
    _exact_keys(payload, ("issue", "pr"))
    return ProgressClaim(issue=_issue_ref(payload, "issue"), pr=_pr_ref(payload, "pr"))


def _decode_follow_up(payload: dict) -> FollowUpClaim:
    _exact_keys(payload, ("finding_id", "pr"))
    return FollowUpClaim(
        pr=_pr_ref(payload, "pr"), finding_id=_finding_id(payload["finding_id"], "finding_id")
    )


# -- kinds ----------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkerKind(Generic[C]):
    """One marker kind: its name, its exact-schema decoder, and its object rule."""

    name: str
    decode: Callable[[dict], C]
    several_per_object: bool  # distinct keys may share one object (follow-ups)

    @property
    def pattern(self) -> re.Pattern[str]:
        # Every *complete* comment bearing the name, whatever the payload
        # looks like, so a broken payload is classified rather than passed
        # over. The payload may not contain a comment delimiter: an
        # unterminated marker is not a complete comment and must not swallow
        # the body up to a later ``-->`` and hide a valid marker in the match.
        #
        # Cost is part of the contract: this pattern runs over every open
        # issue body and every PR body the controller lists, which is text
        # anyone can edit. It is linear in the body: the whitespace runs are
        # possessive (``*+``, no backtracking into them) and no whitespace
        # quantifier touches the lazy payload, so an unterminated marker
        # followed by a long run of blanks is one failed pass, not a
        # quadratic-or-worse search for a split between payload and blanks.
        # :func:`scan` strips the payload instead.
        return re.compile(
            rf"<!--\s*+{re.escape(self.name)}\s*+:(?P<payload>(?:(?!-->|<!--)[\s\S])*?)-->"
        )

    def render(self, payload: dict) -> str:
        """The exact marker text for ``payload``; it round-trips through ``decode``."""
        self.decode(payload)  # a renderer that emits an undecodable marker is a bug
        return f"<!-- {self.name}: {marker_json(payload)} -->"


def marker_json(value: object) -> str:
    """``value`` as JSON text that can live inside an HTML comment.

    A marker payload is JSON between ``<!--`` and ``-->``, and :func:`scan`
    ends the payload at the first comment delimiter it meets, as an HTML
    parser does. A payload string that contains one (``x-->y`` and ``a<!--b``
    are valid git branch names, so a ``reviewed_base_ref`` can) would
    truncate the marker it is part of and leave the scanner an unterminated
    JSON string: the object then carries a *defect*, and no review of that
    PR can ever be read back. ``<`` and ``>`` are therefore emitted as the
    JSON escapes ``\\u003c`` and ``\\u003e``, which every JSON decoder
    reads back to the same characters; both delimiters (and the ``--!>``
    an HTML parser also treats as a close) contain one of them, so no
    payload rendered here can end its own comment. The replacement is
    textual and safe: ``json.dumps`` escapes non-ASCII by default, so the
    two characters occur only as themselves, and only inside a string
    literal, since neither is JSON structure.

    This is the one JSON encoder for marker text: the renderers use it,
    and so does the prompt variable a reviewer copies into its marker.
    """
    return json.dumps(value, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")


IMPLEMENTATION: MarkerKind[ImplementationClaim] = MarkerKind(
    "ai-implementation", _decode_implementation, several_per_object=False
)
REVIEW: MarkerKind[ReviewClaim] = MarkerKind("ai-review-result", _decode_review, False)
PROGRESS: MarkerKind[ProgressClaim] = MarkerKind("ai-epic-progress", _decode_progress, False)
FOLLOW_UP: MarkerKind[FollowUpClaim] = MarkerKind("ai-follow-up", _decode_follow_up, True)


def render_implementation_marker(issue_url: str) -> str:
    """The exact ``ai-implementation`` marker the issue's implementation PR carries."""
    return IMPLEMENTATION.render({"issue": parse_issue_url(issue_url).canonical})


def render_progress_marker(issue_url: str, pr_url: str) -> str:
    """The exact ``ai-epic-progress`` marker of one UPDATE_EPIC entry (issue, PR)."""
    return PROGRESS.render(
        {"issue": parse_issue_url(issue_url).canonical, "pr": parse_pr_url(pr_url).canonical}
    )


def render_follow_up_marker(pr_url: str, finding_id: str) -> str:
    """The exact ``ai-follow-up`` marker of one deferral (PR, finding id)."""
    return FOLLOW_UP.render({"finding_id": finding_id, "pr": parse_pr_url(pr_url).canonical})


# -- scanning one object ------------------------------------------------------------


@dataclass(frozen=True)
class Scan(Generic[C]):
    """Every marker of one kind in one body: the usable claims and the defects."""

    claims: tuple[C, ...]
    defects: tuple[str, ...]  # human-readable reasons; non-empty means "defective object"


def scan(kind: MarkerKind[C], body: str) -> Scan[C]:
    """Classify every complete ``kind`` marker in ``body``.

    A defect is any complete marker whose payload is not exactly the kind's
    schema (unparsable text, a JSON scalar or array, a missing, unknown or
    mistyped key), a second marker on an object that may carry one, or a
    repeated key on an object that may carry several. Defects are reported,
    never skipped: an object that tried to carry a claim and failed is
    evidence of a botched write, not of an unrelated object.
    """
    claims: list[C] = []
    defects: list[str] = []
    for match in kind.pattern.finditer(body or ""):
        raw = match.group("payload").strip()
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            defects.append(f"{kind.name} marker payload is not valid JSON ({exc})")
            continue
        if not isinstance(payload, dict):
            defects.append(f"{kind.name} marker payload must be a JSON object")
            continue
        try:
            claims.append(kind.decode(payload))
        except ValueError as exc:
            defects.append(f"{kind.name} marker payload is invalid ({exc})")
    if not kind.several_per_object and len(claims) > 1:
        defects.append(
            f"carries {len(claims)} {kind.name} markers; an object publishes exactly one "
            "and a provenance that names two things proves neither"
        )
    elif kind.several_per_object:
        keys = [c.key for c in claims]
        if len(set(keys)) != len(keys):
            defects.append(f"carries the same {kind.name} marker more than once")
    return Scan(tuple(claims), tuple(defects))


# -- collections and cardinality -------------------------------------------------------


@dataclass(frozen=True)
class Holder(Generic[O, C]):
    """One object and the one claim of the kind it makes for the key asked about."""

    obj: O
    claim: C


@dataclass(frozen=True)
class Claimants(Generic[O, C]):
    """The objects of a collection claiming one key, with explicit cardinality.

    ``defects`` are the defective objects of the *whole* collection, not only
    of the holders: while any object's marker of this kind could not be
    read, "nothing claims this key" is not provable.
    """

    kind: MarkerKind[C]
    what: str  # "issue #12", "round 2 at HEAD ab12...", for messages
    holders: tuple[Holder[O, C], ...]
    defects: tuple[str, ...]
    noun: str  # "open PR", "comment", "open issue"

    def _refuse_defects(self) -> None:
        if self.defects:
            raise ClaimConflictError(
                f"cannot establish which {self.noun} carries the {self.kind.name} marker for "
                f"{self.what}: {'; '.join(self.defects)}"
            )

    def _refuse_many(self) -> None:
        if len(self.holders) > 1:
            urls = ", ".join(sorted(h.obj.url for h in self.holders))
            raise ClaimConflictError(
                f"{len(self.holders)} {self.noun}s carry the {self.kind.name} marker for "
                f"{self.what} ({urls})"
            )

    def at_most_one(self) -> Holder[O, C] | None:
        """An entry's question: nothing yet (launch), or the one write to adopt."""
        self._refuse_defects()
        self._refuse_many()
        return self.holders[0] if self.holders else None

    def exactly_one(self) -> Holder[O, C]:
        """A read-back's question: the write exists and is the only one."""
        self._refuse_defects()
        self._refuse_many()
        if not self.holders:
            raise ClaimConflictError(
                f"no {self.noun} carries the {self.kind.name} marker for {self.what}"
            )
        return self.holders[0]


@dataclass(frozen=True)
class Collection(Generic[O, C]):
    """Every claim of one kind across a collection of objects, scanned once."""

    kind: MarkerKind[C]
    noun: str
    holders: tuple[Holder[O, C], ...]
    defects: tuple[str, ...]

    def claimants(self, key: Hashable, what: str) -> Claimants[O, C]:
        mine = tuple(h for h in self.holders if h.claim.key == key)
        return Claimants(self.kind, what, mine, self.defects, self.noun)

    def grouped(
        self, select: Callable[[C], bool], what: str
    ) -> dict[Hashable, tuple[Holder[O, C], ...]]:
        """Holders whose claim ``select`` accepts, per key; inconclusive on defects."""
        if self.defects:
            raise ClaimConflictError(
                f"cannot establish which {self.noun}s carry {self.kind.name} markers for "
                f"{what}: {'; '.join(self.defects)}"
            )
        out: dict[Hashable, list[Holder[O, C]]] = {}
        for h in self.holders:
            if select(h.claim):
                out.setdefault(h.claim.key, []).append(h)
        return {k: tuple(v) for k, v in out.items()}


def collect(kind: MarkerKind[C], objects: Iterable[O], noun: str) -> Collection[O, C]:
    """Scan every object once; the result answers every claimant question on it."""
    holders: list[Holder[O, C]] = []
    defects: list[str] = []
    for obj in objects:
        result = scan(kind, obj.body)
        for reason in result.defects:
            defects.append(f"{noun} {obj.url}: {reason}")
        holders.extend(Holder(obj, claim) for claim in result.claims)
    return Collection(kind, noun, tuple(holders), tuple(defects))


__all__ = [
    "FOLLOW_UP",
    "IMPLEMENTATION",
    "MAX_BASE_REF_CHARS",
    "PROGRESS",
    "REVIEW",
    "Claimants",
    "Collection",
    "FollowUpClaim",
    "Holder",
    "ImplementationClaim",
    "MarkerKind",
    "ProgressClaim",
    "ReviewClaim",
    "Scan",
    "collect",
    "render_follow_up_marker",
    "render_implementation_marker",
    "render_progress_marker",
    "scan",
]
