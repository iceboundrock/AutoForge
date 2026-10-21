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

Every kind of the closed set (ADR 0004 D5.1) has one operation here:
:class:`PushOp` (K1), :class:`ImplementationPrOp` (K2), :class:`AdoptPrOp`
(K3), :class:`ReviewCommentOp` (K4), :class:`FollowUpIssueOp` (K5),
:class:`FollowUpAppendOp` (K6), :class:`ReplacementPrOp` (K7),
:class:`ProgressCommentOp` (K8), :class:`AcceptanceCommentOp` (K9) and
:class:`OperatorCommentOp` (K10). A K9
write may also be *held* (``Action.HOLD``, #211): nothing is sent until the
operator approves it, nothing is sent or adopted while the run's issue does
not qualify, and :func:`drive` returns the record unchanged with the
reason, also when the issue stopped qualifying between the write and its
read-back.

K10 (#243) is **read-only**: the operator performs the comment and the
controller only verifies it. :class:`OperatorCommentOp` reads the target's
complete comment listing and the target itself; its ``issue`` raises
:class:`OperatorPerformedError` naming the record, :func:`drive` refuses an
operator-performed record before anything is persisted or sent, and
:func:`verify` is the one way such a record changes stage (``observed`` or
``conflict``, saved as made). Each ``reconcile`` is a complete identity
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

from .claims import (
    ACCEPTANCE,
    FOLLOW_UP,
    IMPLEMENTATION,
    OPERATOR_COMMENT,
    PROGRESS,
    REVIEW,
    MarkerKind,
    collect,
    scan,
)
from .effects import (
    MAX_BODY_CHARS,
    Action,
    Decision,
    EffectKind,
    EffectRecord,
    Found,
    Listed,
    Stage,
    decide_acceptance,
    decide_append,
    decide_create,
    decide_operator_comment,
    decide_push,
)
from .errors import (
    EffectConflictError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
    GitHubWrongTargetError,
    GitTransportError,
    StateError,
)
from .git_transport import GitTransport, PushOutcome
from .github import GitHubClient, IssueInfo, PRInfo
from .redaction import credential_classes, redact
from .replan_txn import scan_replan_markers
from .validation import parse_issue_url, parse_pr_url, same_issue_url

Persist = Callable[[EffectRecord], None]


class EffectOperation(Protocol):
    """One kind's reads and write. ``reconcile`` reads only; ``issue`` writes once."""

    def reconcile(self, record: EffectRecord) -> Decision: ...

    def issue(self, record: EffectRecord) -> None: ...


class OperatorPerformedError(StateError):
    """A K10 record reached a write path: the controller never issues it (#243)."""


class EffectPending(GitHubUnavailableError):
    """The write was issued and its outcome is not yet readable; the record stays
    ``attempted`` and the next entry reconciles it (transient, never re-sent blind)."""


@dataclass(frozen=True)
class Driven:
    """A record as last persisted, and what happened to it in this call."""

    record: EffectRecord
    issued: bool  # a write was sent in this call
    # K9: why the due write was not sent, or its comment not adopted (the
    # record is unchanged from its last save)
    held: str = ""
    held_url: str = ""  # K9: the exact comment a hold did not adopt, "" when none


def drive(record: EffectRecord, op: EffectOperation, persist: Persist) -> Driven:
    """Reconcile ``record`` and issue its write at most once (D4.1-D4.3).

    Returns the record ``observed`` or ``conflict``, or (K9) the record
    unchanged with :attr:`Driven.held` set when its write is due but may
    not be sent yet: nothing is persisted or sent then. A K9 whose issue
    stops qualifying between the write and its read-back is held the same
    way, ``attempted`` as persisted before the write and with
    :attr:`Driven.issued` set (PR #221 R2-F1). Raises, with the record
    left as persisted, when the outcome cannot be read yet:
    :class:`GitHubUnavailableError` from a reconciliation read (nothing was
    persisted or charged), or :class:`EffectPending` after an issue whose
    read-back did not show it. A conclusive read failure
    (:class:`GitHubError`) propagates unchanged for the caller to block on.

    An operator-performed record (K10, #243) is refused before any read,
    persist or write: :func:`verify` is its one path.
    """
    if record.operator_performed:
        raise OperatorPerformedError(
            f"{record.describe()} ({record.action_id}) is operator-performed; the controller "
            "never issues it, so it is verified, never driven"
        )
    decision = op.reconcile(record)
    if decision.action is Action.DONE:
        return Driven(record, issued=False)
    if decision.action is Action.HOLD:
        url = str((decision.observed or {}).get("url", ""))
        return Driven(record, issued=False, held=decision.reason, held_url=url)
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
    held_url = str((after.observed or {}).get("url", ""))
    if after.action is Action.HOLD and (held_url or refused is None):
        # K9 (PR #221 R2-F1): the run's issue stopped qualifying between the
        # write and its read-back. That is conclusive, not a write still
        # propagating: the record stays attempted (it was sent) and the
        # hold, naming the comment if it is there, goes to the caller.
        return Driven(record, issued=True, held=after.reason, held_url=held_url)
    if refused is not None:
        # The read-back's own conflict, or hold, names the object the write
        # collided with or why its target no longer qualifies.
        found = (
            after.reason
            if after.action is Action.HOLD
            or (after.action is Action.CONFLICT and not after.exhausted)
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


def verify(record: EffectRecord, op: EffectOperation, persist: Persist) -> Driven:
    """Reconcile an operator-performed record (K10, #243); never issue anything.

    ``observed`` and ``conflict`` are persisted as made; a hold returns the
    record unchanged with :attr:`Driven.held`. A decision to issue is
    impossible for the kind and is refused as a controller bug. Raises
    :class:`GitHubUnavailableError` with nothing persisted when the target
    cannot be read now.
    """
    if not record.operator_performed:
        raise OperatorPerformedError(f"{record.describe()} is not operator-performed")
    decision = op.reconcile(record)
    if decision.action is Action.DONE:
        return Driven(record, issued=False)
    if decision.action is Action.HOLD:
        return Driven(record, issued=False, held=decision.reason)
    if decision.action is Action.OBSERVE:
        record = record.observing(decision.observed or {})
        persist(record)
        return Driven(record, issued=False)
    if decision.action is Action.CONFLICT:
        if record.stage is not Stage.CONFLICT:
            record = record.conflicting(decision.reason)
            persist(record)
        return Driven(record, issued=False)
    raise OperatorPerformedError(
        f"{record.describe()} ({record.action_id}): a verification decided to "
        f"{decision.action.value}, which an operator-performed record never does"
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


def append_problem(what: str, kind: MarkerKind, payload: str) -> str:
    """D5.5: why a composed append payload may not be persisted, or "".

    The same rules the record's payload is validated with on save (size, no
    NUL or DEL, redaction-invariant), checked first so a rebase (or a plan,
    #161) that fails them is a ``conflict`` or a block instead of a save
    error, plus the marker kind's own scan: the composed body must leave
    every marker of ``kind`` readable. The answer names ``what`` and the
    pattern class, never the matched text or the body.
    """
    if len(payload) > MAX_BODY_CHARS:
        return (
            f"the composed body of {what} would be {len(payload)} characters, over the "
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
            f"the composed body of {what} would carry a malformed or repeated {kind.name} "
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
            rebase_problem=lambda payload: append_problem(pr_url, IMPLEMENTATION, payload),
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
            rebase_problem=lambda payload: append_problem(issue_url, FOLLOW_UP, payload),
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


class AcceptanceCommentOp:
    """K9: the acceptance comment of one finding on the run's own issue (#211).

    Identity: the ``ai-acceptance`` marker for (issue, PR, round, finding)
    on the issue. Precondition: no comment of the issue carries it, the
    issue is open and in the repository of the run's PR, and the write is
    authorized (an operator approval of the record's action id, or the
    policy). Payload: the controller's header, the agent's text and the
    marker. Completion: exactly one comment carries the marker, and its
    body equals the payload (D4.5: the complete comment listing of the
    issue, never the search API). Whoever posted it is irrelevant: an
    operator who posts the exact payload has done the write. It is adopted
    only while the issue still qualifies (PR #221 R1-F2): on an issue that
    was closed or left the run's repository it is held, as the write is.
    """

    def __init__(self, github: GitHubClient) -> None:
        self.github = github

    def reconcile(self, record: EffectRecord) -> Decision:
        if record.stage in (Stage.OBSERVED, Stage.CONFLICT):
            return decide_create(record, ())
        issue_url = record.target["issue_url"]
        claim = scan(ACCEPTANCE, record.marker).claims[0]
        comments = self.github.get_issue_comments(issue_url)
        claimants = collect(ACCEPTANCE, comments, "comment").claimants(
            claim.key,
            f"finding {claim.finding_id} of round {claim.round} of PR {claim.pr.canonical} "
            f"on issue {issue_url}",
        )
        if claimants.defects:
            return Decision(
                Action.CONFLICT,
                reason=(
                    f"{record.describe()}: the issue's comments cannot be read unambiguously "
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
        problem = self._issue_problem(issue_url, record.owner.pr_url)
        return decide_acceptance(record, found, issue_problem=problem)

    def _issue_problem(self, issue_url: str, pr_url: str) -> str:
        """Why the run's issue may not receive the comment now, or ""."""
        issue = self.github.get_issue(issue_url)
        repository = parse_pr_url(pr_url).repository
        if not _same_repository(
            issue.repository or parse_issue_url(issue_url).repository, repository
        ):
            return f"issue {issue_url} is not in the run's repository {repository}"
        if not issue.is_open:
            return f"issue {issue_url} is {issue.state.lower() or 'not open'}, not open"
        return ""

    def issue(self, record: EffectRecord) -> None:
        self.github.create_issue_comment(record.target["issue_url"], record.body)


class OperatorCommentOp:
    """K10: one operator comment on another issue of the run's repository (#243). Read-only.

    Identity: the ``ai-operator-comment`` marker for (run issue, PR, round,
    finding, target) on the target. The operator posts it; the controller
    never does. Completion: exactly one comment of the target's complete
    listing carries the marker and its body equals the payload (``exact``),
    or the comment the operator bound is in the listing with its digest
    unchanged (``bound``). ``plan`` is the FIX plan's records: a marker of
    another K10 of the plan, meant for another issue, found on this target
    is a misplaced payload and a conflict. The target is read too: it must
    still be an issue of the run's repository (open or closed).
    """

    def __init__(self, github: GitHubClient, plan: Sequence[EffectRecord] = ()) -> None:
        self.github = github
        self.plan = tuple(plan)

    def reconcile(self, record: EffectRecord) -> Decision:
        if record.stage in (Stage.OBSERVED, Stage.CONFLICT):
            return decide_operator_comment(record, own=(), misplaced=(), listing=())
        target_url = record.target["issue_url"]
        problem = self.target_problem(target_url, record.owner.issue_url)
        if problem:
            return decide_operator_comment(
                record, own=(), misplaced=(), listing=(), target_problem=problem
            )
        return self.reconcile_listing(record)

    def reconcile_listing(self, record: EffectRecord) -> Decision:
        """:meth:`reconcile` from the target's complete listing alone (#243).

        For a caller that has read the target itself
        (:meth:`target_disqualification`). Any failure to read the listing
        propagates.
        """
        if record.stage in (Stage.OBSERVED, Stage.CONFLICT):
            return decide_operator_comment(record, own=(), misplaced=(), listing=())
        target_url = record.target["issue_url"]
        comments = self.github.get_issue_comments(target_url)
        listing = [Listed(url=c.url, comment_id=c.id, body=c.body) for c in comments]
        collection = collect(OPERATOR_COMMENT, comments, "comment")
        key = scan(OPERATOR_COMMENT, record.marker).claims[0].key
        others = {
            scan(OPERATOR_COMMENT, r.marker).claims[0].key
            for r in self.plan
            if r.kind is EffectKind.OPERATOR_COMMENT
            and r.position != record.position
            and not same_issue_url(r.target["issue_url"], target_url)
        }
        own = [
            Listed(url=h.obj.url, comment_id=h.obj.id, body=h.obj.body)
            for h in collection.holders
            if h.claim.key == key
        ]
        misplaced = [
            Listed(url=h.obj.url, comment_id=h.obj.id, body=h.obj.body)
            for h in collection.holders
            if h.claim.key in others
        ]
        return decide_operator_comment(
            record,
            own=own,
            misplaced=misplaced,
            listing=listing,
            defects=collection.defects,
        )

    def target_problem(self, target_url: str, issue_url: str) -> str:
        """Why the target no longer qualifies (transferred, deleted, a PR), or "".

        A transient failure propagates; every conclusive one is a reason.
        """
        try:
            return self.target_disqualification(target_url, issue_url)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return f"issue {target_url} cannot be read ({redact(str(exc))})"

    def target_disqualification(self, target_url: str, issue_url: str) -> str:
        """:meth:`target_problem` from an answer GitHub gave, or "" (#243).

        Not found, another object or another repository is a reason. Any
        other conclusive failure propagates, like a transient one: an
        ``unblock`` refuses on a target it cannot read rather than holding
        it (PR #254 R1-F1).
        """
        try:
            issue = self.github.get_issue(target_url)
        except GitHubNotFoundError:
            return f"issue {target_url} is not found (deleted, or not readable with this token)"
        except GitHubWrongTargetError as exc:
            what = "a pull request" if exc.is_pull_request else "another object"
            return f"issue {target_url} answers as {what} ({exc.answered_url})"
        repository = parse_issue_url(issue_url).repository
        if not _same_repository(issue.repository or repository, repository):
            return f"issue {target_url} is no longer in the run's repository {repository}"
        return ""

    def issue(self, record: EffectRecord) -> None:
        raise OperatorPerformedError(
            f"{record.describe()} ({record.action_id}) is operator-performed; the controller "
            "never posts it"
        )


_GITHUB_OPS: dict[EffectKind, Callable[[GitHubClient], EffectOperation]] = {
    EffectKind.IMPLEMENTATION_PR: ImplementationPrOp,
    EffectKind.ADOPT_PR: AdoptPrOp,
    EffectKind.REVIEW_COMMENT: ReviewCommentOp,
    EffectKind.FOLLOW_UP_ISSUE: FollowUpIssueOp,
    EffectKind.FOLLOW_UP_APPEND: FollowUpAppendOp,
    EffectKind.REPLACEMENT_PR: ReplacementPrOp,
    EffectKind.PROGRESS_COMMENT: ProgressCommentOp,
    EffectKind.ACCEPTANCE_COMMENT: AcceptanceCommentOp,
    # Read-only: its ``issue`` raises (#243).
    EffectKind.OPERATOR_COMMENT: OperatorCommentOp,
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
    "AcceptanceCommentOp",
    "AdoptPrOp",
    "Driven",
    "EffectOperation",
    "EffectPending",
    "FollowUpAppendOp",
    "FollowUpIssueOp",
    "ImplementationPrOp",
    "OperatorCommentOp",
    "OperatorPerformedError",
    "ProgressCommentOp",
    "PushOp",
    "PushRefusedError",
    "ReplacementPrOp",
    "ReviewCommentOp",
    "drive",
    "operation_for",
    "raise_conflict",
    "verify",
]
