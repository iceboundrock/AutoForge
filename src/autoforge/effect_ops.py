"""The I/O half of controller-owned effects: one typed operation per kind.

:mod:`autoforge.effects` holds the records and the pure decisions; this
module performs the reads and the one write of each effect kind and drives a
record through D4.1-D4.3 with them. It knows ``GitHubClient`` and
``GitTransport`` methods, never ``gh`` or ``git`` argv; it holds no workflow
policy beyond the record it is handed, and it persists nothing itself: every
stage change goes through the ``persist`` callback, which the engine wires to
an atomic state save (D4.1: the count is durable before the write is issued).

One call of :func:`drive` issues a record's write at most once, and only
after a reconciliation read decided to (D4.2). The read-back that follows
decides the outcome: the end state is ``observed``; a write GitHub refused
conclusively is a ``conflict`` (D4.3); anything else -- a lost reply, an
unavailable GitHub, a write not yet visible -- leaves the record
``attempted`` and raises, so the next entry reconciles before anything is
sent again. A create is therefore never re-sent blind, and the attempt bound
of :data:`~autoforge.effects.MAX_EFFECT_ATTEMPTS` holds across every crash
window.

Every kind of the closed Wave 1 set (ADR 0004 D5.1) has one operation here:
:class:`PushOp` (K1), :class:`ImplementationPrOp` (K2), :class:`AdoptPrOp`
(K3), :class:`ReviewCommentOp` (K4), :class:`FollowUpIssueOp` (K5),
:class:`FollowUpAppendOp` (K6), :class:`ReplacementPrOp` (K7) and
:class:`ProgressCommentOp` (K8). Each ``reconcile`` is a complete identity
read (D4.5): an open listing read to its end, or an all-states read bounded
by a head-branch filter or a number watermark, never the search API. Each
``issue`` is exactly one write. What an operation checks is the record's own
identity, target and payload; a consumer's further completion predicates
(K2's and K7's head SHA against K1's observed candidate, K7's
``_bind_replacement``) stay with the consumer, which holds the sibling
records.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .claims import FOLLOW_UP, IMPLEMENTATION, PROGRESS, REVIEW, MarkerKind, collect, scan
from .effects import (
    MAX_BODY_CHARS,
    Action,
    Decision,
    EffectKind,
    EffectRecord,
    Found,
    Stage,
    decide_append,
    decide_create,
    decide_push,
)
from .errors import (
    EffectConflictError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
    GitTransportError,
)
from .git_transport import GitTransport, PushOutcome
from .github import GitHubClient, IssueInfo, PRInfo
from .redaction import credential_classes, redact
from .replan_txn import scan_replan_markers
from .validation import parse_issue_url, parse_pr_url

Persist = Callable[[EffectRecord], None]


class EffectOperation(Protocol):
    """One kind's reads and write. ``reconcile`` reads only; ``issue`` writes once."""

    def reconcile(self, record: EffectRecord) -> Decision: ...

    def issue(self, record: EffectRecord) -> None: ...


class EffectPending(GitHubUnavailableError):
    """The write was issued and its outcome is not yet readable; the record stays
    ``attempted`` and the next entry reconciles it (transient, never re-sent blind)."""


@dataclass(frozen=True)
class Driven:
    """A record as last persisted, and what happened to it in this call."""

    record: EffectRecord
    issued: bool  # a write was sent in this call


def drive(record: EffectRecord, op: EffectOperation, persist: Persist) -> Driven:
    """Reconcile ``record`` and issue its write at most once (D4.1-D4.3).

    Returns the record ``observed`` or ``conflict``. Raises, with the record
    left as persisted, when the outcome cannot be read yet:
    :class:`GitHubUnavailableError` from a reconciliation read (nothing was
    persisted or charged), or :class:`EffectPending` after an issue whose
    read-back did not show it. A conclusive read failure
    (:class:`GitHubError`) propagates unchanged for the caller to block on.
    """
    decision = op.reconcile(record)
    if decision.action is Action.DONE:
        return Driven(record, issued=False)
    if decision.action is Action.OBSERVE:
        record = record.observing(decision.observed or {})
        persist(record)
        return Driven(record, issued=False)
    if decision.action is Action.CONFLICT:
        if record.stage is not Stage.CONFLICT:
            record = record.conflicting(decision.reason)
            persist(record)
        return Driven(record, issued=False)
    if decision.action is Action.REBASE:
        record = record.rebasing(decision.rebase_body)
    else:
        record = record.attempting()
    # D4.1: the attempt is durable before the write leaves the process.
    persist(record)
    refused: GitHubError | None = None
    try:
        op.issue(record)
    except GitHubUnavailableError:
        # Ambiguous: it may have landed (W4) or not (W3). Only a read decides.
        pass
    except GitHubError as exc:
        refused = exc
    try:
        after = op.reconcile(record)
    except GitHubUnavailableError as exc:
        raise EffectPending(
            f"{record.describe()} was issued (attempt {record.attempts}) but GitHub could not "
            f"be read back ({exc}); 'resume' reconciles it before anything is sent again"
        ) from exc
    if after.action is Action.OBSERVE:
        record = record.observing(after.observed or {})
        persist(record)
        return Driven(record, issued=True)
    if refused is not None:
        # The read-back's own conflict names the object the write collided with.
        found = (
            after.reason
            if after.action is Action.CONFLICT and not after.exhausted
            else "the target does not hold it"
        )
        record = record.conflicting(
            f"{record.describe()}: GitHub refused the write ({redact(str(refused))}); {found}; "
            "a refused write is not retried"
        )
        persist(record)
        return Driven(record, issued=True)
    if after.action is Action.CONFLICT and not after.exhausted:
        # Not merely absent: another object holds the identity, or the target
        # no longer qualifies. That is not something a later read can resolve.
        record = record.conflicting(after.reason)
        persist(record)
        return Driven(record, issued=True)
    raise EffectPending(
        f"{record.describe()} was issued (attempt {record.attempts}) but its read-back does "
        "not show it yet; the record stays attempted and 'resume' reconciles it before "
        "anything is sent again"
    )


def raise_conflict(record: EffectRecord) -> None:
    """Raise the typed conflict a ``conflict`` record stands for (names the object only)."""
    if record.stage is Stage.CONFLICT:
        raise EffectConflictError(record.reason)


# -- shared reading helpers ------------------------------------------------------------


def _settled(record: EffectRecord) -> Decision | None:
    """The decision of a terminal record, before any read: ``DONE`` or its conflict."""
    if record.pending:
        return None
    return decide_create(record, ())


def _unreadable(record: EffectRecord, noun: str, defects: Sequence[str]) -> Decision:
    """A conflict: the objects that could carry the identity cannot be read unambiguously.

    The defect texts come from the claim scanner and name the object and the
    rule; they never quote a body.
    """
    return Decision(
        Action.CONFLICT,
        reason=(
            f"{record.describe()}: the {noun} cannot be read unambiguously ({'; '.join(defects)})"
        ),
    )


def _same_repository(a: str, b: str) -> bool:
    return bool(a) and a.lower() == b.lower()


def _same_issue(a: str, b: str) -> bool:
    return parse_issue_url(a).identity == parse_issue_url(b).identity


def _same_pr(a: str, b: str) -> bool:
    return parse_pr_url(a).identity == parse_pr_url(b).identity


def _same_repo_head(pr: PRInfo) -> bool:
    """The PR's head branch lives in the PR's own repository (not a fork)."""
    return not pr.head_repository or _same_repository(pr.head_repository, pr.repository)


def _unique(objects: Iterable[PRInfo]) -> list[PRInfo]:
    """PRs of one repository, each once (the first read of it is kept)."""
    seen: set[tuple[str, int]] = set()
    out: list[PRInfo] = []
    for obj in objects:
        key = (obj.repository.lower(), obj.number)
        if key not in seen:
            seen.add(key)
            out.append(obj)
    return out


def _unique_issues(objects: Iterable[IssueInfo]) -> list[IssueInfo]:
    seen: set[tuple[str, int]] = set()
    out: list[IssueInfo] = []
    for obj in objects:
        key = (obj.repository.lower(), obj.number)
        if key not in seen:
            seen.add(key)
            out.append(obj)
    return out


def _append_problem(what: str, kind: MarkerKind, payload: str) -> str:
    """D5.5: why a rebased payload may not be persisted, or "".

    The same rules the record's payload is validated with on save (size, no
    NUL or DEL, redaction-invariant), checked first so a rebase that fails
    them is a ``conflict`` instead of a save error, plus the marker kind's
    own scan: the composed body must leave every marker of ``kind``
    readable. The answer names ``what`` and the pattern class, never the
    matched text or the body.
    """
    if len(payload) > MAX_BODY_CHARS:
        return (
            f"the rebased body of {what} would be {len(payload)} characters, over the "
            f"{MAX_BODY_CHARS}-character limit"
        )
    if "\x00" in payload or "\x7f" in payload:
        return f"the body of {what} now carries a NUL or DEL character"
    classes = credential_classes(payload)
    if classes:
        return (
            f"the body of {what} now holds a credential-shaped string (pattern class: "
            f"{', '.join(classes)}); published text is refused, never redacted. Remove it "
            "from the body, treat the credential as exposed, then 'unblock'"
        )
    if scan(kind, payload).defects:
        return (
            f"the rebased body of {what} would carry a malformed or repeated {kind.name} "
            "marker; the body read now is not one the block can be appended to"
        )
    return ""


# -- K1: push an exact candidate ------------------------------------------------------


class PushRefusedError(GitHubError):
    """K1: the push was refused conclusively, so the ref was not updated (D4.3).

    Git's lease or the remote refused it, or the controller's own transport
    refused before any ``git push`` ran (a default-branch ref, a
    non-fast-forward, an object it could not read). Not
    :class:`GitHubUnavailableError`, so :func:`drive` records ``conflict``
    unless its read-back finds the ref at the candidate; it is never retried.
    """


def _push_branch(record: EffectRecord) -> str:
    return str(record.target["ref"]).removeprefix("refs/heads/")


class PushOp:
    """K1 (ADR 0004 §2.5, §2.6): an exact candidate SHA to one controller-derived ref.

    Identity: the repository, the ref and the candidate. Precondition: the
    ref is at ``expected_old`` (absent when ``None``), the candidate descends
    from it (a fast-forward), and the ref is not the default branch.
    Payload: the candidate SHA. Completion: the remote ref, read through
    the GitHub client, equals the candidate (D4.5: one branch read).

    The push itself goes through :class:`GitTransport` only: an explicit
    URL, a private ``GIT_DIR``, no repository, global or system
    configuration, hooks off, and a compare-and-swap lease (D6, D7). That
    the candidate descends from the recorded base, and the published
    range's messages, are proven by the consumer before it plans the record
    (``prove_range``, D7.5, D8.5).
    """

    def __init__(self, github: GitHubClient, transport: GitTransport, default_branch: str) -> None:
        if not default_branch:
            raise GitTransportError("the default branch must be named to refuse a push to it")
        self.github = github
        self.transport = transport
        self.default_branch = default_branch

    def reconcile(self, record: EffectRecord) -> Decision:
        settled = _settled(record)
        if settled is not None:
            return settled
        branch = _push_branch(record)
        if branch.lower() == self.default_branch.lower():
            return Decision(
                Action.CONFLICT,
                reason=(
                    f"{record.describe()}: {branch!r} is the default branch, which changes "
                    "only through a reviewed PR; a push to it is never issued"
                ),
            )
        try:
            head: str | None = self.github.get_branch_head_sha(record.target["repository"], branch)
        except GitHubNotFoundError:
            head = None
        decision = decide_push(record, head)
        expected = record.precondition["expected_old"]
        if decision.action is not Action.ISSUE or expected is None:
            return decision
        # D6.2: the lease alone would permit a non-fast-forward. Proven here,
        # before an attempt is charged, and again by the transport at issue.
        candidate = record.payload["sha"]
        try:
            fast_forward = self.transport.descends_from(candidate, expected)
        except GitTransportError as exc:
            return Decision(
                Action.CONFLICT,
                reason=(
                    f"{record.describe()}: whether {candidate} descends from the branch's "
                    f"expected head {expected} cannot be proven ({exc}); a push is never "
                    "issued over an unproven ancestry"
                ),
            )
        if not fast_forward:
            return Decision(
                Action.CONFLICT,
                reason=(
                    f"{record.describe()}: {candidate} does not descend from the branch's "
                    f"expected head {expected}, so the push would not be a fast-forward"
                ),
            )
        return decision

    def issue(self, record: EffectRecord) -> None:
        ref = record.target["ref"]
        sha = record.payload["sha"]
        expected = record.precondition["expected_old"]
        try:
            result = self.transport.push(
                sha=sha,
                branch=_push_branch(record),
                expected_old=expected,
                default_branch=self.default_branch,
            )
        except GitTransportError as exc:
            # Raised before any `git push` ran: nothing was sent.
            raise PushRefusedError(
                f"the controller's git transport refused to push {sha} to {ref} before "
                f"sending anything ({exc})"
            ) from exc
        if result.outcome in (PushOutcome.PUSHED, PushOutcome.UP_TO_DATE):
            return
        if result.outcome in (PushOutcome.LEASE_REJECTED, PushOutcome.REMOTE_REJECTED):
            raise PushRefusedError(
                f"git reported {result.outcome.value} for {sha} to {ref} over "
                f"{expected or 'an absent ref'} ({result.detail})"
            )
        raise GitHubUnavailableError(
            f"the push of {sha} to {ref} has an unknown outcome ({result.detail})"
        )


# -- K2: the implementation PR --------------------------------------------------------


class ImplementationPrOp:
    """K2 (ADR 0004 §2.5): the implementation PR, created by the controller.

    Identity: the issue's ``ai-implementation`` marker and the derived head
    branch. Precondition: no PR in any state is headed at the branch, and no
    open PR carries the marker. Payload: title and body (the body ends with
    the marker). Completion: exactly one PR carries the identity, and it is
    open, headed at the branch in this repository, based on the target base
    and titled and bodied as the payload (D4.5: the all-states listing
    filtered by head branch, and the complete open listing; a closed or
    merged PR on the branch is a conflict, never a reason for a second PR).
    """

    def __init__(self, github: GitHubClient) -> None:
        self.github = github

    def reconcile(self, record: EffectRecord) -> Decision:
        settled = _settled(record)
        if settled is not None:
            return settled
        repo = record.target["repository"]
        head = record.target["head"]
        claim = scan(IMPLEMENTATION, record.marker).claims[0]
        on_head = self.github.list_prs_for_head(repo, head)
        open_prs = self.github.list_open_prs(repo)
        claimants = collect(IMPLEMENTATION, open_prs, "open PR").claimants(
            claim.key, f"issue {claim.issue.canonical}"
        )
        if claimants.defects:
            return _unreadable(record, f"open PRs of {repo}", claimants.defects)
        on_head_numbers = {pr.number for pr in on_head}
        found = [
            Found(
                url=pr.url,
                in_end_state=(
                    pr.number in on_head_numbers
                    and pr.is_open
                    and pr.head_ref == head
                    and _same_repo_head(pr)
                    and pr.base_ref == record.target["base"]
                    and pr.title == record.payload["title"]
                    and pr.body == record.body
                ),
                observed={"url": pr.url, "number": pr.number},
            )
            for pr in _unique([*on_head, *(h.obj for h in claimants.holders)])
        ]
        return decide_create(record, found)

    def issue(self, record: EffectRecord) -> None:
        self.github.create_pull_request(
            record.target["repository"],
            base=record.target["base"],
            head=record.target["head"],
            title=record.payload["title"],
            body=record.body,
        )


# -- K3: adopt an existing PR ---------------------------------------------------------


class AdoptPrOp:
    """K3 (ADR 0004 §2.5, D5.5): append the issue's block to an existing PR's body.

    Target: the PR, open, in the owner issue's repository and not headed in
    a fork. Precondition: its body equals the recorded base and carries no
    implementation marker. Payload: the base, the separator and the block
    (``Closes #n`` and the marker). Completion: the body equals the payload
    byte for byte, and among the open PRs (the complete listing, D4.5) the
    marker resolves to this one only. A rebase (D5.5) is judged by size, the
    credential check and the marker scan of the composed body.
    """

    def __init__(self, github: GitHubClient) -> None:
        self.github = github

    def reconcile(self, record: EffectRecord) -> Decision:
        settled = _settled(record)
        if settled is not None:
            return settled
        pr_url = record.target["pr_url"]
        repo = parse_issue_url(record.owner.issue_url).repository
        claim = scan(IMPLEMENTATION, record.marker).claims[0]
        pr = self.github.get_pr(pr_url)
        claimants = collect(IMPLEMENTATION, self.github.list_open_prs(repo), "open PR").claimants(
            claim.key, f"issue {claim.issue.canonical}"
        )
        if claimants.defects:
            return _unreadable(record, f"open PRs of {repo}", claimants.defects)
        others = sorted({h.obj.url for h in claimants.holders if not _same_pr(h.obj.url, pr_url)})
        if others:
            return Decision(
                Action.CONFLICT,
                reason=(
                    f"{record.describe()}: {', '.join(others)} already carries the "
                    f"implementation marker for issue {claim.issue.canonical}; an issue has "
                    "one implementation PR"
                ),
            )
        found = scan(IMPLEMENTATION, pr.body)
        if pr.body != record.body and (
            found.defects or any(c.key != claim.key for c in found.claims)
        ):
            return Decision(
                Action.CONFLICT,
                reason=(
                    f"{record.describe()}: the PR's body already carries another or a malformed "
                    "implementation marker; an adopted PR implements exactly one issue"
                ),
            )
        return decide_append(
            record,
            pr.body,
            target_ok=(
                pr.is_open
                and _same_repository(pr.repository, repo)
                and _same_pr(pr.url, pr_url)
                and _same_repo_head(pr)
            ),
            carries_block_marker=(
                record.marker in pr.body or any(c.key == claim.key for c in found.claims)
            ),
            rebase_problem=lambda payload: _append_problem(pr_url, IMPLEMENTATION, payload),
        )

    def issue(self, record: EffectRecord) -> None:
        self.github.write_pr_body(record.target["pr_url"], record.body)


# -- K4: the review round comment -----------------------------------------------------


class ReviewCommentOp:
    """K4 (ADR 0004 §2.5): the controller-rendered review round comment on the PR.

    Identity: the ``ai-review-result`` marker for the round, reviewed HEAD,
    base ref and merge base. Precondition: no comment of the PR carries it.
    Payload: the rendered comment, ending with the marker. Completion:
    exactly one top-level comment carries the marker, and its body equals
    the payload (D4.5: the complete comment listing of the PR).
    """

    def __init__(self, github: GitHubClient) -> None:
        self.github = github

    def reconcile(self, record: EffectRecord) -> Decision:
        settled = _settled(record)
        if settled is not None:
            return settled
        pr_url = record.target["pr_url"]
        claim = scan(REVIEW, record.marker).claims[0]
        comments = self.github.get_pr_comments(pr_url)
        claimants = collect(REVIEW, comments, "comment").claimants(
            claim.key, f"round {claim.round} at HEAD {claim.reviewed_head_sha} on {pr_url}"
        )
        if claimants.defects:
            return _unreadable(record, "PR's comments", claimants.defects)
        found = [
            Found(
                url=h.obj.url,
                in_end_state=h.obj.body == record.body,
                observed={"url": h.obj.url},
            )
            for h in claimants.holders
        ]
        return decide_create(record, found)

    def issue(self, record: EffectRecord) -> None:
        self.github.create_pr_comment(record.target["pr_url"], record.body)


# -- K5: a follow-up issue ------------------------------------------------------------


class FollowUpIssueOp:
    """K5 (ADR 0004 §2.5): a follow-up issue for one deferred finding.

    Identity: the ``ai-follow-up`` marker for the PR and finding.
    Precondition: no open issue carries it, and no issue numbered above the
    persisted watermark does. Payload: title and body (the body ends with the
    marker). Completion: exactly one issue carries the marker, and it is
    above the watermark, open, in this repository, not the current issue and
    titled and bodied as the payload (D4.5: the complete open listing, and
    the all-states listing read newest first down to the watermark). A
    follow-up created and then closed by a human is a conflict, never a
    reason to create a second one.
    """

    def __init__(self, github: GitHubClient) -> None:
        self.github = github

    def reconcile(self, record: EffectRecord) -> Decision:
        settled = _settled(record)
        if settled is not None:
            return settled
        repo = record.target["repository"]
        watermark = record.precondition["watermark"]
        claim = scan(FOLLOW_UP, record.marker).claims[0]
        open_issues = self.github.list_open_issues(repo, strict=True)
        above = self.github.list_issues_above(repo, watermark)
        issues = _unique_issues([*above, *open_issues])
        claimants = collect(FOLLOW_UP, issues, "issue").claimants(
            claim.key, f"finding {claim.finding_id} of {claim.pr.canonical}"
        )
        if claimants.defects:
            return _unreadable(record, f"issues of {repo}", claimants.defects)
        found = [
            Found(
                url=h.obj.url,
                in_end_state=(
                    h.obj.number > watermark
                    and h.obj.is_open
                    and _same_repository(h.obj.repository, repo)
                    and not _same_issue(h.obj.url, record.owner.issue_url)
                    and h.obj.title == record.payload["title"]
                    and h.obj.body == record.body
                ),
                observed={"url": h.obj.url, "number": h.obj.number},
            )
            for h in claimants.holders
        ]
        return decide_create(record, found)

    def issue(self, record: EffectRecord) -> None:
        self.github.create_issue(
            record.target["repository"], title=record.payload["title"], body=record.body
        )


# -- K6: append follow-up markers to an existing issue --------------------------------


def _marker_keys(kind: MarkerKind, markers: Iterable[str]) -> set[Hashable]:
    return {scan(kind, m).claims[0].key for m in markers}


class FollowUpAppendOp:
    """K6 (ADR 0004 §2.5, D5.5): append a phase's follow-up markers to one handed-over issue.

    Target: the issue, open, in the owner issue's repository and not the
    current issue. Precondition: its body equals the recorded base and
    carries none of the block's markers. Payload: the base, the separator
    and the block (the markers, one per line). Completion: the body equals
    the payload byte for byte, and no other open issue carries a marker of
    the block (one open follow-up per finding; D4.5: the complete open
    listing). A rebase (D5.5) is judged by size, the credential check and
    the marker scan of the composed body.
    """

    def __init__(self, github: GitHubClient) -> None:
        self.github = github

    def reconcile(self, record: EffectRecord) -> Decision:
        settled = _settled(record)
        if settled is not None:
            return settled
        issue_url = record.target["issue_url"]
        repo = parse_issue_url(record.owner.issue_url).repository
        markers: list[str] = list(record.identity["markers"])
        keys = _marker_keys(FOLLOW_UP, markers)
        issue = self.github.get_issue(issue_url)
        listing = collect(FOLLOW_UP, self.github.list_open_issues(repo, strict=True), "open issue")
        if listing.defects:
            return _unreadable(record, f"open issues of {repo}", listing.defects)
        others = sorted(
            {
                h.obj.url
                for h in listing.holders
                if h.claim.key in keys and not _same_issue(h.obj.url, issue_url)
            }
        )
        if others:
            return Decision(
                Action.CONFLICT,
                reason=(
                    f"{record.describe()}: {', '.join(others)} already carries a marker of its "
                    "block; a finding has one open follow-up issue"
                ),
            )
        found = scan(FOLLOW_UP, issue.body)
        return decide_append(
            record,
            issue.body,
            target_ok=(
                issue.is_open
                and _same_repository(issue.repository, repo)
                and _same_issue(issue.url, issue_url)
                and not _same_issue(issue.url, record.owner.issue_url)
            ),
            carries_block_marker=(
                any(m in issue.body for m in markers) or any(c.key in keys for c in found.claims)
            ),
            rebase_problem=lambda payload: _append_problem(issue_url, FOLLOW_UP, payload),
        )

    def issue(self, record: EffectRecord) -> None:
        self.github.write_issue_body(record.target["issue_url"], record.body)


# -- K7: the replacement PR -----------------------------------------------------------


class ReplacementPrOp:
    """K7 (ADR 0004 §2.5): the replan's replacement PR, created by the controller.

    Identity: the transaction's ``autoforge-replan-transaction`` marker and
    the derived replacement branch. Precondition: no PR in any state is
    headed at the branch, and no PR numbered above the persisted watermark
    carries the transaction's marker. Payload: title and body. Completion:
    exactly one PR carries the identity, and it is above the watermark, open,
    headed at the branch in this repository, based on the target base and
    titled and bodied as the payload (D4.5: the all-states listing filtered
    by head branch, and the all-states listing bounded by the watermark).
    ``_bind_replacement``'s other predicates (linked issue, sole
    implementation claimant, attestation) remain the consumer's (#164).
    """

    def __init__(self, github: GitHubClient) -> None:
        self.github = github

    def reconcile(self, record: EffectRecord) -> Decision:
        settled = _settled(record)
        if settled is not None:
            return settled
        repo = record.target["repository"]
        head = record.target["head"]
        watermark = record.precondition["watermark"]
        txn = record.owner.transaction_id
        on_head = self.github.list_prs_for_head(repo, head)
        above = [p for p in self.github.list_all_prs(repo, strict=True) if p.number > watermark]
        malformed: list[str] = []
        holders: list[PRInfo] = []
        for pr in above:
            markers = scan_replan_markers(pr.body)
            malformed.extend(f"PR {pr.url}: {reason}" for reason in markers.malformed)
            if any(a.transaction_id == txn for a in markers.attestations):
                holders.append(pr)
        if malformed:
            return _unreadable(record, f"PRs of {repo} above #{watermark}", malformed)
        on_head_numbers = {pr.number for pr in on_head}
        found = [
            Found(
                url=pr.url,
                in_end_state=(
                    pr.number in on_head_numbers
                    and pr.number > watermark
                    and pr.is_open
                    and pr.head_ref == head
                    and _same_repo_head(pr)
                    and pr.base_ref == record.target["base"]
                    and pr.title == record.payload["title"]
                    and pr.body == record.body
                ),
                observed={"url": pr.url, "number": pr.number},
            )
            for pr in _unique([*on_head, *holders])
        ]
        return decide_create(record, found)

    def issue(self, record: EffectRecord) -> None:
        self.github.create_pull_request(
            record.target["repository"],
            base=record.target["base"],
            head=record.target["head"],
            title=record.payload["title"],
            body=record.body,
        )


# -- K8: the EPIC progress comment ------------------------------------------------


class ProgressCommentOp:
    """K8 (ADR 0004 §2.5): the controller-posted progress comment on the EPIC.

    Identity: the ``ai-epic-progress`` marker for (issue, PR) on the EPIC.
    Precondition: no comment of the EPIC carries it. Payload: the progress
    text followed by the marker. Completion: exactly one comment carries the
    marker, and its body equals the payload (D4.5: the complete comment
    listing of the EPIC, never the search API).
    """

    def __init__(self, github: GitHubClient) -> None:
        self.github = github

    def reconcile(self, record: EffectRecord) -> Decision:
        if record.stage in (Stage.OBSERVED, Stage.CONFLICT):
            return decide_create(record, ())
        epic = record.target["epic_url"]
        claim = scan(PROGRESS, record.marker).claims[0]
        comments = self.github.get_issue_comments(epic)
        claimants = collect(PROGRESS, comments, "comment").claimants(
            claim.key, f"issue {claim.issue.canonical} (PR {claim.pr.canonical}) on EPIC {epic}"
        )
        if claimants.defects:
            return Decision(
                Action.CONFLICT,
                reason=(
                    f"{record.describe()}: the EPIC's comments cannot be read unambiguously "
                    f"({'; '.join(claimants.defects)})"
                ),
            )
        found = [
            Found(
                url=h.obj.url,
                in_end_state=h.obj.body == record.body,
                observed={"url": h.obj.url},
            )
            for h in claimants.holders
        ]
        return decide_create(record, found)

    def issue(self, record: EffectRecord) -> None:
        self.github.create_issue_comment(record.target["epic_url"], record.body)


_GITHUB_OPS: dict[EffectKind, Callable[[GitHubClient], EffectOperation]] = {
    EffectKind.IMPLEMENTATION_PR: ImplementationPrOp,
    EffectKind.ADOPT_PR: AdoptPrOp,
    EffectKind.REVIEW_COMMENT: ReviewCommentOp,
    EffectKind.FOLLOW_UP_ISSUE: FollowUpIssueOp,
    EffectKind.FOLLOW_UP_APPEND: FollowUpAppendOp,
    EffectKind.REPLACEMENT_PR: ReplacementPrOp,
    EffectKind.PROGRESS_COMMENT: ProgressCommentOp,
}


def operation_for(
    record: EffectRecord,
    github: GitHubClient,
    *,
    transport: GitTransport | None = None,
    default_branch: str = "",
) -> EffectOperation:
    """The operation of ``record``'s kind.

    A push (K1) also needs the controller's git transport and the default
    branch it must never push to; without them it has no operation, which
    is a conflict naming the record rather than a push through anything
    else. Every other kind needs the GitHub client only.
    """
    if record.kind is EffectKind.PUSH:
        if transport is None or not default_branch:
            raise EffectConflictError(
                f"{record.describe()}: a push needs the controller's git transport and the "
                "default branch, and neither may be left out"
            )
        return PushOp(github, transport, default_branch)
    return _GITHUB_OPS[record.kind](github)


__all__ = [
    "AdoptPrOp",
    "Driven",
    "EffectOperation",
    "EffectPending",
    "FollowUpAppendOp",
    "FollowUpIssueOp",
    "ImplementationPrOp",
    "ProgressCommentOp",
    "PushOp",
    "PushRefusedError",
    "ReplacementPrOp",
    "ReviewCommentOp",
    "drive",
    "operation_for",
    "raise_conflict",
]
