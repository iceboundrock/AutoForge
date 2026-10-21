"""#160: controller-owned external effects (ADR 0004 D2, D4, D5).

Three layers are pinned here:

- the persisted record (``autoforge.effects``): it round-trips, and every
  corrupt or incomplete shape fails loudly with ``StateError`` and is never
  defaulted, including the plan and field bounds;
- the operation of each kind (``autoforge.effect_ops``), driven through
  :func:`drive` against the in-memory ``FakeGitHub`` (and, for K1, a local
  bare repository): the attempt is saved before the write, an object already
  on GitHub is adopted, every identity collision is a conflict naming the
  object, an ambiguous write is reconciled before anything is re-sent, and
  the attempt bound holds;
- the closed-object identity reads of the create kinds: an object the
  controller created and a human closed before the save is a conflict, never
  a reason for a second create.

Nothing touches the network or a real GitHub: the git remote is a bare
repository under ``tmp_path`` reached over ``file://``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from autoforge.claims import (
    REVIEW,
    render_acceptance_marker,
    render_follow_up_marker,
    render_implementation_marker,
    render_operator_comment_marker,
    render_progress_marker,
)
from autoforge.effect_ops import (
    AcceptanceCommentOp,
    AdoptPrOp,
    EffectPending,
    FollowUpAppendOp,
    FollowUpIssueOp,
    ImplementationPrOp,
    OperatorCommentOp,
    OperatorPerformedError,
    ProgressCommentOp,
    PushOp,
    ReplacementPrOp,
    ReviewCommentOp,
    drive,
    operation_for,
    raise_conflict,
    verify,
)
from autoforge.effects import (
    ACTION_ID_RE,
    APPEND_SEPARATOR,
    MAX_BODY_CHARS,
    MAX_CONFLICT_REASON_CHARS,
    MAX_EFFECT_ATTEMPTS,
    MAX_EFFECT_STATE_CHARS,
    MAX_EFFECTS_PER_PLAN,
    MAX_OPERATOR_TARGETS,
    MAX_TITLE_CHARS,
    OPERATOR_ACTION_ID_RE,
    Action,
    AnalyzeContext,
    Binding,
    Decision,
    EffectKind,
    EffectOwner,
    EffectRecord,
    EntryObservation,
    FixContext,
    Found,
    Listed,
    OperatorTargets,
    ReplanContext,
    Stage,
    acceptance_action_id,
    acceptance_comment_body,
    compose_append,
    decide_acceptance,
    decide_operator_comment,
    follow_up_issue_body,
    implementation_closing_block,
    load_phase_effects,
    load_records,
    normalized_comment_text,
    operator_action_id,
    operator_comment_body,
    payload_chars,
    plan_size_problem,
    progress_comment_body,
    replacement_pr_body,
    sha256_text,
)
from autoforge.errors import (
    EffectConflictError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
    GitHubWrongTargetError,
    GitTransportError,
    StateError,
)
from autoforge.executor import ExecutionRequest, ExecutionResult, execute
from autoforge.git_transport import GitRemote, GitTransport
from autoforge.github import GitHubClient
from autoforge.locking import SharedGitLock
from autoforge.replan_txn import ReplanAttestation, render_marker
from autoforge.result_parser import (
    MAX_FINDINGS_PER_REVIEW,
    MAX_FOLLOW_UP_BODY_CHARS,
    MAX_OPERATOR_ACTIONS_PER_FIX,
    MAX_OPERATOR_COMMENT_CHARS,
    MAX_PR_BODY_CHARS,
)
from autoforge.transitions import Phase
from tests.conftest import (
    BRANCH,
    EPIC,
    ISSUE,
    ISSUE3,
    PR,
    SHA_A,
    SHA_B,
    SHA_C,
    FakeGitHub,
    comment_url,
)

REPO = "owner/repo"
RUN_ID = "run-160"
TXN = "0123456789abcdef0123456789abcdef"
PR43 = "https://github.com/owner/repo/pull/43"
PR44 = "https://github.com/owner/repo/pull/44"
ISSUE4 = "https://github.com/owner/repo/issues/4"
ISSUE43 = "https://github.com/owner/repo/issues/43"
ISSUE44 = "https://github.com/owner/repo/issues/44"
REPLACEMENT_BRANCH = "autoforge/2-replan-1"
SECRET = "supersecretvalue123"
CREDENTIAL_TEXT = f"GH_TOKEN={SECRET}"

IMPL_MARKER = render_implementation_marker(ISSUE)
CLOSING = f"Closes #2\n\n{IMPL_MARKER}"
FOLLOW_UP_MARKER = render_follow_up_marker(PR, "R1-F1")
PROGRESS_MARKER = render_progress_marker(ISSUE, PR)
REVIEW_PAYLOAD: dict[str, object] = {
    "round": 1,
    "reviewed_head_sha": SHA_A,
    "needs_fix_round": False,
    "finding_ids": [],
    "reviewed_base_ref": "main",
    "reviewed_merge_base_sha": SHA_C,
}
REVIEW_MARKER = REVIEW.render(REVIEW_PAYLOAD)
REPLAN_MARKER = render_marker(ReplanAttestation(TXN, 1, 2, 1, True))
FAILED_REPLAN_MARKER = render_marker(ReplanAttestation(TXN, 1, 2, 1, False))
ADOPT_BASE = "A human's PR.\n\nIt adds the transaction filter."
APPEND_BASE = "An older follow-up issue.\n\nStill open."


# -- owners and record builders ---------------------------------------------------------


def owner(
    phase: Phase,
    *,
    pr_url: str = "",
    txn: str = "",
    run_id: str = RUN_ID,
    issue_url: str = ISSUE,
) -> EffectOwner:
    return EffectOwner(run_id, phase, issue_url, pr_url, txn)


ANALYZE_OWNER = owner(Phase.ANALYZE_EXECUTE)
FIX_OWNER = owner(Phase.FIX, pr_url=PR)
REPLAN_OWNER = owner(Phase.REPLAN_REEXECUTE, pr_url=PR, txn=TXN)
EPIC_OWNER = owner(Phase.UPDATE_EPIC, pr_url=PR)


def push_record(
    *,
    candidate: str = SHA_B,
    expected_old: str | None = SHA_A,
    base: str = SHA_C,
    branch: str = BRANCH,
    position: int = 0,
    by: EffectOwner = FIX_OWNER,
) -> EffectRecord:
    ref = f"refs/heads/{branch}"
    return EffectRecord.plan(
        position,
        EffectKind.PUSH,
        by,
        identity={"repository": REPO, "ref": ref, "candidate_sha": candidate},
        target={"repository": REPO, "ref": ref},
        precondition={"expected_old": expected_old, "base_sha": base},
        payload={"sha": candidate},
    )


def impl_record(
    *,
    title: str = "Implement #2",
    body: str | None = None,
    head: str = BRANCH,
    base: str = "main",
    by: EffectOwner = ANALYZE_OWNER,
) -> EffectRecord:
    return EffectRecord.plan(
        1,
        EffectKind.IMPLEMENTATION_PR,
        by,
        identity={"repository": REPO, "marker": IMPL_MARKER, "head_branch": head},
        target={"repository": REPO, "base": base, "head": head},
        precondition={"absent": True},
        payload={
            "title": title,
            "body": f"Implements {ISSUE}.\n\n{CLOSING}" if body is None else body,
        },
    )


def adopt_record(base: str = ADOPT_BASE) -> EffectRecord:
    block = CLOSING
    return EffectRecord.plan(
        1,
        EffectKind.ADOPT_PR,
        ANALYZE_OWNER,
        identity={"pr_url": PR, "marker": IMPL_MARKER},
        target={"pr_url": PR},
        precondition={"base_sha256": sha256_text(base)},
        payload={"body": compose_append(base, block), "block": block},
    )


def follow_up_record(
    *,
    finding_id: str = "R1-F1",
    watermark: int = 42,
    position: int = 1,
    body: str | None = None,
    by: EffectOwner = FIX_OWNER,
) -> EffectRecord:
    marker = render_follow_up_marker(PR, finding_id)
    return EffectRecord.plan(
        position,
        EffectKind.FOLLOW_UP_ISSUE,
        by,
        identity={"repository": REPO, "marker": marker},
        target={"repository": REPO},
        precondition={"absent": True, "watermark": watermark},
        payload={
            "title": f"Follow-up: {finding_id} of PR #42",
            "body": (
                follow_up_issue_body(f"The deferred part of {finding_id}.", PR, finding_id)
                if body is None
                else body
            ),
        },
    )


def append_record(
    *,
    base: str = APPEND_BASE,
    finding_ids: tuple[str, ...] = ("R1-F1",),
    target: str = ISSUE3,
) -> EffectRecord:
    markers = [render_follow_up_marker(PR, fid) for fid in finding_ids]
    block = "\n".join(markers)
    return EffectRecord.plan(
        1,
        EffectKind.FOLLOW_UP_APPEND,
        FIX_OWNER,
        identity={"issue_url": target, "markers": markers},
        target={"issue_url": target},
        precondition={"base_sha256": sha256_text(base)},
        payload={"body": compose_append(base, block), "block": block},
    )


def replacement_record(
    *, watermark: int = 42, head: str = REPLACEMENT_BRANCH, position: int = 1
) -> EffectRecord:
    return EffectRecord.plan(
        position,
        EffectKind.REPLACEMENT_PR,
        REPLAN_OWNER,
        identity={"repository": REPO, "transaction_marker": REPLAN_MARKER, "head_branch": head},
        target={"repository": REPO, "base": "main", "head": head},
        precondition={"absent": True, "watermark": watermark},
        payload={
            "title": "Replace PR #42 for #2",
            "body": replacement_pr_body(f"Replaces {PR} for {ISSUE}.", ISSUE, REPLAN_MARKER),
        },
    )


def review_record() -> EffectRecord:
    return EffectRecord.plan(
        0,
        EffectKind.REVIEW_COMMENT,
        owner(Phase.REVIEW, pr_url=PR),
        identity={"pr_url": PR, "marker": REVIEW_MARKER},
        target={"pr_url": PR},
        precondition={"absent": True},
        payload={"body": f"## Review round 1\n\nNo findings.\n\n{REVIEW_MARKER}"},
    )


def progress_record() -> EffectRecord:
    return EffectRecord.plan(
        0,
        EffectKind.PROGRESS_COMMENT,
        EPIC_OWNER,
        identity={"epic_url": EPIC, "marker": PROGRESS_MARKER},
        target={"epic_url": EPIC},
        precondition={"absent": True},
        payload={
            "body": progress_comment_body("Issue #2 is done; PR #42 merged.", PROGRESS_MARKER)
        },
    )


ACCEPTANCE_MARKER = render_acceptance_marker(ISSUE, PR, 1, "R1-F1")
ACCEPTANCE_TEXT = "Ran the operator check on the reviewed HEAD; every check passed."


def acceptance_record(
    *,
    authorization: str = "policy",
    text: str = ACCEPTANCE_TEXT,
    position: int = 0,
    by: EffectOwner = FIX_OWNER,
) -> EffectRecord:
    """K9 (#211): authorized by policy (its own id approved), or awaiting the operator."""
    identity = {"issue_url": ISSUE, "marker": ACCEPTANCE_MARKER}
    target = {"issue_url": ISSUE}
    payload = {"body": acceptance_comment_body(text, ISSUE, PR, 1, "R1-F1")}
    approved = (
        acceptance_action_id(by, identity, target, payload) if authorization == "policy" else ""
    )
    return EffectRecord.plan(
        position,
        EffectKind.ACCEPTANCE_COMMENT,
        by,
        identity=identity,
        target=target,
        precondition={
            "absent": True,
            "authorization": authorization,
            "approved_action_id": approved,
        },
        payload=payload,
    )


# -- the persist callback and GitHub stand-ins -----------------------------------------


class Crash(Exception):
    """The process died in the middle of a save: that save never happened."""


@dataclass
class Saves:
    """The ``persist`` callback: every save, with the write count at that moment.

    Every saved record is round-tripped through JSON and ``from_dict``,
    because that is what recovery reads. ``crash_on`` kills the save of a
    record of that stage (it is not logged); ``hook`` runs after a save.
    """

    writes: Callable[[], int]
    hook: Callable[[EffectRecord], None] | None = None
    crash_on: Stage | None = None
    log: list[tuple[EffectRecord, int]] = field(default_factory=list)

    def __call__(self, record: EffectRecord) -> None:
        if record.stage is self.crash_on:
            raise Crash(record.stage.value)
        assert EffectRecord.from_dict(json.loads(json.dumps(record.to_dict()))) == record
        self.log.append((record, self.writes()))
        if self.hook is not None:
            self.hook(record)

    @property
    def last(self) -> EffectRecord:
        return self.log[-1][0]

    @property
    def stages(self) -> list[tuple[Stage, int, int]]:
        """(stage, attempts, writes already sent when it was saved) per save."""
        return [(r.stage, r.attempts, n) for r, n in self.log]


def saves_for(fake: FakeGitHub, **kwargs: Any) -> Saves:
    return Saves(lambda: len(fake.effect_writes), **kwargs)


class Outage:
    """``FakeGitHub`` behind a switch: while ``down``, every read is unavailable.

    The writes still reach the fake (``write_failures`` decides their fate),
    so a write can land while its read-back is lost.
    """

    WRITES = frozenset(
        {
            "create_pull_request",
            "write_pr_body",
            "create_issue",
            "write_issue_body",
            "create_issue_comment",
            "create_pr_comment",
        }
    )

    def __init__(self, fake: FakeGitHub, *, down: bool = False) -> None:
        self.fake = fake
        self.down = down

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.fake, name)
        if name in self.WRITES or not callable(attr):
            return attr

        def read(*args: Any, **kwargs: Any) -> Any:
            if self.down:
                raise GitHubUnavailableError(f"{name}: connection reset")
            return attr(*args, **kwargs)

        return read


# -- the GitHub kinds (K2, K3, K5, K6, K7, K8) -----------------------------------------


class Case:
    """One GitHub effect kind: how the fake is prepared, and what its objects are."""

    name: str
    write: str  # the FakeGitHub write method its operation calls
    op_type: type

    def setup(self, fake: FakeGitHub) -> EffectRecord:
        raise NotImplementedError

    def landed(self, fake: FakeGitHub, record: EffectRecord) -> list[str]:
        """Every object holding ``record``'s payload."""
        raise NotImplementedError

    def plant(self, fake: FakeGitHub, record: EffectRecord) -> str:
        """The intended end state, as a landed write would have left it; its URL."""
        raise NotImplementedError

    def imposter(self, fake: FakeGitHub, record: EffectRecord) -> str:
        """An object with the identity that is not the intended one; the URL named."""
        raise NotImplementedError

    def second_holder(self, fake: FakeGitHub, record: EffectRecord) -> str:
        """Another object carrying the identity; its URL."""
        raise NotImplementedError

    def human_edit(self, fake: FakeGitHub, record: EffectRecord) -> str:
        """A human's change after a lost write; the URL the conflict names."""
        return self.imposter(fake, record)

    def __repr__(self) -> str:
        return self.name


class ImplementationPrCase(Case):
    name, write, op_type = "K2-implementation-pr", "create_pull_request", ImplementationPrOp

    def setup(self, fake: FakeGitHub) -> EffectRecord:
        fake.branch_heads[BRANCH] = SHA_A
        return impl_record()

    def landed(self, fake: FakeGitHub, record: EffectRecord) -> list[str]:
        return [p.url for p in fake.prs.values() if p.head_ref == BRANCH and p.body == record.body]

    def plant(self, fake: FakeGitHub, record: EffectRecord) -> str:
        info = fake.add_pr(PR43, head_sha=SHA_A, branch=BRANCH, body=record.body)
        info.title = record.payload["title"]
        return PR43

    def imposter(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.add_pr(PR43, head_sha=SHA_A, branch=BRANCH, body=record.body)  # title "PR"
        return PR43

    def second_holder(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.add_pr(PR44, head_sha=SHA_B, branch="human/other", body=record.body)
        return PR44

    def close(self, fake: FakeGitHub, url: str) -> None:
        fake.prs[url].state = "CLOSED"
        fake.add_close_event(url)


class AdoptPrCase(Case):
    name, write, op_type = "K3-adopt-pr", "write_pr_body", AdoptPrOp

    def setup(self, fake: FakeGitHub) -> EffectRecord:
        fake.add_pr(PR, head_sha=SHA_A, branch="human/feature", body=ADOPT_BASE)
        return adopt_record()

    def landed(self, fake: FakeGitHub, record: EffectRecord) -> list[str]:
        return [PR] if fake.prs[PR].body == record.body else []

    def plant(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.prs[PR].body = record.body
        return PR

    def imposter(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.prs[PR].body = compose_append(ADOPT_BASE, render_implementation_marker(ISSUE3))
        return PR

    def second_holder(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.add_pr(PR43, head_sha=SHA_B, branch="human/other", body=f"Closes #2\n\n{IMPL_MARKER}")
        return PR43

    def human_edit(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.prs[PR].body = "A human rewrote the description."
        return PR

    def close_target(self, fake: FakeGitHub) -> None:
        fake.prs[PR].state = "CLOSED"

    def edit_target(self, fake: FakeGitHub, body: str) -> None:
        fake.prs[PR].body = body


class FollowUpIssueCase(Case):
    name, write, op_type = "K5-follow-up-issue", "create_issue", FollowUpIssueOp

    def setup(self, fake: FakeGitHub) -> EffectRecord:
        fake.add_pr(PR)
        return follow_up_record(watermark=fake.latest_issue_number(REPO))

    def landed(self, fake: FakeGitHub, record: EffectRecord) -> list[str]:
        return [i.url for i in fake.issues.values() if i.body == record.body]

    def plant(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.add_issue(ISSUE43, title=record.payload["title"], body=record.body)
        return ISSUE43

    def imposter(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.add_issue(ISSUE43, title="Another title", body=record.body)
        return ISSUE43

    def second_holder(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.add_issue(ISSUE44, title=record.payload["title"], body=record.body)
        return ISSUE44

    def close(self, fake: FakeGitHub, url: str) -> None:
        fake.issues[url].state = "CLOSED"


class FollowUpAppendCase(Case):
    name, write, op_type = "K6-follow-up-append", "write_issue_body", FollowUpAppendOp

    def setup(self, fake: FakeGitHub) -> EffectRecord:
        fake.add_issue(ISSUE3, "Older follow-up", body=APPEND_BASE)
        return append_record()

    def landed(self, fake: FakeGitHub, record: EffectRecord) -> list[str]:
        return [ISSUE3] if fake.issues[ISSUE3].body == record.body else []

    def plant(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.issues[ISSUE3].body = record.body
        return ISSUE3

    def imposter(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.issues[ISSUE3].body = compose_append(APPEND_BASE, FOLLOW_UP_MARKER) + "\nedited"
        return ISSUE3

    def second_holder(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.add_issue(ISSUE4, "Another follow-up", body=f"Older.\n\n{FOLLOW_UP_MARKER}")
        return ISSUE4

    def human_edit(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.issues[ISSUE3].body = "A human rewrote the description."
        return ISSUE3

    def close_target(self, fake: FakeGitHub) -> None:
        fake.issues[ISSUE3].state = "CLOSED"

    def edit_target(self, fake: FakeGitHub, body: str) -> None:
        fake.issues[ISSUE3].body = body


class ReplacementPrCase(Case):
    name, write, op_type = "K7-replacement-pr", "create_pull_request", ReplacementPrOp

    def setup(self, fake: FakeGitHub) -> EffectRecord:
        fake.add_pr(PR)
        fake.branch_heads[REPLACEMENT_BRANCH] = SHA_B
        return replacement_record(watermark=fake.latest_pr_number(REPO))

    def landed(self, fake: FakeGitHub, record: EffectRecord) -> list[str]:
        return [
            p.url
            for p in fake.prs.values()
            if p.head_ref == REPLACEMENT_BRANCH and p.body == record.body
        ]

    def plant(self, fake: FakeGitHub, record: EffectRecord) -> str:
        info = fake.add_pr(PR43, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, body=record.body)
        info.title = record.payload["title"]
        return PR43

    def imposter(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.add_pr(PR43, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, body=record.body)
        return PR43

    def second_holder(self, fake: FakeGitHub, record: EffectRecord) -> str:
        fake.add_pr(PR44, head_sha=SHA_C, branch="human/other", body=record.body)
        return PR44

    def close(self, fake: FakeGitHub, url: str) -> None:
        fake.prs[url].state = "CLOSED"
        fake.add_close_event(url)


class ReviewCommentCase(Case):
    name, write, op_type = "K4-review-comment", "create_pr_comment", ReviewCommentOp

    def setup(self, fake: FakeGitHub) -> EffectRecord:
        fake.add_pr(PR)
        return review_record()

    def landed(self, fake: FakeGitHub, record: EffectRecord) -> list[str]:
        return [c.url for c in fake.comments.get(PR, []) if c.body == record.body]

    def plant(self, fake: FakeGitHub, record: EffectRecord) -> str:
        return fake.add_comment(PR, 300, record.body).url

    def imposter(self, fake: FakeGitHub, record: EffectRecord) -> str:
        return fake.add_comment(PR, 301, f"Another review.\n\n{REVIEW_MARKER}").url

    def second_holder(self, fake: FakeGitHub, record: EffectRecord) -> str:
        return fake.add_comment(PR, 302, f"A copy.\n\n{REVIEW_MARKER}").url


class ProgressCommentCase(Case):
    name, write, op_type = "K8-progress-comment", "create_issue_comment", ProgressCommentOp

    def setup(self, fake: FakeGitHub) -> EffectRecord:
        return progress_record()

    def landed(self, fake: FakeGitHub, record: EffectRecord) -> list[str]:
        return [c.url for c in fake.comments.get(EPIC, []) if c.body == record.body]

    def plant(self, fake: FakeGitHub, record: EffectRecord) -> str:
        return fake.add_comment(EPIC, 300, record.body).url

    def imposter(self, fake: FakeGitHub, record: EffectRecord) -> str:
        return fake.add_comment(EPIC, 301, f"Other progress.\n\n{PROGRESS_MARKER}").url

    def second_holder(self, fake: FakeGitHub, record: EffectRecord) -> str:
        return fake.add_comment(EPIC, 302, f"A copy.\n\n{PROGRESS_MARKER}").url


class AcceptanceCommentCase(Case):
    name, write, op_type = "K9-acceptance-comment", "create_issue_comment", AcceptanceCommentOp

    def setup(self, fake: FakeGitHub) -> EffectRecord:
        fake.add_issue(ISSUE, "Ship the feature")
        return acceptance_record()

    def landed(self, fake: FakeGitHub, record: EffectRecord) -> list[str]:
        return [c.url for c in fake.comments.get(ISSUE, []) if c.body == record.body]

    def plant(self, fake: FakeGitHub, record: EffectRecord) -> str:
        return fake.add_comment(ISSUE, 300, record.body).url

    def imposter(self, fake: FakeGitHub, record: EffectRecord) -> str:
        return fake.add_comment(ISSUE, 301, f"Other evidence.\n\n{ACCEPTANCE_MARKER}").url

    def second_holder(self, fake: FakeGitHub, record: EffectRecord) -> str:
        return fake.add_comment(ISSUE, 302, f"A copy.\n\n{ACCEPTANCE_MARKER}").url


K2, K3, K4, K5, K6, K7, K8, K9 = (
    ImplementationPrCase(),
    AdoptPrCase(),
    ReviewCommentCase(),
    FollowUpIssueCase(),
    FollowUpAppendCase(),
    ReplacementPrCase(),
    ProgressCommentCase(),
    AcceptanceCommentCase(),
)
CASES = [K2, K3, K4, K5, K6, K7, K8, K9]
CREATE_CASES = [K2, K4, K5, K7, K8, K9]
APPEND_CASES = [K3, K6]
CLOSABLE_CASES = [K2, K5, K7]
by_case = pytest.mark.parametrize("case", CASES, ids=repr)


def world_of(case: Case) -> tuple[FakeGitHub, EffectRecord]:
    fake = FakeGitHub()
    return fake, case.setup(fake)


# -- record schema: round trip -----------------------------------------------------------

OBSERVED: list[tuple[Callable[[], EffectRecord], dict]] = [
    (push_record, {"sha": SHA_B}),
    (impl_record, {"url": PR43, "number": 43}),
    (adopt_record, {"url": PR}),
    (follow_up_record, {"url": ISSUE43, "number": 43}),
    (append_record, {"url": ISSUE3}),
    (review_record, {"url": comment_url(PR, 300)}),
    (replacement_record, {"url": PR43, "number": 43}),
    (progress_record, {"url": comment_url(EPIC, 300)}),
    (acceptance_record, {"url": comment_url(ISSUE, 300)}),
]


@pytest.mark.parametrize(("build", "observed"), OBSERVED, ids=lambda v: getattr(v, "__name__", ""))
def test_every_stage_of_every_kind_round_trips_through_json(build, observed):
    """``to_dict``/``from_dict`` are inverse in each stage, through JSON text."""
    intended = build()
    attempted = intended.attempting()
    stages = [
        intended,
        attempted,
        attempted.attempting(),
        attempted.observing(observed),
        intended.observing(observed),
        attempted.conflicting("a human closed it"),
        intended.conflicting("a human closed it"),
    ]
    for record in stages:
        raw = json.loads(json.dumps(record.to_dict()))
        assert EffectRecord.from_dict(raw) == record
        assert EffectRecord.from_dict(raw).to_dict() == raw


def test_plan_sets_the_kind_completion_and_starts_intended():
    """A planned record is intended, uncharged, with its kind's completion criterion."""
    record = impl_record()
    assert (record.stage, record.attempts, record.observed, record.reason) == (
        Stage.INTENDED,
        0,
        None,
        "",
    )
    assert record.completion == "one_pr_on_head_branch_matching_payload"
    assert record.describe() == f"implementation_pr effect 1 on {REPO}"


# -- record schema: corruption fails loudly ---------------------------------------------

Mutation = Callable[[dict], None]


def at(path: str, value: object) -> Mutation:
    def mutate(raw: dict) -> None:
        *parents, leaf = path.split(".")
        node = raw
        for key in parents:
            node = node[key]
        node[leaf] = value

    return mutate


def without(path: str) -> Mutation:
    def mutate(raw: dict) -> None:
        *parents, leaf = path.split(".")
        node = raw
        for key in parents:
            node = node[key]
        del node[leaf]

    return mutate


def both(*mutations: Mutation) -> Mutation:
    def mutate(raw: dict) -> None:
        for m in mutations:
            m(raw)

    return mutate


def observed_as(value: object) -> Mutation:
    return both(at("stage", "observed"), at("observed", value))


def _dict(build: Callable[[], EffectRecord]) -> Callable[[], dict]:
    return lambda: build().to_dict()


IMPL, PUSH, ADOPT, REVIEWED, FOLLOW, APPEND, REPLACE, PROGRESS, ACCEPT = (
    _dict(impl_record),
    _dict(push_record),
    _dict(adopt_record),
    _dict(review_record),
    _dict(follow_up_record),
    _dict(append_record),
    _dict(replacement_record),
    _dict(progress_record),
    _dict(acceptance_record),
)
WAITING = _dict(lambda: acceptance_record(authorization="operator"))
PRE_BASE_REVIEW_MARKER = REVIEW.render(
    {k: v for k, v in REVIEW_PAYLOAD.items() if k != "reviewed_base_ref"}
)
PRE_MERGE_BASE_REVIEW_MARKER = REVIEW.render(
    {k: v for k, v in REVIEW_PAYLOAD.items() if k != "reviewed_merge_base_sha"}
)
OTHER_IMPL_MARKER = render_implementation_marker(ISSUE3)
OTHER_PR_FOLLOW_UP = render_follow_up_marker(PR43, "R1-F1")
OTHER_TXN_MARKER = render_marker(ReplanAttestation("f" * 32, 1, 2, 1, True))
TOO_MANY_MARKERS = [
    render_follow_up_marker(PR, f"R1-F{i}") for i in range(1, MAX_FINDINGS_PER_REVIEW + 2)
]


def _p(id_: str, base: Callable[[], dict], mutate: Mutation, match: str) -> Any:
    return pytest.param(base, mutate, match, id=id_)


CORRUPTIONS = [
    # The record's own keys and scalar fields.
    _p("missing-key", IMPL, without("reason"), r"is missing key\(s\) \['reason'\]"),
    _p("extra-key", IMPL, at("note", "x"), r"has unknown key\(s\) \['note'\]"),
    _p("position-type", IMPL, at("position", "1"), "position must be an integer"),
    _p("position-bool", IMPL, at("position", True), "position must be an integer"),
    _p(
        "position-over-plan",
        IMPL,
        at("position", MAX_EFFECTS_PER_PLAN),
        rf"must be in \[0, {MAX_EFFECTS_PER_PLAN - 1}\]",
    ),
    _p("position-negative", IMPL, at("position", -1), r"must be in \[0, "),
    _p("kind-unknown", IMPL, at("kind", "merge"), "is not a Wave 1 effect kind"),
    _p("kind-type", IMPL, at("kind", 2), "kind must be a string"),
    _p("stage-unknown", IMPL, at("stage", "done"), "is not a stage"),
    _p("stage-type", IMPL, at("stage", None), "stage must be a string"),
    _p("attempts-over-bound", IMPL, at("attempts", MAX_EFFECT_ATTEMPTS + 1), r"in \[0, 2\]"),
    _p("attempts-negative", IMPL, at("attempts", -1), r"in \[0, 2\]"),
    _p("attempts-bool", IMPL, at("attempts", True), "attempts must be an integer"),
    _p("attempts-type", IMPL, at("attempts", "1"), "attempts must be an integer"),
    _p("intended-charged", IMPL, at("attempts", 1), "is intended but records an issued attempt"),
    _p("attempted-uncharged", IMPL, at("stage", "attempted"), "is attempted but records no"),
    _p("completion-other", IMPL, at("completion", "anything"), "completion must be"),
    # ``observed`` exists in the observed stage only, and is validated there.
    _p(
        "observed-while-intended",
        IMPL,
        at("observed", {"url": PR43, "number": 43}),
        "must be null while the record is intended",
    ),
    _p(
        "observed-while-attempted",
        IMPL,
        both(at("stage", "attempted"), at("attempts", 1), at("observed", {"url": PR43})),
        "must be null while the record is attempted",
    ),
    _p(
        "observed-while-conflict",
        IMPL,
        both(at("stage", "conflict"), at("reason", "x"), at("observed", {"url": PR43})),
        "must be null while the record is conflict",
    ),
    _p("observed-missing", IMPL, observed_as(None), "observed must be an object"),
    _p(
        "observed-extra-key",
        IMPL,
        observed_as({"url": PR43, "number": 43, "sha": SHA_A}),
        r"has unknown key\(s\) \['sha'\]",
    ),
    _p("observed-missing-key", IMPL, observed_as({"url": PR43}), r"missing key\(s\) \['number'\]"),
    _p(
        "observed-number-type",
        IMPL,
        observed_as({"url": PR43, "number": "43"}),
        "number must be an integer",
    ),
    _p(
        "observed-non-canonical",
        IMPL,
        observed_as({"url": PR43 + "/", "number": 43}),
        "must be stored in its canonical form",
    ),
    _p(
        "observed-wrong-object",
        IMPL,
        observed_as({"url": ISSUE43, "number": 43}),
        "is not a GitHub URL of the expected kind",
    ),
    # ``reason`` is set exactly on a conflict, and bounded.
    _p("reason-without-conflict", IMPL, at("reason", "x"), "is set exactly when"),
    _p("conflict-without-reason", IMPL, at("stage", "conflict"), "is set exactly when"),
    _p(
        "reason-over-bound",
        IMPL,
        both(at("stage", "conflict"), at("reason", "r" * (MAX_CONFLICT_REASON_CHARS + 1))),
        f"over its bound of {MAX_CONFLICT_REASON_CHARS}",
    ),
    _p("reason-type", IMPL, both(at("stage", "conflict"), at("reason", 1)), "must be a string"),
    # The owner binding.
    _p("owner-type", IMPL, at("owner", "run-160"), "owner must be an object"),
    _p("owner-missing-key", IMPL, without("owner.run_id"), r"missing key\(s\) \['run_id'\]"),
    _p("owner-phase-unknown", IMPL, at("owner.phase", "DEPLOY"), "is not a phase"),
    _p("owner-phase-foreign", IMPL, at("owner.phase", "FIX"), "which FIX never plans"),
    _p("owner-txn-outside-replan", IMPL, at("owner.transaction_id", TXN), "must be empty outside"),
    _p("owner-txn-malformed", REPLACE, at("owner.transaction_id", "xyz"), "32-hex replan"),
    _p("owner-run-id-over-bound", IMPL, at("owner.run_id", "r" * 129), "over its bound of 128"),
    _p("owner-issue-non-canonical", IMPL, at("owner.issue_url", ISSUE + "/"), "canonical form"),
    _p("owner-pr-is-an-issue", IMPL, at("owner.pr_url", ISSUE), "not a GitHub URL of the expected"),
    # Identity, target, precondition and payload, per kind schema.
    _p("identity-type", IMPL, at("identity", []), "identity must be an object"),
    _p("identity-missing", IMPL, without("identity.head_branch"), r"missing key\(s\)"),
    _p("identity-extra", IMPL, at("identity.sha", SHA_A), r"unknown key\(s\) \['sha'\]"),
    _p("repository-shape", IMPL, at("identity.repository", "owner"), "<owner>/<repo>"),
    _p("marker-not-a-marker", IMPL, at("identity.marker", "Closes #2"), "exactly one well-formed"),
    _p("marker-doubled", IMPL, at("identity.marker", IMPL_MARKER * 2), "exactly one well-formed"),
    _p("marker-type", IMPL, at("identity.marker", 7), "marker must be a string"),
    _p("branch-dotdot", IMPL, at("target.head", "a..b"), "must be a plain branch name"),
    _p("branch-full-ref", IMPL, at("target.base", "refs/heads/main"), "plain branch name"),
    _p("branch-empty", IMPL, at("target.base", ""), "must be a plain branch name"),
    _p("absent-false", IMPL, at("precondition.absent", False), "must be true"),
    _p("absent-truthy", IMPL, at("precondition.absent", 1), "must be true"),
    _p("title-two-lines", IMPL, at("payload.title", "a\nb"), "must be one non-empty line"),
    _p("title-blank", IMPL, at("payload.title", "  "), "must be one non-empty line"),
    _p(
        "title-over-bound",
        IMPL,
        at("payload.title", "t" * (MAX_TITLE_CHARS + 1)),
        f"over its bound of {MAX_TITLE_CHARS}",
    ),
    _p(
        "body-over-bound",
        IMPL,
        at("payload.body", "x" * (MAX_BODY_CHARS + 1)),
        f"over its bound of {MAX_BODY_CHARS}",
    ),
    _p("body-nul", IMPL, at("payload.body", f"\x00\n\n{IMPL_MARKER}"), "a NUL or DEL character"),
    _p("body-del", IMPL, at("payload.body", f"\x7f\n\n{IMPL_MARKER}"), "a NUL or DEL character"),
    _p(
        "body-credential",
        IMPL,
        at("payload.body", f"{CREDENTIAL_TEXT}\n\n{IMPL_MARKER}"),
        "is not redaction-invariant",
    ),
    _p("title-credential", IMPL, at("payload.title", CREDENTIAL_TEXT), "redaction-invariant"),
    _p("body-type", IMPL, at("payload.body", None), "body must be a string"),
    # Per-kind cross checks: K2.
    _p("k2-body-without-marker", IMPL, at("payload.body", "Closes #2"), "does not end with its"),
    _p(
        "k2-marker-of-another-issue",
        IMPL,
        both(
            at("identity.marker", OTHER_IMPL_MARKER),
            at("payload.body", f"Closes #3\n\n{OTHER_IMPL_MARKER}"),
        ),
        "carries a marker for another issue than its owner's",
    ),
    _p("k2-head-mismatch", IMPL, at("identity.head_branch", "other"), "head branch that differs"),
    _p("k2-repo-mismatch", IMPL, at("target.repository", "owner/other"), "repository that differs"),
    # K2's text (#161): the closing block is the controller's, exactly, and the
    # title and the body text before it get the ANALYZE parser's rules again.
    _p(
        "k2-repo-of-another-issue",
        IMPL,
        both(at("identity.repository", "owner/other"), at("target.repository", "owner/other")),
        "in another repository than its owner's issue",
    ),
    _p(
        "k2-closes-another-issue",
        IMPL,
        at("payload.body", f"Implements it.\n\nCloses #3\n\n{IMPL_MARKER}"),
        "not its text, a blank line and the closing block of its owner's issue",
    ),
    _p(
        "k2-no-closing-line",
        IMPL,
        at("payload.body", f"Implements it.\n\n{IMPL_MARKER}"),
        "not its text, a blank line and the closing block of its owner's issue",
    ),
    _p(
        "k2-closing-line-not-after-a-blank-line",
        IMPL,
        at("payload.body", f"Implements it.\n{CLOSING}"),
        "not its text, a blank line and the closing block of its owner's issue",
    ),
    _p(
        "k2-title-mention",
        IMPL,
        at("payload.title", "Notify @octocat"),
        "invalid PR title: .*an @-mention",
    ),
    _p(
        "k2-title-closing-reference",
        IMPL,
        at("payload.title", "Fixes owner/repo#3"),
        "invalid PR title: .*a closing keyword",
    ),
    _p(
        "k2-title-marker-opener",
        IMPL,
        at("payload.title", "Add it <!-- ai-x -->"),
        "invalid PR title: .*a controller marker opener",
    ),
    _p(
        "k2-title-control-character",
        IMPL,
        at("payload.title", "Add\x1bit"),
        r"invalid PR title: .*a control character \(U\+001B",
    ),
    _p(
        "k2-title-not-stripped",
        IMPL,
        at("payload.title", "Add it "),
        "PR title that is not in the parser's stored form",
    ),
    _p(
        "k2-body-mention",
        IMPL,
        at("payload.body", f"Thanks @octocat.\n\n{CLOSING}"),
        "invalid PR body: .*an @-mention",
    ),
    _p(
        "k2-body-extra-closing-reference",
        IMPL,
        at("payload.body", f"Closes #3\nImplements it.\n\n{CLOSING}"),
        "invalid PR body: .*a closing keyword",
    ),
    _p(
        "k2-body-marker-opener",
        IMPL,
        at("payload.body", f"<!-- autoforge-note -->\n\n{CLOSING}"),
        "invalid PR body: .*a controller marker opener",
    ),
    _p(
        "k2-body-control-character",
        IMPL,
        at("payload.body", f"Implements\x0bit.\n\n{CLOSING}"),
        r"invalid PR body: .*a control character \(U\+000B",
    ),
    _p(
        "k2-body-over-its-bound",
        IMPL,
        at("payload.body", f"{'x' * (MAX_PR_BODY_CHARS + 1)}\n\n{CLOSING}"),
        f"invalid PR body: .*is {MAX_PR_BODY_CHARS + 1} characters",
    ),
    _p(
        "k2-body-blank",
        IMPL,
        at("payload.body", f"\n\n{CLOSING}"),
        "invalid PR body: .*missing required field 'pr_body'",
    ),
    _p(
        "k2-body-not-stripped",
        IMPL,
        at("payload.body", f"Implements it.\n\n\n{CLOSING}"),
        "PR body that is not in the parser's stored form",
    ),
    # K1.
    _p("k1-ref-not-full", PUSH, at("target.ref", BRANCH), r"full refs/heads/<branch> ref"),
    _p("k1-ref-bad-branch", PUSH, at("target.ref", "refs/heads/a b"), "plain branch name"),
    _p("k1-sha-short", PUSH, at("payload.sha", "abc"), "full 40-character lowercase commit SHA"),
    _p("k1-sha-upper", PUSH, at("identity.candidate_sha", "B" * 40), "40-character lowercase"),
    _p("k1-expected-bad", PUSH, at("precondition.expected_old", "x"), "40-character lowercase"),
    _p("k1-base-missing", PUSH, without("precondition.base_sha"), r"missing key\(s\)"),
    _p("k1-ref-mismatch", PUSH, at("identity.ref", "refs/heads/other"), "ref that differs"),
    _p("k1-repo-mismatch", PUSH, at("identity.repository", "o/r"), "repository that differs"),
    _p(
        "k1-candidate-mismatch",
        PUSH,
        at("identity.candidate_sha", SHA_C),
        "pushes another SHA than its identity's candidate",
    ),
    _p("k1-pushes-base", PUSH, at("precondition.base_sha", SHA_B), "pushes the base itself"),
    _p("k1-observed-elsewhere", PUSH, observed_as({"sha": SHA_C}), "is observed at another SHA"),
    # K3.
    _p("k3-digest-shape", ADOPT, at("precondition.base_sha256", "abc"), "64-character lowercase"),
    _p(
        "k3-digest-mismatch",
        ADOPT,
        at("precondition.base_sha256", sha256_text("another base")),
        "does not match its recorded base digest",
    ),
    _p(
        "k3-not-composed",
        ADOPT,
        at("payload.body", f"{ADOPT_BASE}\nCloses #2\n\n{IMPL_MARKER}"),
        "is not its base, the separator and its block",
    ),
    _p(
        "k3-block-without-marker",
        ADOPT,
        both(
            at("payload.block", "Closes #2"),
            at("payload.body", compose_append(ADOPT_BASE, "Closes #2")),
        ),
        "has a block that does not carry its identity's marker",
    ),
    _p(
        "k3-block-with-more-than-the-closing-block",
        ADOPT,
        both(
            at("payload.block", f"Closes #2 thanks @octocat\n\n{IMPL_MARKER}"),
            at(
                "payload.body",
                compose_append(ADOPT_BASE, f"Closes #2 thanks @octocat\n\n{IMPL_MARKER}"),
            ),
        ),
        "has a block that is not the closing block of its owner's issue",
    ),
    _p(
        "k3-block-closes-another-issue",
        ADOPT,
        both(
            at("payload.block", f"Closes #3\n\n{IMPL_MARKER}"),
            at("payload.body", compose_append(ADOPT_BASE, f"Closes #3\n\n{IMPL_MARKER}")),
        ),
        "has a block that is not the closing block of its owner's issue",
    ),
    _p(
        "k3-pr-of-another-repository",
        ADOPT,
        both(
            at("identity.pr_url", "https://github.com/owner/other/pull/42"),
            at("target.pr_url", "https://github.com/owner/other/pull/42"),
        ),
        "in another repository than its owner's issue",
    ),
    _p("k3-pr-mismatch", ADOPT, at("identity.pr_url", PR43), "PR that differs"),
    _p("k3-observed-other-pr", ADOPT, observed_as({"url": PR43}), "observed on another PR"),
    # K5.
    _p(
        "k5-marker-of-another-pr",
        FOLLOW,
        both(
            at("identity.marker", OTHER_PR_FOLLOW_UP),
            at("payload.body", f"Deferred.\n\n{OTHER_PR_FOLLOW_UP}"),
        ),
        "carries a marker for another PR than its owner's",
    ),
    # K5's text (#163): the reference and marker are the controller's, exactly,
    # and the title and the body text before them get the FIX parser's rules
    # again, because a resumed create publishes them with no result between.
    _p(
        "k5-repo-of-another-pr",
        FOLLOW,
        both(at("identity.repository", "owner/other"), at("target.repository", "owner/other")),
        "in another repository than its owner's PR",
    ),
    _p(
        "k5-body-without-the-reference",
        FOLLOW,
        at("payload.body", f"Deferred.{APPEND_SEPARATOR}{FOLLOW_UP_MARKER}"),
        "not its text, the controller's reference to its PR and finding, and its marker",
    ),
    _p(
        "k5-reference-to-another-finding",
        FOLLOW,
        at(
            "payload.body",
            follow_up_issue_body("Deferred.", PR, "R1-F2").replace(
                render_follow_up_marker(PR, "R1-F2"), FOLLOW_UP_MARKER
            ),
        ),
        "not its text, the controller's reference to its PR and finding, and its marker",
    ),
    _p(
        "k5-title-mention",
        FOLLOW,
        at("payload.title", "Ask @octocat"),
        "has an invalid follow-up title",
    ),
    _p(
        "k5-title-not-stripped",
        FOLLOW,
        at("payload.title", " Follow-up "),
        "follow-up title that is not in the parser's stored form",
    ),
    _p(
        "k5-body-text-closes-an-issue",
        FOLLOW,
        at("payload.body", follow_up_issue_body("Closes #3", PR, "R1-F1")),
        "has an invalid follow-up body",
    ),
    _p(
        "k5-body-text-blank",
        FOLLOW,
        at("payload.body", follow_up_issue_body("", PR, "R1-F1")),
        "has an invalid follow-up body",
    ),
    _p("k5-watermark-negative", FOLLOW, at("precondition.watermark", -1), "must be >= 0"),
    _p("k5-watermark-bool", FOLLOW, at("precondition.watermark", True), "must be an integer"),
    _p(
        "k5-observed-number-zero",
        FOLLOW,
        observed_as({"url": ISSUE43, "number": 0}),
        "must be >= 1",
    ),
    _p(
        "k5-observed-a-pr",
        FOLLOW,
        observed_as({"url": PR43, "number": 43}),
        "not a GitHub URL of the expected kind",
    ),
    # K6.
    _p("k6-markers-empty", APPEND, at("identity.markers", []), "must be a non-empty list"),
    _p("k6-markers-type", APPEND, at("identity.markers", FOLLOW_UP_MARKER), "non-empty list"),
    _p(
        "k6-markers-repeated",
        APPEND,
        at("identity.markers", [FOLLOW_UP_MARKER, FOLLOW_UP_MARKER]),
        "repeats a marker",
    ),
    _p(
        "k6-markers-over-bound",
        APPEND,
        at("identity.markers", TOO_MANY_MARKERS),
        f"over {MAX_FINDINGS_PER_REVIEW}",
    ),
    _p(
        "k6-marker-malformed",
        APPEND,
        at("identity.markers", ["<!-- ai-follow-up: {} -->"]),
        "exactly one well-formed ai-follow-up marker",
    ),
    _p("k6-block-not-markers", APPEND, at("payload.block", "x"), "is not its markers"),
    _p(
        "k6-marker-of-another-pr",
        APPEND,
        at("identity.markers", [OTHER_PR_FOLLOW_UP]),
        "follow-up marker for another PR than its owner's",
    ),
    _p(
        "k6-appends-to-current-issue",
        APPEND,
        both(at("identity.issue_url", ISSUE), at("target.issue_url", ISSUE)),
        "appends to the current issue",
    ),
    _p("k6-issue-mismatch", APPEND, at("identity.issue_url", ISSUE4), "issue that differs"),
    _p("k6-observed-other", APPEND, observed_as({"url": ISSUE4}), "observed on another issue"),
    # K7.
    _p(
        "k7-marker-of-another-transaction",
        REPLACE,
        both(
            at("identity.transaction_marker", OTHER_TXN_MARKER),
            at("payload.body", f"Replacement.\n\n{OTHER_TXN_MARKER}"),
        ),
        "the marker of another replan transaction",
    ),
    _p(
        "k7-body-without-marker",
        REPLACE,
        at("payload.body", "Replacement."),
        "does not end with its identity's marker",
    ),
    _p(
        "k7-marker-malformed",
        REPLACE,
        at("identity.transaction_marker", "<!-- autoforge-replan-transaction: {} -->"),
        "exactly one well-formed replan transaction marker",
    ),
    _p("k7-watermark-type", REPLACE, at("precondition.watermark", "42"), "must be an integer"),
    # K7's text (#164): the controller renders the closing block and the
    # marker after the agent's text, and the text gets the parser's rules.
    _p(
        "k7-no-closing-line",
        REPLACE,
        at("payload.body", f"Replacement.\n\n{REPLAN_MARKER}"),
        "not its text, the closing block of its owner's issue and its transaction marker",
    ),
    _p(
        "k7-closes-another-issue",
        REPLACE,
        at(
            "payload.body",
            f"Replacement.\n\nCloses #3\n\n{render_implementation_marker(ISSUE3)}"
            f"\n\n{REPLAN_MARKER}",
        ),
        "not its text, the closing block of its owner's issue and its transaction marker",
    ),
    _p(
        "k7-marker-attests-failed-tests",
        REPLACE,
        both(
            at("identity.transaction_marker", FAILED_REPLAN_MARKER),
            at("payload.body", replacement_pr_body("Replacement.", ISSUE, FAILED_REPLAN_MARKER)),
        ),
        "attesting failed tests",
    ),
    _p(
        "k7-text-closes-an-issue",
        REPLACE,
        at("payload.body", replacement_pr_body("Fixes #9 too.", ISSUE, REPLAN_MARKER)),
        "has an invalid PR body",
    ),
    _p(
        "k7-text-carries-a-marker",
        REPLACE,
        at(
            "payload.body",
            replacement_pr_body(f"Replacement.\n\n{OTHER_IMPL_MARKER}", ISSUE, REPLAN_MARKER),
        ),
        "has an invalid PR body",
    ),
    _p("k7-title-credential", REPLACE, at("payload.title", CREDENTIAL_TEXT), "redaction-invariant"),
    _p(
        "k7-observed-in-another-repository",
        REPLACE,
        observed_as({"url": "https://github.com/owner/other/pull/43", "number": 43}),
        "observed in another repository",
    ),
    # K4: the marker names a revision, not a PR; the PR is where the comment is posted.
    _p("k4-pr-mismatch", REVIEWED, at("identity.pr_url", PR43), "PR that differs"),
    _p(
        "k4-target-of-another-pr",
        REVIEWED,
        both(at("identity.pr_url", PR43), at("target.pr_url", PR43)),
        "targets another PR than its owner's",
    ),
    _p(
        "k4-marker-without-base",
        REVIEWED,
        both(
            at("identity.marker", PRE_BASE_REVIEW_MARKER),
            at("payload.body", f"Review.\n\n{PRE_BASE_REVIEW_MARKER}"),
        ),
        "without the base it reviewed",
    ),
    _p(
        "k4-marker-without-merge-base",
        REVIEWED,
        both(
            at("identity.marker", PRE_MERGE_BASE_REVIEW_MARKER),
            at("payload.body", f"Review.\n\n{PRE_MERGE_BASE_REVIEW_MARKER}"),
        ),
        "without the base it reviewed",
    ),
    _p(
        "k4-observed-elsewhere",
        REVIEWED,
        observed_as({"url": comment_url(PR43, 5)}),
        "observed on another PR than its target",
    ),
    # K8.
    _p(
        "k8-marker-of-another-issue",
        PROGRESS,
        both(
            at("identity.marker", render_progress_marker(ISSUE3, PR)),
            at("payload.body", f"Done.\n\n{render_progress_marker(ISSUE3, PR)}"),
        ),
        "carries a marker for another issue than its owner's",
    ),
    _p(
        "k8-marker-of-another-pr",
        PROGRESS,
        both(
            at("identity.marker", render_progress_marker(ISSUE, PR43)),
            at("payload.body", f"Done.\n\n{render_progress_marker(ISSUE, PR43)}"),
        ),
        "carries a marker for another PR than its owner's",
    ),
    _p("k8-epic-mismatch", PROGRESS, at("identity.epic_url", ISSUE3), "EPIC that differs"),
    _p(
        "k8-observed-elsewhere",
        PROGRESS,
        observed_as({"url": comment_url(ISSUE, 5)}),
        "observed on another issue than its EPIC",
    ),
    _p("k8-observed-not-a-comment", PROGRESS, observed_as({"url": EPIC}), "of the expected kind"),
    # K9 (#211).
    _p("k9-issue-mismatch", ACCEPT, at("identity.issue_url", ISSUE3), "issue that differs"),
    _p(
        "k9-another-issue",
        ACCEPT,
        both(at("identity.issue_url", ISSUE3), at("target.issue_url", ISSUE3)),
        "comments on another issue than its owner's",
    ),
    _p(
        "k9-marker-of-another-pr",
        ACCEPT,
        at("identity.marker", render_acceptance_marker(ISSUE, PR43, 1, "R1-F1")),
        "another PR than its owner's",
    ),
    _p(
        "k9-body-not-rendered",
        ACCEPT,
        at("payload.body", f"Evidence.\n\n{ACCEPTANCE_MARKER}"),
        "is not the controller's header, its text and its marker",
    ),
    _p(
        "k9-text-unpublishable",
        ACCEPT,
        at(
            "payload.body",
            acceptance_comment_body("ask @octocat", ISSUE, PR, 1, "R1-F1"),
        ),
        "has an invalid acceptance text",
    ),
    _p(
        "k9-text-not-stripped",
        ACCEPT,
        at(
            "payload.body",
            acceptance_comment_body(f" {ACCEPTANCE_TEXT}", ISSUE, PR, 1, "R1-F1"),
        ),
        "not in the parser's stored form",
    ),
    _p(
        "k9-authorization-unknown",
        ACCEPT,
        at("precondition.authorization", "agent"),
        "must be one of",
    ),
    _p(
        "k9-approval-malformed",
        ACCEPT,
        at("precondition.approved_action_id", "A-123"),
        "must be empty or an action id",
    ),
    _p(
        "k9-approval-of-other-bytes",
        ACCEPT,
        at("precondition.approved_action_id", "A-" + "0" * 16),
        "is not the record's action id",
    ),
    _p(
        "k9-approval-survives-an-edit",
        ACCEPT,
        at(
            "payload.body",
            acceptance_comment_body(ACCEPTANCE_TEXT + " Twice.", ISSUE, PR, 1, "R1-F1"),
        ),
        "is not the record's action id",
    ),
    _p(
        "k9-policy-without-approval",
        ACCEPT,
        at("precondition.approved_action_id", ""),
        "authorized by policy but records no approved action id",
    ),
    _p(
        "k9-attempted-unapproved",
        WAITING,
        both(at("stage", "attempted"), at("attempts", 1)),
        "records an issued attempt without an approval",
    ),
    _p("k9-not-absent", ACCEPT, at("precondition.absent", False), "absent"),
    _p(
        "k9-observed-elsewhere",
        ACCEPT,
        observed_as({"url": comment_url(ISSUE3, 5)}),
        "observed on another issue than its target",
    ),
]


@pytest.mark.parametrize(("base", "mutate", "match"), CORRUPTIONS)
def test_a_corrupt_record_fails_loudly(base, mutate, match):
    """Every corrupt or incomplete record is a StateError naming the rule; none is defaulted."""
    raw = base()
    mutate(raw)
    with pytest.raises(StateError, match=match) as info:
        EffectRecord.from_dict(raw)
    assert SECRET not in str(info.value)


@pytest.mark.parametrize("raw", [None, [], "record", 3])
def test_a_record_that_is_not_an_object_fails_loudly(raw):
    """A record must be a JSON object; nothing else is coerced into one."""
    with pytest.raises(StateError, match="must be an object"):
        EffectRecord.from_dict(raw)


def test_plan_refuses_a_record_it_could_not_persist():
    """``plan`` validates exactly as a load does: a credential never becomes a record."""
    with pytest.raises(StateError, match="is not redaction-invariant") as info:
        impl_record(body=f"{CREDENTIAL_TEXT}\n\n{IMPL_MARKER}")
    assert SECRET not in str(info.value)
    with pytest.raises(StateError, match="must be one non-empty line"):
        impl_record(title="two\nlines")


# -- record schema: field bounds --------------------------------------------------------


def _body_of_length(length: int, marker: str = IMPL_MARKER) -> str:
    tail = f"\n\n{marker}"
    return "x" * (length - len(tail)) + tail


def test_field_bounds_are_inclusive():
    """Each field holds exactly its bound and refuses one character more."""
    assert len(impl_record(title="t" * MAX_TITLE_CHARS).payload["title"]) == MAX_TITLE_CHARS
    marker_line = render_follow_up_marker(PR, "R1-F1")
    base = "x" * (MAX_BODY_CHARS - len(APPEND_SEPARATOR) - len(marker_line))
    assert len(append_record(base=base).body) == MAX_BODY_CHARS
    with pytest.raises(StateError, match=f"over its bound of {MAX_BODY_CHARS}"):
        append_record(base=base + "x")
    # K5's body is the agent's text, at most the parser's bound, then the
    # controller's reference to the PR and finding and the marker (#163).
    text = "x" * MAX_FOLLOW_UP_BODY_CHARS
    assert follow_up_record(body=follow_up_issue_body(text, PR, "R1-F1")).body.startswith(text)
    with pytest.raises(StateError, match=f"is {MAX_FOLLOW_UP_BODY_CHARS + 1} characters"):
        follow_up_record(body=follow_up_issue_body(text + "x", PR, "R1-F1"))
    # K2's body is the agent's text, at most the parser's bound, then the closing block.
    text = "x" * MAX_PR_BODY_CHARS
    assert impl_record(body=f"{text}\n\n{CLOSING}").body.startswith(text)
    with pytest.raises(StateError, match=f"is {MAX_PR_BODY_CHARS + 1} characters"):
        impl_record(body=f"{text}x\n\n{CLOSING}")
    assert impl_record(by=owner(Phase.ANALYZE_EXECUTE, run_id="r" * 128)).owner.run_id
    raw = impl_record().to_dict() | {"stage": "conflict", "reason": "r" * MAX_CONFLICT_REASON_CHARS}
    assert len(EffectRecord.from_dict(raw).reason) == MAX_CONFLICT_REASON_CHARS
    ids = tuple(f"R1-F{i}" for i in range(1, MAX_FINDINGS_PER_REVIEW + 1))
    assert len(append_record(finding_ids=ids).identity["markers"]) == MAX_FINDINGS_PER_REVIEW


def test_a_conflict_reason_is_redacted_and_clipped_before_it_is_kept():
    """``conflicting`` keeps a reason that loads: redacted, clipped at the bound."""
    long = impl_record().conflicting("r" * (3 * MAX_CONFLICT_REASON_CHARS))
    assert len(long.reason) == MAX_CONFLICT_REASON_CHARS
    assert long.reason.endswith("…")
    secret = impl_record().conflicting(f"gh said {CREDENTIAL_TEXT}")
    assert SECRET not in secret.reason
    for record in (long, secret):
        assert EffectRecord.from_dict(record.to_dict()) == record


# -- record schema: stage transitions --------------------------------------------------


def test_the_attempt_bound_is_enforced_by_the_record_itself():
    """``attempting`` charges one attempt per issue and refuses past the bound or a terminal."""
    first = impl_record().attempting()
    second = first.attempting()
    assert (first.stage, first.attempts, second.attempts) == (Stage.ATTEMPTED, 1, 2)
    with pytest.raises(StateError, match=f"has used its {MAX_EFFECT_ATTEMPTS}"):
        second.attempting()
    with pytest.raises(StateError, match="is observed; not issued"):
        first.observing({"url": PR43, "number": 43}).attempting()
    with pytest.raises(StateError, match="is conflict; not issued"):
        first.conflicting("x").attempting()


def test_observing_revalidates_and_refuses_a_terminal_record():
    """An observed result is checked like a load; a conflict is never observed."""
    with pytest.raises(StateError, match="observed on another PR"):
        adopt_record().observing({"url": PR43})
    with pytest.raises(StateError, match="is conflict; not observed"):
        adopt_record().conflicting("x").observing({"url": PR})


def test_rebasing_is_for_an_intended_append_only():
    """D5.5: a rebase charges an attempt and records the new base; nothing else rebases."""
    edited = "A human clarified the description."
    rebased = adopt_record().rebasing(edited)
    assert (rebased.stage, rebased.attempts) == (Stage.ATTEMPTED, 1)
    assert rebased.precondition["base_sha256"] == sha256_text(edited)
    assert rebased.body == compose_append(edited, rebased.payload["block"])
    with pytest.raises(StateError, match="cannot be rebased"):
        impl_record().rebasing(edited)
    with pytest.raises(StateError, match="cannot be rebased"):
        adopt_record().attempting().rebasing(edited)


def test_a_reopened_conflict_keeps_its_attempt_count():
    """``unblock`` reconciles a conflict again without resetting the D4.3 bound."""
    assert impl_record().conflicting("x").reopened() == impl_record()
    charged = impl_record().attempting().conflicting("x").reopened()
    assert (charged.stage, charged.attempts, charged.reason) == (Stage.ATTEMPTED, 1, "")
    observed = impl_record().observing({"url": PR43, "number": 43})
    assert observed.reopened() is observed


# -- the plan: record count, owner, binding, total size ---------------------------------

FIX_BINDING = Binding(RUN_ID, Phase.FIX, ISSUE, PR, review_round=1, transaction_id="")


def fix_plan(follow_ups: int) -> list[dict]:
    """A FIX plan: one push, then one follow-up issue per finding, in position order."""
    records = [push_record(position=0)]
    records += [
        follow_up_record(finding_id=f"R1-F{i}", position=i) for i in range(1, follow_ups + 1)
    ]
    return [r.to_dict() for r in records]


def test_the_largest_plan_loads():
    """D2.4: one push plus one follow-up per possible finding is the bound, and it loads."""
    plan = fix_plan(MAX_EFFECTS_PER_PLAN - 1)
    assert len(load_records(plan, FIX_BINDING)) == MAX_EFFECTS_PER_PLAN


def test_a_plan_over_the_record_bound_fails_loudly():
    """One record over the bound is refused before any record is read."""
    plan = fix_plan(MAX_EFFECTS_PER_PLAN - 1)
    with pytest.raises(StateError, match=f"holds {MAX_EFFECTS_PER_PLAN + 1} records, over"):
        load_records([*plan, plan[-1]], FIX_BINDING)


@pytest.mark.parametrize(
    ("raw", "binding", "match"),
    [
        pytest.param({"0": {}}, FIX_BINDING, "must be a list", id="not-a-list"),
        pytest.param(
            [push_record().to_dict(), follow_up_record(position=2).to_dict()],
            FIX_BINDING,
            "is at position 2, not 1",
            id="position-gap",
        ),
        pytest.param(
            [
                push_record().to_dict(),
                follow_up_record(by=owner(Phase.FIX, pr_url=PR, run_id="other")).to_dict(),
            ],
            FIX_BINDING,
            "has another owner than the plan's first record",
            id="two-owners",
        ),
        pytest.param(
            [push_record().to_dict(), push_record(position=1).to_dict()],
            FIX_BINDING,
            "plans more than one push",
            id="two-pushes",
        ),
        pytest.param(
            fix_plan(1), replace(FIX_BINDING, run_id="other"), "another run", id="other-run"
        ),
        pytest.param(
            fix_plan(1),
            replace(FIX_BINDING, phase=Phase.REVIEW),
            "is bound to FIX but the state is in REVIEW",
            id="other-phase",
        ),
        pytest.param(
            fix_plan(1), replace(FIX_BINDING, issue_url=ISSUE3), "another issue", id="other-issue"
        ),
        pytest.param(fix_plan(1), replace(FIX_BINDING, pr_url=PR43), "another PR", id="other-pr"),
        pytest.param(
            [replacement_record(position=0).to_dict()],
            Binding(RUN_ID, Phase.REPLAN_REEXECUTE, ISSUE, PR, 1, "f" * 32),
            "another replan transaction",
            id="other-transaction",
        ),
    ],
)
def test_a_plan_that_does_not_fit_its_state_fails_loudly(raw, binding, match):
    """A plan is in position order, has one owner and one push, and is the state's."""
    with pytest.raises(StateError, match=match):
        load_records(raw, binding)


def test_a_blocked_state_loads_the_plan_of_the_phase_it_stopped():
    """A conflict stops the phase in BLOCKED; the plan is still the state's own."""
    blocked = replace(FIX_BINDING, phase=Phase.BLOCKED)
    assert [r.kind for r in load_records(fix_plan(1), blocked)] == [
        EffectKind.PUSH,
        EffectKind.FOLLOW_UP_ISSUE,
    ]


FIX_CONTEXT = {"phase": "FIX", "issue_url": ISSUE, "pr_url": PR, "round": 1, "resolutions": []}


def _maximal_follow_ups(count: int) -> list[dict]:
    records = [
        follow_up_record(
            finding_id=f"R1-F{i}",
            position=i - 1,
            body=follow_up_issue_body("x" * MAX_FOLLOW_UP_BODY_CHARS, PR, f"R1-F{i}"),
        )
        for i in range(1, count + 1)
    ]
    return [r.to_dict() for r in records]


def test_the_total_size_bound_counts_payloads_and_the_context():
    """D2.4: payloads plus the context's stored bound stay under MAX_EFFECT_STATE_CHARS."""
    room = MAX_EFFECT_STATE_CHARS - FixContext.STORED_BOUND
    largest = payload_chars([EffectRecord.from_dict(_maximal_follow_ups(1)[0])])
    over = room // largest + 1
    assert over <= MAX_EFFECTS_PER_PLAN
    fits = load_phase_effects(_maximal_follow_ups(over - 1), {}, FIX_CONTEXT, FIX_BINDING)
    assert len(fits.records) == over - 1
    with pytest.raises(StateError, match=f"over the bound of {MAX_EFFECT_STATE_CHARS}"):
        load_phase_effects(_maximal_follow_ups(over), {}, FIX_CONTEXT, FIX_BINDING)
    assert plan_size_problem((), None) == ""


def test_records_without_their_completion_context_fail_loudly():
    """The records and the context are saved together; records alone are corruption."""
    with pytest.raises(StateError, match="without the completion context"):
        load_phase_effects(fix_plan(1), {}, {}, FIX_BINDING)


def test_pieces_naming_two_phases_fail_loudly():
    """Records, observation and context of one entry name one phase."""
    blocked = replace(FIX_BINDING, phase=Phase.BLOCKED)
    with pytest.raises(StateError, match="names more than one phase"):
        load_phase_effects(fix_plan(1), {}, AnalyzeContext(ISSUE).to_dict(), blocked)


ANALYZE_BINDING = Binding(RUN_ID, Phase.ANALYZE_EXECUTE, ISSUE, "", 0, "")
ANALYZE_REF = f"refs/heads/{BRANCH}"


def analyze_observation(
    head: str | None = SHA_A,
    base: str | None = SHA_C,
    ref: str = ANALYZE_REF,
    *,
    pr: str | None = None,
    prs: dict | None = None,
    default_refs: dict | None = None,
):
    """The ANALYZE_EXECUTE entry read: the branch, the default branch at the base, its PR."""
    default = {"refs/heads/main": base} if default_refs is None else default_refs
    return {
        "phase": "ANALYZE_EXECUTE",
        "issue_url": ISSUE,
        "pr_url": "",
        "refs": {ref: head, **default},
        "base_sha": base,
        "objects": {IMPL_MARKER: None},
        **({"prs": {ref: pr}} if prs is None else {"prs": prs} if prs else {}),
    }


def analyze_push() -> EffectRecord:
    return push_record(by=ANALYZE_OWNER)


@pytest.mark.parametrize("pr", [None, PR], ids=["no-pr", "pr"])
def test_an_analyze_observation_round_trips_with_the_pr_on_its_branch(pr):
    """#161: the open PR the entry read on the branch, or none, is stored by its ref."""
    raw = analyze_observation(pr=pr)
    observation = EntryObservation.from_dict(raw, ANALYZE_BINDING)
    assert observation.prs == {ANALYZE_REF: pr}
    assert observation.to_dict() == raw


def test_an_observation_that_read_no_pr_is_stored_without_prs():
    """#161: ``prs`` is written only when PRs were read, so other phases' are unchanged."""
    raw = analyze_observation(prs={})
    assert "prs" not in raw
    observation = EntryObservation.from_dict(raw, ANALYZE_BINDING)
    assert observation.prs == {}
    assert observation.to_dict() == raw


@pytest.mark.parametrize(
    ("second", "pr"), [(impl_record, None), (adopt_record, PR)], ids=["create", "adopt"]
)
def test_an_analyze_context_loads_with_its_push_and_pr_plan(second, pr):
    """#161: the context, the push then the PR create or adoption, and the observation load."""
    effects = load_phase_effects(
        [analyze_push().to_dict(), second().to_dict()],
        analyze_observation(pr=pr),
        AnalyzeContext(ISSUE).to_dict(),
        ANALYZE_BINDING,
    )
    assert [r.kind for r in effects.records] == [EffectKind.PUSH, second().kind]
    assert isinstance(effects.context, AnalyzeContext)


@pytest.mark.parametrize(
    ("records", "observation", "match"),
    [
        ([], analyze_observation(), "a push then a PR create or adoption"),
        ([analyze_push().to_dict()], analyze_observation(), "a push then a PR create"),
        (
            [replace(impl_record(), position=0).to_dict()],
            analyze_observation(),
            "a push then a PR create or adoption",
        ),
        (None, {}, "with the entry observation"),
        (None, analyze_observation(ref="refs/heads/elsewhere"), "a ref the entry observation"),
        (None, analyze_observation(head=SHA_B), "another head than the entry observed"),
        (None, analyze_observation(head=None), "another head than the entry observed"),
        (None, analyze_observation(base=SHA_A), "another base than the entry read"),
        (
            [analyze_push().to_dict(), impl_record(head="autoforge/2-other").to_dict()],
            analyze_observation(),
            "from another branch than the one it pushes",
        ),
        (
            [analyze_push().to_dict(), impl_record(base="trunk").to_dict()],
            analyze_observation(),
            "onto another branch than the default branch the entry read",
        ),
        (
            None,
            analyze_observation(default_refs={}),
            "onto another branch than the default branch the entry read",
        ),
        (
            None,
            analyze_observation(default_refs={"refs/heads/main": SHA_B}),
            "onto another branch than the default branch the entry read",
        ),
        (
            [analyze_push().to_dict(), impl_record(base=BRANCH).to_dict()],
            analyze_observation(),
            "onto another branch than the default branch the entry read",
        ),
        (None, analyze_observation(prs={}), "did not read the PRs on its ref"),
        (
            None,
            analyze_observation(prs={"refs/heads/main": None}),
            "did not read the PRs on its ref",
        ),
        (None, analyze_observation(pr=PR), "where the entry observed an open PR"),
        (
            [analyze_push().to_dict(), adopt_record().to_dict()],
            analyze_observation(),
            "adopts another PR than the one the entry observed",
        ),
        (
            [analyze_push().to_dict(), adopt_record().to_dict()],
            analyze_observation(pr=PR43),
            "adopts another PR than the one the entry observed",
        ),
    ],
    ids=[
        "no-plan",
        "push-only",
        "pr-only",
        "no-observation",
        "unobserved-ref",
        "other-old-head",
        "branch-observed-absent",
        "other-base",
        "pr-from-another-branch",
        "pr-onto-another-base-branch",
        "default-branch-not-observed",
        "default-branch-at-another-head",
        "pr-onto-its-own-branch",
        "prs-not-read",
        "prs-read-on-another-ref",
        "create-beside-an-observed-pr",
        "adopt-where-none-was-observed",
        "adopt-another-pr",
    ],
)
def test_an_analyze_context_the_entry_does_not_explain_fails_loudly(records, observation, match):
    """#161: a plan whose push no entry read explains is never completed."""
    if records is None:
        records = [analyze_push().to_dict(), impl_record().to_dict()]
    with pytest.raises(StateError, match=match):
        load_phase_effects(records, observation, AnalyzeContext(ISSUE).to_dict(), ANALYZE_BINDING)


REPLAN_BINDING = Binding(RUN_ID, Phase.REPLAN_REEXECUTE, ISSUE, PR, 1, TXN)
REPLAN_REF = f"refs/heads/{REPLACEMENT_BRANCH}"
REPLAN_CONTEXT = ReplanContext(ISSUE, PR, TXN, 2, 1)


def replan_observation(
    head: str | None = None,
    base: str | None = SHA_C,
    ref: str = REPLAN_REF,
    *,
    default_refs: dict | None = None,
):
    """The REPLAN_REEXECUTE entry read (#164): the derived branch absent, the base."""
    default = {"refs/heads/main": base} if default_refs is None else default_refs
    return {
        "phase": "REPLAN_REEXECUTE",
        "issue_url": ISSUE,
        "pr_url": PR,
        "refs": {ref: head, **default},
        "base_sha": base,
        "objects": {},
    }


def replan_push(**kw: Any) -> EffectRecord:
    return push_record(
        **{"expected_old": None, "branch": REPLACEMENT_BRANCH, "by": REPLAN_OWNER, **kw}
    )


def replan_plan() -> list[dict]:
    return [replan_push().to_dict(), replacement_record().to_dict()]


def test_a_replan_context_loads_with_its_push_and_replacement_pr_plan():
    """#164: the context, the push of the candidate then the replacement PR create,
    and the entry observation that read the derived branch absent, load."""
    effects = load_phase_effects(
        replan_plan(), replan_observation(), REPLAN_CONTEXT.to_dict(), REPLAN_BINDING
    )
    assert [r.kind for r in effects.records] == [EffectKind.PUSH, EffectKind.REPLACEMENT_PR]
    assert effects.context == REPLAN_CONTEXT


@pytest.mark.parametrize(
    ("records", "observation", "context", "match"),
    [
        pytest.param([], None, None, "a push then the replacement PR", id="no-plan"),
        pytest.param(
            [replan_push().to_dict()], None, None, "a push then the replacement PR", id="push-only"
        ),
        pytest.param(
            [replacement_record(position=0).to_dict()],
            None,
            None,
            "a push then the replacement PR",
            id="pr-only",
        ),
        pytest.param(None, {}, None, "with the entry observation", id="no-observation"),
        pytest.param(
            None,
            replan_observation(ref="refs/heads/elsewhere"),
            None,
            "a ref the entry observation did not read as absent",
            id="unobserved-ref",
        ),
        pytest.param(
            None,
            replan_observation(head=SHA_A),
            None,
            "a ref the entry observation did not read as absent",
            id="branch-observed-present",
        ),
        pytest.param(
            [replan_push(expected_old=SHA_A).to_dict(), replacement_record().to_dict()],
            None,
            None,
            "compared against absent",
            id="push-over-a-head",
        ),
        pytest.param(
            None, replan_observation(base=SHA_A), None, "another base than the entry", id="base"
        ),
        pytest.param(
            [replan_push().to_dict(), replacement_record(head="autoforge/2-other").to_dict()],
            None,
            None,
            "from another branch than the one it pushes",
            id="pr-from-another-branch",
        ),
        pytest.param(
            None,
            replan_observation(default_refs={}),
            None,
            "onto another branch than the default branch the entry read",
            id="default-branch-not-observed",
        ),
        pytest.param(
            None,
            replan_observation(default_refs={"refs/heads/main": SHA_B}),
            None,
            "onto another branch than the default branch the entry read",
            id="default-branch-at-another-head",
        ),
        pytest.param(
            None,
            None,
            replace(REPLAN_CONTEXT, historical_findings_considered=3),
            "a transaction marker that attests other counts",
            id="other-findings-count",
        ),
        pytest.param(
            None,
            None,
            replace(REPLAN_CONTEXT, unique_failure_constraints=2),
            "a transaction marker that attests other counts",
            id="other-unique-count",
        ),
        pytest.param(
            None,
            None,
            replace(REPLAN_CONTEXT, unique_failure_constraints=3),
            "more unique constraints than findings considered",
            id="more-constraints-than-findings",
        ),
    ],
)
def test_a_replan_context_the_entry_does_not_explain_fails_loudly(
    records, observation, context, match
):
    """#164: a replan plan no entry read explains is never driven: the derived
    branch was read absent at the recorded base, the PR goes from it onto the
    default branch, and the marker attests the counts the context records."""
    with pytest.raises(StateError, match=match):
        load_phase_effects(
            replan_plan() if records is None else records,
            replan_observation() if observation is None else observation,
            (REPLAN_CONTEXT if context is None else context).to_dict(),
            REPLAN_BINDING,
        )


@pytest.mark.parametrize(
    ("mutate", "rule"),
    [
        (at("payload.title", "Notify @octocat"), "invalid PR title: .*an @-mention"),
        (
            at("payload.body", f"Closes #3\nImplements it.\n\n{CLOSING}"),
            "invalid PR body: .*a closing keyword",
        ),
        (
            at("payload.body", f"Implements it.\n\nCloses #3\n\n{CLOSING}"),
            "invalid PR body: .*a closing keyword",
        ),
    ],
    ids=["title-mention", "closing-reference-first", "closing-reference-before-the-block"],
)
def test_an_analyze_plan_whose_pr_text_the_parser_refuses_fails_to_load(mutate, rule):
    """R2-F1: a journaled K2 is created with no agent result in between, so a
    title or body text the ANALYZE parser refuses fails the load; the plan is
    never completed from it."""
    raw = impl_record().to_dict()
    mutate(raw)
    with pytest.raises(StateError, match=rule) as info:
        load_phase_effects(
            [analyze_push().to_dict(), raw],
            analyze_observation(),
            AnalyzeContext(ISSUE).to_dict(),
            ANALYZE_BINDING,
        )
    assert "octocat" not in str(info.value)


def test_an_adoption_keeps_the_adopted_prs_own_body_as_it_is():
    """#161 (ADR 0004 K3): the base of an adoption is the PR's existing body, a
    human's text the controller does not rewrite, so the agent-text policy is
    not applied to it; only the block is the controller's, and it is exactly
    the closing block of the owner's issue."""
    assert implementation_closing_block(ISSUE) == CLOSING
    base = "Started by hand, thanks @octocat.\n\nFixes #3"
    effects = load_phase_effects(
        [analyze_push().to_dict(), adopt_record(base).to_dict()],
        analyze_observation(pr=PR),
        AnalyzeContext(ISSUE).to_dict(),
        ANALYZE_BINDING,
    )
    assert effects.records[1].body == compose_append(base, CLOSING)


# -- operations: success and adoption (all GitHub kinds) --------------------------------


@by_case
def test_success_saves_the_attempt_before_the_write(case):
    """D4.1: ``attempted`` is durable before the write, ``observed`` after its read-back."""
    fake, record = world_of(case)
    saves = saves_for(fake)
    driven = drive(record, operation_for(record, fake), saves)

    assert saves.stages == [(Stage.ATTEMPTED, 1, 0), (Stage.OBSERVED, 1, 1)]
    assert driven.issued and driven.record == saves.last
    assert [(name, body) for name, _, body in fake.effect_writes] == [(case.write, record.body)]
    landed = case.landed(fake, record)
    assert len(landed) == 1
    assert driven.record.observed is not None and driven.record.observed["url"] == landed[0]


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_operation_for_maps_each_kind_to_its_operation(case):
    """Every GitHub kind has exactly one operation, chosen by the record's kind."""
    fake, record = world_of(case)
    assert type(operation_for(record, fake)) is case.op_type


@by_case
def test_an_object_already_in_the_end_state_is_adopted_without_a_write(case):
    """D4.2: reconcile first; the intended end state on GitHub is observed, not re-sent."""
    fake, record = world_of(case)
    url = case.plant(fake, record)
    saves = saves_for(fake)
    driven = drive(record, operation_for(record, fake), saves)

    assert saves.stages == [(Stage.OBSERVED, 0, 0)]
    assert not driven.issued and fake.effect_writes == []
    assert driven.record.observed is not None and driven.record.observed["url"] == url


@by_case
def test_an_object_with_the_identity_but_not_the_payload_is_a_conflict(case):
    """A colliding object is never repaired or duplicated: conflict, naming it, no write."""
    fake, record = world_of(case)
    url = case.imposter(fake, record)
    saves = saves_for(fake)
    driven = drive(record, operation_for(record, fake), saves)

    assert saves.stages == [(Stage.CONFLICT, 0, 0)]
    assert not driven.issued and fake.effect_writes == []
    assert url in driven.record.reason
    with pytest.raises(EffectConflictError) as info:
        raise_conflict(driven.record)
    assert url in str(info.value)


@by_case
def test_duplicate_identity_holders_are_a_conflict(case):
    """Two objects carrying one identity are a conflict naming the other one, no write."""
    fake, record = world_of(case)
    mine = case.plant(fake, record)
    other = case.second_holder(fake, record)
    saves = saves_for(fake)
    driven = drive(record, operation_for(record, fake), saves)

    assert saves.stages == [(Stage.CONFLICT, 0, 0)]
    assert fake.effect_writes == []
    assert other in driven.record.reason
    if case in CREATE_CASES:
        assert mine in driven.record.reason
        assert "2 objects carry its identity" in driven.record.reason


@by_case
def test_an_observed_record_is_never_read_or_written_again(case):
    """A duplicate invocation of an observed record is a no-op: no read, no write, no save."""
    fake, record = world_of(case)
    observed = drive(record, operation_for(record, fake), saves_for(fake)).record
    writes = list(fake.effect_writes)
    saves = saves_for(fake)
    again = drive(observed, operation_for(observed, Outage(fake, down=True)), saves)

    assert again.record is observed and not again.issued
    assert saves.log == [] and fake.effect_writes == writes


# -- operations: ambiguous and refused writes ------------------------------------------


@by_case
def test_a_landed_write_whose_reply_and_read_back_are_lost_is_reconciled_next(case):
    """W4: the write landed but neither its reply nor its read-back arrived. The record
    stays attempted; the next drive observes it without a second write."""
    fake, record = world_of(case)
    fake.write_failures.append((case.write, GitHubUnavailableError("reply lost"), True))
    github = Outage(fake)

    def lose_read_back(saved: EffectRecord) -> None:
        github.down = saved.stage is Stage.ATTEMPTED

    saves = saves_for(fake, hook=lose_read_back)
    op = operation_for(record, github)
    with pytest.raises(EffectPending):
        drive(record, op, saves)
    assert saves.stages == [(Stage.ATTEMPTED, 1, 0)]
    assert len(case.landed(fake, record)) == 1

    github.down = False
    driven = drive(saves.last, op, saves)
    assert saves.stages[-1] == (Stage.OBSERVED, 1, 1)
    assert not driven.issued and len(fake.effect_writes) == 1
    assert case.landed(fake, record) == [driven.record.observed["url"]]


@by_case
def test_a_write_that_never_landed_is_reissued_once_within_the_bound(case):
    """W3: the write never reached GitHub. The read-back shows nothing, so the next drive
    issues it once more (attempt 2 of 2), saved before that write too."""
    fake, record = world_of(case)
    fake.write_failures.append((case.write, GitHubUnavailableError("connection reset"), False))
    op = operation_for(record, fake)
    saves = saves_for(fake)
    with pytest.raises(EffectPending):
        drive(record, op, saves)
    assert saves.stages == [(Stage.ATTEMPTED, 1, 0)]
    assert case.landed(fake, record) == []

    driven = drive(saves.last, op, saves)
    assert saves.stages == [
        (Stage.ATTEMPTED, 1, 0),
        (Stage.ATTEMPTED, 2, 1),
        (Stage.OBSERVED, 2, 2),
    ]
    assert driven.issued and len(fake.effect_writes) == 2
    assert len(case.landed(fake, record)) == 1


@by_case
def test_two_lost_writes_spend_the_bound_and_the_next_entry_blocks(case):
    """D4.3: after two issues that never landed, the third drive is a conflict, not a third
    write; the second drive leaves the record attempted (it may still become visible)."""
    fake, record = world_of(case)
    for _ in range(2):
        fake.write_failures.append((case.write, GitHubUnavailableError("reset"), False))
    op = operation_for(record, fake)
    saves = saves_for(fake)
    with pytest.raises(EffectPending):
        drive(record, op, saves)
    with pytest.raises(EffectPending):
        drive(saves.last, op, saves)
    assert saves.stages == [(Stage.ATTEMPTED, 1, 0), (Stage.ATTEMPTED, 2, 1)]

    driven = drive(saves.last, op, saves)
    assert driven.record.stage is Stage.CONFLICT and not driven.issued
    assert len(fake.effect_writes) == 2 and case.landed(fake, record) == []


@by_case
def test_an_exhausted_bound_is_a_conflict_naming_the_manual_step(case):
    """A record whose two attempts are spent and whose write is absent is a conflict that
    names the effect and what the operator does by hand; nothing is sent."""
    fake, record = world_of(case)
    spent = record.attempting().attempting()
    saves = saves_for(fake)
    driven = drive(spent, operation_for(spent, fake), saves)

    assert saves.stages == [(Stage.CONFLICT, 2, 0)]
    assert fake.effect_writes == []
    reason = driven.record.reason
    assert record.describe() in reason
    assert f"has used its {MAX_EFFECT_ATTEMPTS} attempts" in reason
    assert "perform the write by hand" in reason and "'unblock'" in reason
    with pytest.raises(EffectConflictError, match="perform the write by hand"):
        raise_conflict(driven.record)


@by_case
def test_a_conclusively_refused_write_is_a_conflict_and_not_retried(case):
    """D4.3: a write GitHub refused (not ambiguous) is a conflict after one read-back."""
    fake, record = world_of(case)
    fake.write_failures.append((case.write, GitHubError("HTTP 422: Validation Failed"), False))
    saves = saves_for(fake)
    driven = drive(record, operation_for(record, fake), saves)

    assert saves.stages == [(Stage.ATTEMPTED, 1, 0), (Stage.CONFLICT, 1, 1)]
    assert driven.issued
    assert "GitHub refused the write" in driven.record.reason
    assert "the target does not hold it" in driven.record.reason
    assert "a refused write is not retried" in driven.record.reason


@by_case
def test_an_unreadable_github_before_the_write_charges_nothing(case):
    """A reconciliation read that fails transiently raises before anything is saved or sent."""
    fake, record = world_of(case)
    saves = saves_for(fake)
    with pytest.raises(GitHubUnavailableError) as info:
        drive(record, operation_for(record, Outage(fake, down=True)), saves)
    assert not isinstance(info.value, EffectPending)
    assert saves.log == [] and fake.effect_writes == []


# -- operations: crash windows and human mutations -------------------------------------


@by_case
def test_a_crash_after_the_write_before_the_save_resumes_without_a_second_write(case):
    """The process dies between the write and the observed save. Driving the persisted
    pre-write record again observes the object; nothing is sent twice."""
    fake, record = world_of(case)
    op = operation_for(record, fake)
    crashing = saves_for(fake, crash_on=Stage.OBSERVED)
    with pytest.raises(Crash):
        drive(record, op, crashing)
    persisted = crashing.last
    assert (persisted.stage, persisted.attempts) == (Stage.ATTEMPTED, 1)

    saves = saves_for(fake)
    driven = drive(persisted, op, saves)
    assert saves.stages == [(Stage.OBSERVED, 1, 1)]
    assert not driven.issued and len(fake.effect_writes) == 1
    assert len(case.landed(fake, record)) == 1


@by_case
def test_a_human_change_after_a_lost_write_is_a_conflict_not_a_reissue(case):
    """Between the persisted attempt and the next issue a human changed the target: the
    next drive is a conflict naming the object, and the write is not sent again."""
    fake, record = world_of(case)
    fake.write_failures.append((case.write, GitHubUnavailableError("reset"), False))
    op = operation_for(record, fake)
    saves = saves_for(fake)
    with pytest.raises(EffectPending):
        drive(record, op, saves)
    url = case.human_edit(fake, record)

    driven = drive(saves.last, op, saves)
    assert driven.record.stage is Stage.CONFLICT and not driven.issued
    assert url in driven.record.reason
    assert len(fake.effect_writes) == 1


def _publish_after_the_attempt_save(case: Case) -> tuple[FakeGitHub, EffectRecord, Saves, list]:
    fake, record = world_of(case)
    named: list[str] = []

    def human(saved: EffectRecord) -> None:
        if saved.stage is Stage.ATTEMPTED:
            named.append(case.imposter(fake, record))

    return fake, record, saves_for(fake, hook=human), named


@pytest.mark.parametrize("case", [K4, K5, K8], ids=repr)
def test_a_human_object_published_after_the_attempt_save_is_a_conflict(case):
    """A human publishes an object with the identity after the attempted save, before the
    write: the write goes out, its read-back finds two holders, and the record is a
    conflict naming the human's object (never observed against an ambiguous identity)."""
    fake, record, saves, named = _publish_after_the_attempt_save(case)
    driven = drive(record, operation_for(record, fake), saves)

    assert saves.stages == [(Stage.ATTEMPTED, 1, 0), (Stage.CONFLICT, 1, 1)]
    assert driven.issued and named[0] in driven.record.reason
    assert "2 objects carry its identity" in driven.record.reason


@pytest.mark.parametrize("case", [K2, K7], ids=repr)
def test_a_human_pr_opened_on_the_head_after_the_attempt_save_is_a_conflict(case):
    """A human opens a PR on the head branch after the attempted save: GitHub refuses the
    controller's create (one open PR per head), and the record is a conflict after one
    read-back. Nothing is retried and the human's PR is the only one on the head. The
    conflict keeps the read-back's reason, so it names the human's PR as well."""
    fake, record, saves, named = _publish_after_the_attempt_save(case)
    driven = drive(record, operation_for(record, fake), saves)

    assert saves.stages == [(Stage.ATTEMPTED, 1, 0), (Stage.CONFLICT, 1, 1)]
    assert driven.issued and "GitHub refused the write" in driven.record.reason
    assert named[0] in driven.record.reason
    head = record.target["head"]
    assert [p.url for p in fake.prs.values() if p.head_ref == head] == named


@pytest.mark.parametrize("case", APPEND_CASES, ids=repr)
def test_an_append_target_closed_after_the_intent_is_a_conflict(case):
    """An append's target that no longer qualifies (closed) is a conflict, with no write."""
    fake, record = world_of(case)
    case.close_target(fake)
    saves = saves_for(fake)
    driven = drive(record, operation_for(record, fake), saves)

    assert saves.stages == [(Stage.CONFLICT, 0, 0)]
    assert "no longer meets the kind's conditions" in driven.record.reason
    assert fake.effect_writes == []


@pytest.mark.parametrize("case", APPEND_CASES, ids=repr)
def test_an_intended_append_is_rebased_onto_a_human_edit(case):
    """D5.5: a body a human edited (without the block's markers) before the first issue is
    the new base; the rebased payload is saved, with its digest, before the write."""
    fake, record = world_of(case)
    edited = "A human clarified the description."
    case.edit_target(fake, edited)
    saves = saves_for(fake)
    driven = drive(record, operation_for(record, fake), saves)

    assert saves.stages == [(Stage.ATTEMPTED, 1, 0), (Stage.OBSERVED, 1, 1)]
    rebased = saves.log[0][0]
    assert rebased.precondition["base_sha256"] == sha256_text(edited)
    assert rebased.body == compose_append(edited, record.payload["block"])
    assert case.landed(fake, driven.record) == [driven.record.observed["url"]]


@pytest.mark.parametrize("case", APPEND_CASES, ids=repr)
def test_an_append_is_not_rebased_onto_a_body_holding_a_credential(case):
    """D5.5: a rebase that would publish a credential is a conflict naming the pattern class
    only; the secret is not repeated and nothing is written."""
    fake, record = world_of(case)
    case.edit_target(fake, f"Deploy notes: {CREDENTIAL_TEXT}")
    saves = saves_for(fake)
    driven = drive(record, operation_for(record, fake), saves)

    assert saves.stages == [(Stage.CONFLICT, 0, 0)]
    assert "no rebase" in driven.record.reason and "env-assignment" in driven.record.reason
    assert SECRET not in driven.record.reason
    assert fake.effect_writes == []


# -- closed-object identity reads of the create kinds ----------------------------------


@pytest.mark.parametrize("case", CLOSABLE_CASES, ids=repr)
def test_a_created_object_a_human_closed_before_the_save_is_a_conflict(case):
    """The controller's create landed, the save did not, and a human closed the object.
    The identity read covers closed objects, so resuming is a conflict naming it, never
    a second create."""
    fake, record = world_of(case)
    op = operation_for(record, fake)
    crashing = saves_for(fake, crash_on=Stage.OBSERVED)
    with pytest.raises(Crash):
        drive(record, op, crashing)
    (created,) = case.landed(fake, record)
    case.close(fake, created)

    saves = saves_for(fake)
    driven = drive(crashing.last, op, saves)
    assert saves.stages == [(Stage.CONFLICT, 1, 1)]
    assert created in driven.record.reason and not driven.issued
    assert len(fake.effect_writes) == 1 and case.landed(fake, record) == [created]


# -- complete comment listings behind the real client (K4, K8) -------------------------


class CommentConversation:
    """`gh` for one issue's or PR's conversation, behind the real ``GitHubClient``.

    Serves the GraphQL comment walk at most 100 comments a page, by cursor,
    and the REST comment create, which appends a comment. ``lose_reply``
    makes the next create land and then fail as `gh` does on a lost reply
    (HTTP 502), an unknown outcome. Every other `gh` call fails the test.
    """

    PAGE = 100

    def __init__(self, parent_url: str, bodies: list[str]) -> None:
        self.parent_url = parent_url
        self.parent = "issue" if "/issues/" in parent_url else "pullRequest"
        self.comments: list[tuple[int, str]] = []
        self.pages_read = 0
        self.creates = 0
        self.lose_reply = False
        for body in bodies:
            self.add(body)

    def add(self, body: str) -> str:
        cid = 5000 + len(self.comments)
        self.comments.append((cid, body))
        return comment_url(self.parent_url, cid)

    def holding(self, body: str) -> list[str]:
        return [comment_url(self.parent_url, cid) for cid, b in self.comments if b == body]

    def __call__(self, req: ExecutionRequest) -> ExecutionResult:
        command = req.command
        if command[1:3] == ["api", "graphql"]:
            return self._page(command)
        if command[1:4] == ["api", "--method", "POST"]:
            assert req.stdin_data is not None
            self.creates += 1
            url = self.add(json.loads(req.stdin_data)["body"])
            if self.lose_reply:
                self.lose_reply = False
                return self._reply("", exit_code=1, stderr="HTTP 502: Bad Gateway")
            cid = int(url.rsplit("-", 1)[1])
            return self._reply(json.dumps({"id": cid, "html_url": url}))
        raise AssertionError(f"unexpected gh call: {command}")

    def _page(self, command: list[str]) -> ExecutionResult:
        self.pages_read += 1
        after = [a.removeprefix("after=") for a in command if a.startswith("after=")]
        start = int(after[0]) if after else 0
        rows = self.comments[start : start + self.PAGE]
        end = start + len(rows)
        has_next = end < len(self.comments)
        nodes = [
            {
                "url": comment_url(self.parent_url, cid),
                "body": body,
                "author": {"login": "someone"},
                "createdAt": "2026-01-01T00:00:00Z",
            }
            for cid, body in rows
        ]
        connection = {
            "pageInfo": {"hasNextPage": has_next, "endCursor": str(end) if has_next else None},
            "nodes": nodes,
        }
        data = {"data": {"repository": {self.parent: {"comments": connection}}}}
        return self._reply(json.dumps(data))

    @staticmethod
    def _reply(stdout: str, *, exit_code: int = 0, stderr: str = "") -> ExecutionResult:
        return ExecutionResult(
            command=["gh"],
            cwd=None,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            started_at="t",
            finished_at="t",
        )


# The comment kinds: the record, and the conversation its identity read lists.
COMMENT_KINDS = [
    pytest.param(review_record, PR, REVIEW_MARKER, id="K4-review-comment"),
    pytest.param(progress_record, EPIC, PROGRESS_MARKER, id="K8-progress-comment"),
]
FILLER = [f"Comment {n}, carrying no marker." for n in range(150)]


def conversation_client(parent_url: str, bodies: list[str]):
    conversation = CommentConversation(parent_url, bodies)
    return conversation, GitHubClient(runner=conversation, retry_delay_seconds=0)


def count_saves(log: list[EffectRecord]) -> Callable[[EffectRecord], None]:
    def persist(record: EffectRecord) -> None:
        assert EffectRecord.from_dict(json.loads(json.dumps(record.to_dict()))) == record
        log.append(record)

    return persist


@pytest.mark.parametrize(("build", "parent_url", "marker"), COMMENT_KINDS)
def test_a_comment_that_landed_past_the_first_hundred_is_reconciled_without_a_second_post(
    build, parent_url, marker
):
    """#160 R1-F1, post-write recovery: the create landed, the process died before
    the save, and the conversation had grown past one page. The comment is the
    151st; the identity read walks every page and adopts it, no second post."""
    record = build()
    conversation, github = conversation_client(parent_url, FILLER)
    landed = conversation.add(record.body)
    conversation.add("A later comment.")
    attempted = record.attempting()  # the save that preceded the lost write

    saves: list[EffectRecord] = []
    driven = drive(attempted, operation_for(attempted, github), count_saves(saves))

    assert not driven.issued and conversation.creates == 0
    assert [(r.stage, r.attempts) for r in saves] == [(Stage.OBSERVED, 1)]
    assert driven.record.observed == {"url": landed}
    assert conversation.pages_read == 2


@pytest.mark.parametrize(("build", "parent_url", "marker"), COMMENT_KINDS)
def test_a_lost_reply_is_read_back_past_the_first_hundred_comments(build, parent_url, marker):
    """#160 R1-F1: the controller's create lands as the 151st comment and its reply
    is lost. The read-back that decides the unknown outcome walks every page,
    so it observes the comment: one post, never a second."""
    record = build()
    conversation, github = conversation_client(parent_url, FILLER)
    conversation.lose_reply = True

    saves: list[EffectRecord] = []
    driven = drive(record, operation_for(record, github), count_saves(saves))

    assert driven.issued and conversation.creates == 1
    assert [(r.stage, r.attempts) for r in saves] == [(Stage.ATTEMPTED, 1), (Stage.OBSERVED, 1)]
    (landed,) = conversation.holding(record.body)
    assert driven.record.observed == {"url": landed}
    assert landed == comment_url(parent_url, 5150)


@pytest.mark.parametrize(("build", "parent_url", "marker"), COMMENT_KINDS)
def test_a_marker_comment_past_the_first_hundred_is_seen_before_the_first_post(
    build, parent_url, marker
):
    """#160 R1-F1: the precondition (no comment carries the identity) is judged on the
    whole conversation. A comment carrying the marker past the first page is
    a conflict naming it, and nothing is posted."""
    record = build()
    conversation, github = conversation_client(parent_url, FILLER)
    other = conversation.add(f"Someone else's comment.\n\n{marker}")

    saves: list[EffectRecord] = []
    driven = drive(record, operation_for(record, github), count_saves(saves))

    assert not driven.issued and conversation.creates == 0
    assert [r.stage for r in saves] == [Stage.CONFLICT]
    assert other in driven.record.reason


@pytest.mark.parametrize(("build", "parent_url", "marker"), COMMENT_KINDS)
def test_a_comment_listing_that_cannot_reach_its_end_never_reads_as_absent(
    build, parent_url, marker
):
    """#160 R1-F1: a conversation whose second page cannot be read is not a
    conversation without the marker: the identity read raises, nothing is
    saved, charged or posted."""
    record = build()
    conversation, github = conversation_client(parent_url, FILLER)
    serve = conversation.__call__

    def second_page_fails(req: ExecutionRequest) -> ExecutionResult:
        if any(a.startswith("after=") for a in req.command):
            return CommentConversation._reply("", exit_code=1, stderr="HTTP 502: Bad Gateway")
        return serve(req)

    github = GitHubClient(runner=second_page_fails, retry_delay_seconds=0)
    saves: list[EffectRecord] = []
    with pytest.raises(GitHubUnavailableError, match="502"):
        drive(record, operation_for(record, github), count_saves(saves))
    assert saves == [] and conversation.creates == 0


# -- K1: push, against a local bare repository -----------------------------------------

IDENT = ("-c", "user.name=t", "-c", "user.email=t@example.com")


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def _run_git(*args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], env=_clean_env(), input=input, capture_output=True, check=False
    )


def git(*args: str, input: bytes | None = None) -> str:
    res = _run_git(*args, input=input)
    assert res.returncode == 0, res.stderr.decode(errors="replace")
    return res.stdout.decode().strip()


@dataclass
class Remote:
    """A bare repository as the remote (``file://``) and the shared repository whose
    object store the transport reads. Every push the transport runs is recorded."""

    shared: Path
    bare: Path
    gh: Path
    requests: list[ExecutionRequest] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"file://{self.bare}"

    def g(self, *args: str, input: bytes | None = None) -> str:
        return git("-C", str(self.shared), *IDENT, *args, input=input)

    def commit(self, message: str, *parents: str) -> str:
        argv = ["commit-tree", self.g("mktree", input=b"")]
        for parent in parents:
            argv += ["-p", parent]
        return self.g(*argv, "-m", message)

    def publish(self, sha: str, branch: str) -> None:
        """Someone else's push (the test's, or a human's): not the transport's."""
        self.g("push", "-q", self.url, f"{sha}:refs/heads/{branch}")

    def head(self, branch: str) -> str | None:
        res = _run_git(
            f"--git-dir={self.bare}", "rev-parse", "-q", "--verify", f"refs/heads/{branch}"
        )
        return res.stdout.decode().strip() if res.returncode == 0 else None

    def in_base_history(self, base: str, sha: str) -> bool:
        return (
            _run_git(f"--git-dir={self.bare}", "merge-base", "--is-ancestor", sha, base).returncode
            == 0
        )

    def runner(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        return execute(request)

    def transport(
        self, runner: Callable[[ExecutionRequest], ExecutionResult] | None = None
    ) -> GitTransport:
        return GitTransport(
            object_directory=self.shared / ".git" / "objects",
            remote=GitRemote(url=self.url),
            in_base_history=self.in_base_history,
            gh_command=str(self.gh),
            runner=runner or self.runner,
            git_lock=lambda op: SharedGitLock(self.shared / ".git", operation=op),
        )

    def pushes(self) -> int:
        return sum(1 for r in self.requests if "push" in r.command)


class BranchReads:
    """K1's one GitHub read, answered from the bare remote as GitHub answers it."""

    def __init__(self, remote: Remote) -> None:
        self.remote = remote
        self.down = False

    def get_branch_head_sha(self, repository: str, branch: str) -> str:
        if self.down:
            raise GitHubUnavailableError("`gh api` failed: connection reset")
        sha = self.remote.head(branch)
        if sha is None:
            raise GitHubNotFoundError(f"branch not found: {branch}")
        return sha


def lost_push(remote: Remote, *, lands: bool) -> Callable[[ExecutionRequest], ExecutionResult]:
    """A runner whose ``git push`` times out: after running (``lands``) or before."""

    def runner(request: ExecutionRequest) -> ExecutionResult:
        if "push" not in request.command:
            return remote.runner(request)
        remote.requests.append(request)
        if lands:
            return replace(execute(request), timed_out=True)
        return ExecutionResult(request.command, request.cwd, -9, "", "", "t", "t", timed_out=True)

    return runner


@pytest.fixture
def remote(tmp_path, monkeypatch) -> Remote:
    private = tmp_path / "private-tmp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    shared = tmp_path / "shared"
    bare = tmp_path / "remote.git"
    git("init", "-q", "-b", "main", str(shared))
    git("init", "-q", "--bare", "-b", "main", str(bare))
    git("-C", str(shared), "config", "gc.auto", "0")
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text("#!/bin/sh\ncat >/dev/null\nprintf 'username=x-access-token\\npassword=x\\n'\n")
    gh.chmod(0o755)
    return Remote(shared=shared, bare=bare, gh=gh)


@dataclass
class Branch:
    """The PR branch at ``head`` (on ``base``), and a fix ``candidate`` on top of it."""

    base: str
    head: str
    candidate: str
    record: EffectRecord


@pytest.fixture
def branch(remote: Remote) -> Branch:
    base = remote.commit("base")
    remote.publish(base, "main")
    head = remote.commit("feature work", base)
    remote.publish(head, BRANCH)
    candidate = remote.commit("fix the findings", head)
    record = push_record(candidate=candidate, expected_old=head, base=head)
    return Branch(base, head, candidate, record)


def push_op(remote: Remote, reads: BranchReads, **kwargs: Any) -> PushOp:
    return PushOp(reads, remote.transport(**kwargs), "main")  # type: ignore[arg-type]


def push_saves(remote: Remote, **kwargs: Any) -> Saves:
    return Saves(remote.pushes, **kwargs)


def test_push_fast_forwards_the_branch_after_saving_the_attempt(remote, branch):
    """K1 success: attempted is saved before the push; the remote ref is then the candidate."""
    saves = push_saves(remote)
    driven = drive(branch.record, push_op(remote, BranchReads(remote)), saves)

    assert saves.stages == [(Stage.ATTEMPTED, 1, 0), (Stage.OBSERVED, 1, 1)]
    assert driven.issued and driven.record.observed == {"sha": branch.candidate}
    assert remote.head(BRANCH) == branch.candidate


def test_push_creates_an_absent_branch(remote):
    """K1 with ``expected_old=None``: the branch must not exist, and the push creates it."""
    base = remote.commit("base")
    remote.publish(base, "main")
    candidate = remote.commit("implement #2", base)
    record = push_record(candidate=candidate, expected_old=None, base=base, by=ANALYZE_OWNER)
    saves = push_saves(remote)
    driven = drive(record, push_op(remote, BranchReads(remote)), saves)

    assert saves.stages == [(Stage.ATTEMPTED, 1, 0), (Stage.OBSERVED, 1, 1)]
    assert driven.record.observed == {"sha": candidate} and remote.head(BRANCH) == candidate


def test_push_already_at_the_candidate_is_observed_without_a_push(remote, branch):
    """The ref already at the candidate is the write: observed, nothing pushed."""
    remote.publish(branch.candidate, BRANCH)
    saves = push_saves(remote)
    driven = drive(branch.record, push_op(remote, BranchReads(remote)), saves)

    assert saves.stages == [(Stage.OBSERVED, 0, 0)]
    assert not driven.issued and remote.pushes() == 0


def test_push_over_a_human_push_is_a_conflict_naming_both_shas(remote, branch):
    """The ref moved to neither the expected head nor the candidate: conflict, no push."""
    human = remote.commit("a human pushed", branch.head)
    remote.publish(human, BRANCH)
    saves = push_saves(remote)
    driven = drive(branch.record, push_op(remote, BranchReads(remote)), saves)

    assert saves.stages == [(Stage.CONFLICT, 0, 0)]
    for name in (f"refs/heads/{BRANCH}", human, branch.head, branch.candidate):
        assert name in driven.record.reason
    assert remote.pushes() == 0 and remote.head(BRANCH) == human


def test_push_that_is_not_a_fast_forward_is_a_conflict_before_any_attempt(remote, branch):
    """D6.2: a candidate that does not descend from the expected head is refused before an
    attempt is charged, and nothing is pushed."""
    unrelated = remote.commit("unrelated", branch.base)
    record = push_record(candidate=unrelated, expected_old=branch.head, base=branch.base)
    saves = push_saves(remote)
    driven = drive(record, push_op(remote, BranchReads(remote)), saves)

    assert saves.stages == [(Stage.CONFLICT, 0, 0)]
    assert "does not descend" in driven.record.reason and unrelated in driven.record.reason
    assert remote.pushes() == 0 and remote.head(BRANCH) == branch.head


def test_push_to_the_default_branch_is_a_conflict(remote, branch):
    """The default branch changes only through a reviewed PR: conflict, nothing pushed."""
    record = push_record(
        candidate=branch.candidate, expected_old=branch.base, base=branch.base, branch="main"
    )
    saves = push_saves(remote)
    driven = drive(record, push_op(remote, BranchReads(remote)), saves)

    assert saves.stages == [(Stage.CONFLICT, 0, 0)]
    assert "is the default branch" in driven.record.reason
    assert remote.pushes() == 0 and remote.head("main") == branch.base


def test_push_whose_lease_is_rejected_is_a_conflict_not_retried(remote, branch):
    """A human pushes between the attempted save and the push: git's lease refuses it, and
    the record is a conflict after one read-back; the human's commit stays."""
    human = remote.commit("a human pushed", branch.head)

    def race(saved: EffectRecord) -> None:
        if saved.stage is Stage.ATTEMPTED:
            remote.publish(human, BRANCH)

    saves = push_saves(remote, hook=race)
    driven = drive(branch.record, push_op(remote, BranchReads(remote)), saves)

    assert saves.stages == [(Stage.ATTEMPTED, 1, 0), (Stage.CONFLICT, 1, 1)]
    assert "GitHub refused the write" in driven.record.reason
    assert remote.head(BRANCH) == human


def test_push_with_an_unknown_outcome_that_did_not_land_is_reissued_once(remote, branch):
    """W3 for K1: the push timed out before it ran. The ref is still at the expected head,
    so the next drive pushes once more (attempt 2) and observes the candidate."""
    reads = BranchReads(remote)
    saves = push_saves(remote)
    with pytest.raises(EffectPending):
        drive(branch.record, push_op(remote, reads, runner=lost_push(remote, lands=False)), saves)
    assert saves.stages == [(Stage.ATTEMPTED, 1, 0)]
    assert remote.head(BRANCH) == branch.head

    driven = drive(saves.last, push_op(remote, reads), saves)
    assert saves.stages[1:] == [(Stage.ATTEMPTED, 2, 1), (Stage.OBSERVED, 2, 2)]
    assert driven.issued and remote.head(BRANCH) == branch.candidate


def test_push_that_landed_with_its_read_back_lost_is_reconciled_next(remote, branch):
    """W4 for K1: the push landed but timed out, and the branch read failed. The record
    stays attempted; the next drive observes the candidate and pushes nothing."""
    reads = BranchReads(remote)

    def lose_read_back(saved: EffectRecord) -> None:
        reads.down = saved.stage is Stage.ATTEMPTED

    saves = push_saves(remote, hook=lose_read_back)
    with pytest.raises(EffectPending):
        drive(branch.record, push_op(remote, reads, runner=lost_push(remote, lands=True)), saves)
    assert saves.stages == [(Stage.ATTEMPTED, 1, 0)]
    assert remote.head(BRANCH) == branch.candidate

    reads.down = False
    driven = drive(saves.last, push_op(remote, reads), saves)
    assert saves.stages[-1] == (Stage.OBSERVED, 1, 1)
    assert not driven.issued and remote.pushes() == 1


def test_push_crash_after_the_push_before_the_save_resumes_without_a_second_push(remote, branch):
    """Driving the persisted attempted record after a crash observes the pushed candidate."""
    op = push_op(remote, BranchReads(remote))
    crashing = push_saves(remote, crash_on=Stage.OBSERVED)
    with pytest.raises(Crash):
        drive(branch.record, op, crashing)

    saves = push_saves(remote)
    driven = drive(crashing.last, op, saves)
    assert saves.stages == [(Stage.OBSERVED, 1, 1)]
    assert not driven.issued and remote.pushes() == 1


def test_push_observed_record_is_never_read_or_pushed_again(remote, branch):
    """A duplicate invocation of an observed push reads nothing and pushes nothing."""
    reads = BranchReads(remote)
    observed = drive(branch.record, push_op(remote, reads), push_saves(remote)).record
    reads.down = True
    saves = push_saves(remote)
    again = drive(observed, push_op(remote, reads), saves)

    assert again.record is observed and not again.issued
    assert saves.log == [] and remote.pushes() == 1


def test_push_with_its_bound_spent_is_a_conflict_naming_the_manual_step(remote, branch):
    """Two attempts spent and the ref still at the expected head: conflict, no third push."""
    spent = branch.record.attempting().attempting()
    saves = push_saves(remote)
    driven = drive(spent, push_op(remote, BranchReads(remote)), saves)

    assert saves.stages == [(Stage.CONFLICT, 2, 0)]
    assert branch.record.describe() in driven.record.reason
    assert "perform the write by hand" in driven.record.reason
    assert remote.pushes() == 0


def test_push_unreadable_branch_before_the_push_charges_nothing(remote, branch):
    """A transient branch read failure raises before anything is saved or pushed."""
    reads = BranchReads(remote)
    reads.down = True
    saves = push_saves(remote)
    with pytest.raises(GitHubUnavailableError):
        drive(branch.record, push_op(remote, reads), saves)
    assert saves.log == [] and remote.pushes() == 0


def test_push_has_no_operation_without_the_transport_or_the_default_branch(remote, branch):
    """K1 is pushed through the controller's transport only, and never blind to the default
    branch: leaving either out is a conflict naming the record."""
    fake = FakeGitHub()
    transport = remote.transport()
    with pytest.raises(EffectConflictError, match=branch.record.describe()):
        operation_for(branch.record, fake, default_branch="main")
    with pytest.raises(EffectConflictError, match="git transport and the default branch"):
        operation_for(branch.record, fake, transport=transport)
    assert type(operation_for(branch.record, fake, transport=transport, default_branch="main")) is (
        PushOp
    )
    with pytest.raises(GitTransportError, match="default branch must be named"):
        PushOp(fake, transport, "")


# -- K9: the acceptance comment (#211) ---------------------------------------------------


def test_the_acceptance_action_id_names_the_owner_the_target_and_the_exact_bytes():
    """The id is recomputed from the record: the same plan is the same id in
    any process, and another body, issue, PR, round, finding or run is
    another id. An approval is never carried to other bytes."""
    record = acceptance_record(authorization="operator")
    again = EffectRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert record.action_id == again.action_id
    assert record.action_id.startswith("A-") and len(record.action_id) == 18
    others = [
        acceptance_record(authorization="operator", text=ACCEPTANCE_TEXT + "!"),
        acceptance_record(authorization="operator", by=owner(Phase.FIX, pr_url=PR, run_id="r2")),
    ]
    assert len({record.action_id, *(o.action_id for o in others)}) == 3
    identity, target, payload = record.identity, record.target, record.payload
    other_round = {**identity, "marker": render_acceptance_marker(ISSUE, PR, 2, "R2-F1")}
    other_issue = {"issue_url": ISSUE3}
    assert acceptance_action_id(FIX_OWNER, identity, target, payload) == record.action_id
    assert acceptance_action_id(FIX_OWNER, other_round, target, payload) != record.action_id
    assert acceptance_action_id(FIX_OWNER, identity, other_issue, payload) != record.action_id
    assert push_record().action_id == ""


def test_an_acceptance_comment_awaiting_the_operator_is_held_with_nothing_sent():
    """Without an approval the write is held: nothing is saved or sent, the
    record is unchanged, and the reason names the action id. The record
    itself refuses to become attempted."""
    fake = FakeGitHub()
    fake.add_issue(ISSUE, "Ship the feature")
    record = acceptance_record(authorization="operator")
    saves = saves_for(fake)
    driven = drive(record, AcceptanceCommentOp(fake), saves)
    assert driven.record == record and not driven.issued
    assert f"action {record.action_id} awaits the operator's approval" in driven.held
    assert saves.log == [] and fake.effect_writes == []
    with pytest.raises(StateError, match="is not authorized; not issued"):
        record.attempting()


def test_an_approval_records_the_recomputed_id_while_intended_only():
    record = acceptance_record(authorization="operator")
    approved = record.approving()
    assert approved.authorized and not record.authorized
    assert approved.precondition == {
        "absent": True,
        "authorization": "operator",
        "approved_action_id": record.action_id,
    }
    assert approved.action_id == record.action_id
    with pytest.raises(StateError, match="cannot be approved"):
        approved.attempting().approving()
    with pytest.raises(StateError, match="cannot be approved"):
        push_record().approving()


@pytest.mark.parametrize("state", ["CLOSED", "ELSEWHERE"])
def test_an_authorized_acceptance_comment_is_held_while_the_issue_does_not_qualify(state):
    fake = FakeGitHub()
    info = fake.add_issue(
        ISSUE, "Ship the feature", state="CLOSED" if state == "CLOSED" else "OPEN"
    )
    if state == "ELSEWHERE":
        fake.issues[ISSUE] = replace(info, repository="other/repo")
    record = acceptance_record()
    saves = saves_for(fake)
    driven = drive(record, AcceptanceCommentOp(fake), saves)
    assert driven.record == record and saves.log == [] and fake.effect_writes == []
    assert ("is closed, not open" if state == "CLOSED" else "not in the run's") in driven.held


@pytest.mark.parametrize("authorization", ["operator", "policy"])
def test_an_acceptance_comment_already_on_the_issue_is_adopted_whoever_posted_it(authorization):
    """The exact payload on the issue is the write, approved or not: an
    operator may post it instead of approving."""
    fake = FakeGitHub()
    fake.add_issue(ISSUE, "Ship the feature")
    record = acceptance_record(authorization=authorization)
    url = fake.add_comment(ISSUE, 300, record.body).url
    driven = drive(record, AcceptanceCommentOp(fake), saves_for(fake))
    assert driven.record.stage is Stage.OBSERVED and driven.record.observed == {"url": url}
    assert not driven.held and fake.effect_writes == []


@pytest.mark.parametrize("state", ["CLOSED", "ELSEWHERE"])
@pytest.mark.parametrize("stage", ["awaiting", "by policy", "attempted"])
def test_an_exact_acceptance_comment_is_not_adopted_while_the_issue_does_not_qualify(state, stage):
    """PR #221 R1-F2: the issue's eligibility holds an adoption as it holds
    a write. The exact payload on a closed issue, or on one no longer in the
    run's repository, is not adopted, whoever posted it and whatever the
    record's stage: the record is held unchanged, nothing is saved or sent,
    and the hold names the comment, so the plan's later writes wait too."""
    fake = FakeGitHub()
    info = fake.add_issue(
        ISSUE, "Ship the feature", state="CLOSED" if state == "CLOSED" else "OPEN"
    )
    if state == "ELSEWHERE":
        fake.issues[ISSUE] = replace(info, repository="other/repo")
    if stage == "awaiting":
        record = acceptance_record(authorization="operator")
    elif stage == "by policy":
        record = acceptance_record()
    else:
        record = acceptance_record().attempting()
    url = fake.add_comment(ISSUE, 300, record.body).url
    saves = saves_for(fake)
    driven = drive(record, AcceptanceCommentOp(fake), saves)
    assert driven.record == record and not driven.issued
    assert saves.log == [] and fake.effect_writes == []
    assert driven.held_url == url and f"its exact payload is on {url}" in driven.held
    assert ("is closed, not open" if state == "CLOSED" else "not in the run's") in driven.held
    assert "is not adopted while the issue does not qualify" in driven.held


def _stop_qualifying_after_the_comment(fake: FakeGitHub, state: str) -> None:
    """The run's issue is closed, or leaves the run's repository, as the comment
    write returns, before its read-back (PR #221 R2-F1)."""

    def after() -> None:
        info = fake.issues[ISSUE]
        fake.issues[ISSUE] = (
            replace(info, state="CLOSED")
            if state == "CLOSED"
            else replace(info, repository="other/repo")
        )

    fake.after_issue_comment = after


@pytest.mark.parametrize("state", ["CLOSED", "ELSEWHERE"])
def test_an_acceptance_comment_whose_issue_stops_qualifying_before_the_read_back_is_held(state):
    """PR #221 R2-F1: the comment lands, and the issue is closed or leaves the
    run's repository before the read-back. That is a conclusive hold, not a
    write still propagating: drive returns it with the comment, marked
    issued, and the record stays `attempted` as saved before the write. The
    next drive sends nothing while the issue does not qualify, and adopts
    the one comment once it does again."""
    fake = FakeGitHub()
    fake.add_issue(ISSUE, "Ship the feature")
    _stop_qualifying_after_the_comment(fake, state)
    record = acceptance_record()
    saves = saves_for(fake)
    driven = drive(record, AcceptanceCommentOp(fake), saves)
    [comment] = fake.comments[ISSUE]
    assert driven.issued and driven.held_url == comment.url
    assert driven.record == record.attempting()
    assert saves.stages == [(Stage.ATTEMPTED, 1, 0)] and len(fake.effect_writes) == 1
    assert f"its exact payload is on {comment.url}" in driven.held
    assert ("is closed, not open" if state == "CLOSED" else "not in the run's") in driven.held
    again = drive(driven.record, AcceptanceCommentOp(fake), saves)
    assert again.held == driven.held and not again.issued and again.record == driven.record
    assert len(saves.log) == 1 and len(fake.effect_writes) == 1
    fake.issues[ISSUE] = replace(fake.issues[ISSUE], state="OPEN", repository="owner/repo")
    adopted = drive(driven.record, AcceptanceCommentOp(fake), saves)
    assert adopted.record.stage is Stage.OBSERVED and adopted.record.observed == {
        "url": comment.url
    }
    assert not adopted.issued and len(fake.effect_writes) == 1
    assert fake.comments[ISSUE] == [comment]


@pytest.mark.parametrize("state", ["CLOSED", "ELSEWHERE"])
def test_an_acceptance_write_not_read_back_on_an_issue_that_stopped_qualifying_is_held(state):
    """PR #221 R2-F1: the write's reply is lost and its comment is not on the
    issue (it never landed, or is not visible yet) when the issue stops
    qualifying. Held, `attempted` and marked issued, with a reason that says
    the attempt was sent rather than that nothing was."""
    fake = FakeGitHub()
    fake.add_issue(ISSUE, "Ship the feature")
    fake.write_failures.append(("create_issue_comment", GitHubUnavailableError("timed out"), False))
    _stop_qualifying_after_the_comment(fake, state)
    record = acceptance_record()
    saves = saves_for(fake)
    driven = drive(record, AcceptanceCommentOp(fake), saves)
    assert driven.issued and driven.held_url == "" and fake.comments.get(ISSUE, []) == []
    assert driven.record == record.attempting()
    assert saves.stages == [(Stage.ATTEMPTED, 1, 0)] and len(fake.effect_writes) == 1
    assert ("is closed, not open" if state == "CLOSED" else "not in the run's") in driven.held
    assert "the comment of attempt 1 is not on the issue" in driven.held
    assert "nothing was sent" not in driven.held


def test_a_refused_acceptance_write_on_an_issue_that_stopped_qualifying_is_a_conflict():
    """A write GitHub refused is never retried, also when the issue stopped
    qualifying meanwhile: a conflict that names the refusal and the issue."""
    fake = FakeGitHub()
    fake.add_issue(ISSUE, "Ship the feature")
    fake.write_failures.append(("create_issue_comment", GitHubError("HTTP 403: locked"), False))
    _stop_qualifying_after_the_comment(fake, "CLOSED")
    saves = saves_for(fake)
    driven = drive(acceptance_record(), AcceptanceCommentOp(fake), saves)
    assert driven.issued and not driven.held and driven.record.stage is Stage.CONFLICT
    assert "GitHub refused the write" in driven.record.reason
    assert f"issue {ISSUE} is closed, not open" in driven.record.reason
    assert saves.stages == [(Stage.ATTEMPTED, 1, 0), (Stage.CONFLICT, 1, 1)]


def test_decide_acceptance_holds_only_a_due_write():
    """D4.2 decides first: an exact holder is adopted, any other holder is a
    conflict, a spent bound is a conflict; only then is a due write held."""
    waiting = acceptance_record(authorization="operator")
    exact = Found(url=comment_url(ISSUE, 1), in_end_state=True, observed={"url": "u"})
    edited = Found(url=comment_url(ISSUE, 2), in_end_state=False, observed={"url": "v"})
    assert decide_acceptance(waiting, [exact]).action is Action.OBSERVE
    assert decide_acceptance(waiting, [edited]).action is Action.CONFLICT
    assert decide_acceptance(waiting, [exact, edited]).action is Action.CONFLICT
    assert decide_acceptance(waiting, []).action is Action.HOLD
    authorized = acceptance_record()
    assert decide_acceptance(authorized, []).action is Action.ISSUE
    held = decide_acceptance(authorized, [], issue_problem="issue x is closed, not open")
    assert held.action is Action.HOLD and "issue x is closed" in held.reason
    assert held.observed is None and "nothing was sent" in held.reason
    # PR #221 R2-F1: an attempted record's write was sent, and its hold says so.
    sent = decide_acceptance(
        authorized.attempting(), [], issue_problem="issue x is closed, not open"
    )
    assert sent.action is Action.HOLD and "nothing was sent" not in sent.reason
    assert "issue x is closed, not open; the comment of attempt 1 is not on the issue" in (
        sent.reason
    )
    spent = authorized.attempting().attempting()
    assert decide_acceptance(spent, []).exhausted
    # PR #221 R1-F2: an exact holder is adopted only while the issue qualifies;
    # otherwise the hold carries it. A conflict stays a conflict.
    for record in (waiting, authorized, spent):
        kept = decide_acceptance(record, [exact], issue_problem="issue x is closed, not open")
        assert kept.action is Action.HOLD and kept.observed == {"url": "u"}
        assert "its exact payload is on u, but issue x is closed" in kept.reason
    closed = decide_acceptance(waiting, [edited], issue_problem="issue x is closed, not open")
    assert closed.action is Action.CONFLICT


def _acceptance_context(source: int | None = 0, fid: str = "R1-F1") -> dict:
    return {
        **FIX_CONTEXT,
        "resolutions": [
            {
                "finding_id": fid,
                "resolution": "acceptance_action",
                "rationale": "The finding asks for evidence on the issue, not for a change.",
                "commit_sha": "",
                "follow_up_source": None,
                "acceptance_source": source,
                "operator_sources": None,
            }
        ],
    }


def test_a_plan_with_an_acceptance_comment_loads_with_it_first_and_alone():
    """K9 is first in the plan (nothing is sent before the write that may
    wait) and there is at most one; the context names it from exactly the
    finding it resolves."""
    plan = [acceptance_record().to_dict(), push_record(position=1).to_dict()]
    binding = replace(FIX_BINDING, open_finding_ids=("R1-F1",))
    loaded = load_phase_effects(plan, {}, _acceptance_context(), binding)
    assert [r.kind for r in loaded.records] == [EffectKind.ACCEPTANCE_COMMENT, EffectKind.PUSH]
    with pytest.raises(StateError, match="plans an acceptance comment after another effect"):
        load_records(
            [push_record().to_dict(), acceptance_record(position=1).to_dict()], FIX_BINDING
        )
    with pytest.raises(StateError, match="plans more than one acceptance comment"):
        load_records(
            [acceptance_record().to_dict(), acceptance_record(position=1).to_dict()],
            FIX_BINDING,
        )
    with pytest.raises(StateError, match="no resolution of the context names"):
        load_phase_effects(plan, {}, FIX_CONTEXT, FIX_BINDING)
    with pytest.raises(StateError, match="which is not an acceptance comment"):
        load_phase_effects(plan, {}, _acceptance_context(1), binding)
    with pytest.raises(StateError, match="another finding's or round's"):
        load_phase_effects(
            plan,
            {},
            _acceptance_context(0, "R1-F2"),
            replace(FIX_BINDING, open_finding_ids=("R1-F2",)),
        )
    with pytest.raises(StateError, match="is set exactly when"):
        load_phase_effects(plan, {}, _acceptance_context(None), binding)


def test_the_largest_plan_with_an_acceptance_comment_loads():
    """One acceptance comment, one follow-up per other finding and the push
    still fit the record bound."""
    records = [acceptance_record()]
    records += [
        follow_up_record(finding_id=f"R1-F{i}", position=i - 1)
        for i in range(2, MAX_EFFECTS_PER_PLAN)
    ]
    records.append(push_record(position=MAX_EFFECTS_PER_PLAN - 1))
    assert len(load_records([r.to_dict() for r in records], FIX_BINDING)) == MAX_EFFECTS_PER_PLAN


# -- K10: the operator comment (#243) ------------------------------------------------------
#
# The operator posts a comment on another issue of the run's repository and the
# controller only verifies it: a K10 record is never attempted, driven or
# issued, it leads its plan, and its verification reads the target's complete
# comment listing (spec section 7.4).

ISSUE42 = "https://github.com/owner/repo/issues/42"  # the run's PR number, as an issue URL
OTHER_REPO_ISSUE3 = "https://github.com/other/repo/issues/3"
OPERATOR_TEXT = "Confirmed on the reviewed HEAD: the schema migration #2 depends on is applied."
OPERATOR_ISSUE_BODY = "Depends on #3 and #4."
OPERATOR_BODY_SHA = sha256_text(OPERATOR_ISSUE_BODY)
OTHER_DIGEST = "d" * 64
EVIDENCE_TEXT = "Checked the migration by hand on staging; it is applied."
K10_COMPLETION = "operator_comment_exact_or_bound_in_complete_listing"
FIVE_TARGETS = (ISSUE3, ISSUE4, EPIC, ISSUE43, ISSUE44)


def operator_dict(
    *,
    target: str = ISSUE3,
    finding_id: str = "R1-F1",
    round_: int = 1,
    text: str = OPERATOR_TEXT,
    position: int = 0,
    by: EffectOwner = FIX_OWNER,
    allowed_by: tuple[str, ...] = ("issue_body",),
    issue_body_sha256: str = OPERATOR_BODY_SHA,
    binding: dict | None = None,
    marker: str | None = None,
) -> dict:
    """A K10 record as persisted, built without validation so a test can make it inconsistent.

    Everything is rendered as the FIX planner renders it (``_operator_plan``):
    the marker for (owner issue, owner PR, round, finding, target) and the
    payload from the agent's ``text``; ``marker`` replaces the identity's marker.
    """
    rendered = render_operator_comment_marker(by.issue_url, by.pr_url, round_, finding_id, target)
    return EffectRecord(
        position=position,
        kind=EffectKind.OPERATOR_COMMENT,
        owner=by,
        identity={"issue_url": target, "marker": rendered if marker is None else marker},
        target={"issue_url": target},
        precondition={
            "allowed_by": list(allowed_by),
            "issue_body_sha256": issue_body_sha256,
            "binding": binding,
        },
        payload={
            "body": operator_comment_body(text, by.issue_url, by.pr_url, round_, finding_id, target)
        },
        completion=K10_COMPLETION,
    ).to_dict()


def operator_record(**kwargs: Any) -> EffectRecord:
    """K10 (#243): a valid record, loaded as recovery loads it."""
    return EffectRecord.from_dict(operator_dict(**kwargs))


def exact_evidence(record: EffectRecord, cid: int = 300) -> dict:
    """What an exact K10 observation of ``record``'s payload as comment ``cid`` holds."""
    return {
        "url": comment_url(record.target["issue_url"], cid),
        "comment_id": cid,
        "evidence": "exact",
        "body_sha256": sha256_text(record.body),
    }


def evidence_binding(cid: int = 400, body: str = EVIDENCE_TEXT, target: str = ISSUE3) -> dict:
    """The operator's binding of comment ``cid`` with ``body`` on ``target``."""
    return {
        "comment_url": comment_url(target, cid),
        "comment_id": cid,
        "body_sha256": sha256_text(body),
    }


K10_BINDING = evidence_binding()
K10_EXACT = exact_evidence(EffectRecord.from_dict(operator_dict()))
K10_BOUND_EVIDENCE = {
    "url": K10_BINDING["comment_url"],
    "comment_id": K10_BINDING["comment_id"],
    "evidence": "bound",
    "body_sha256": K10_BINDING["body_sha256"],
}


def operator_targets(
    numbers: Any = (3, 4, 1),
    *,
    allowed: dict[int, list[str]] | None = None,
    digest: str = OPERATOR_BODY_SHA,
) -> dict:
    """A FIX entry's allowed set as persisted: #3 and #4 from the issue body, the EPIC (#1)."""
    numbers = list(numbers)
    if allowed is None:
        allowed = {n: ["epic"] if n == 1 else ["issue_body"] for n in numbers}
    return {
        "numbers": numbers,
        "allowed_by": {str(n): list(v) for n, v in allowed.items()},
        "issue_body_sha256": digest,
    }


def operator_observation(targets: dict | None = None, *, phase: str = "FIX") -> dict:
    """A FIX entry observation recording ``targets`` (the default set) as its allowed set."""
    data: dict[str, Any] = {
        "phase": phase,
        "issue_url": ISSUE,
        "pr_url": PR,
        "refs": {},
        "base_sha": None,
        "objects": {},
    }
    data["operator_targets"] = operator_targets() if targets is None else targets
    return data


def fix_resolution(
    fid: str,
    resolution: str,
    *,
    follow_up: int | str | None = None,
    operator: Any = None,
) -> dict:
    return {
        "finding_id": fid,
        "resolution": resolution,
        "rationale": f"Why {fid} is resolved this way.",
        "commit_sha": "",
        "follow_up_source": follow_up,
        "acceptance_source": None,
        "operator_sources": operator,
    }


def k10_resolution(fid: str = "R1-F1", sources: Any = (0, 1), kind: str = "operator_acceptance"):
    """The resolution of ``fid`` naming its K10 records (``sources``) by plan position."""
    return fix_resolution(fid, kind, operator=None if sources is None else list(sources))


# The plan the K10 load cases start from: two operator comments for R1-F1 (on
# #3, allowed by the issue body, and on the EPIC), then the push.
K10_PLAN = [
    operator_record(),
    operator_record(target=EPIC, position=1, allowed_by=("epic",)),
    push_record(position=2),
]


def _k10_case(**change: Any) -> tuple[list[dict], dict, dict, Binding]:
    """``load_phase_effects``'s arguments for :data:`K10_PLAN`, with ``change`` applied."""
    case: dict[str, Any] = {
        "records": K10_PLAN,
        "observation": operator_observation(),
        "resolutions": [k10_resolution()],
        "open_ids": ("R1-F1",),
        **change,
    }
    return (
        [r.to_dict() for r in case["records"]],
        case["observation"],
        {**FIX_CONTEXT, "resolutions": case["resolutions"]},
        replace(FIX_BINDING, open_finding_ids=case["open_ids"]),
    )


# -- K10: the record ----------------------------------------------------------------------


def test_an_operator_comment_round_trips_in_every_stage_it_can_hold():
    """Intended, bound, observed (exact or bound evidence) and conflict are
    inverse through JSON; ``plan`` validates as a load does; only K10 is
    operator-performed. A reopened conflict is intended again, never attempted."""
    record = operator_record()
    planned = EffectRecord.plan(
        0,
        EffectKind.OPERATOR_COMMENT,
        FIX_OWNER,
        identity=record.identity,
        target=record.target,
        precondition=record.precondition,
        payload=record.payload,
    )
    assert planned == record
    assert (record.stage, record.attempts, record.completion) == (Stage.INTENDED, 0, K10_COMPLETION)
    assert record.operator_performed and record.binding is None
    assert record.describe() == f"operator_comment effect 0 on {ISSUE3}"
    assert not any(build().operator_performed for build, _ in OBSERVED)
    bound = record.binding_to(K10_BINDING["comment_url"], 400, K10_BINDING["body_sha256"])
    stages = [
        record,
        bound,
        record.observing(exact_evidence(record)),
        bound.observing(exact_evidence(bound)),
        bound.observing(K10_BOUND_EVIDENCE),
        record.conflicting("a human edited it"),
        bound.conflicting("a human edited it"),
    ]
    for each in stages:
        raw = json.loads(json.dumps(each.to_dict()))
        assert EffectRecord.from_dict(raw) == each
        assert EffectRecord.from_dict(raw).to_dict() == raw
    assert record.conflicting("x").reopened() == record
    assert bound.conflicting("x").reopened() == bound


def test_the_link_rule_allows_links_into_the_runs_repository_only():
    """The agent's text is published under the operator's identity: a link
    into the run's repository loads, any other link is refused on load."""
    inside = "Details: https://github.com/owner/repo/issues/3#issuecomment-1 and #4."
    assert operator_record(text=inside).body.count(inside) == 1
    with pytest.raises(StateError, match="has an invalid operator text: .*link or URL"):
        operator_record(text="Details: https://github.com/owner/repo-evil/issues/3")


def _k10(**kwargs: Any) -> Callable[[], dict]:
    return lambda: operator_dict(**kwargs)


def as_built(raw: dict) -> None:
    """No mutation: the builder already made the record inconsistent."""


def _strip_header(raw: dict) -> None:
    raw["payload"]["body"] = raw["payload"]["body"].split(APPEND_SEPARATOR, 1)[1]


def _without(data: dict, key: str) -> dict:
    return {k: v for k, v in data.items() if k != key}


ALLOWED_BY_RULE = "must be a non-empty list of \\('issue_body', 'epic'\\)"
OPERATOR_CORRUPTIONS = [
    # Never attempted: the controller never issues K10 (D4.1 has nothing to count).
    _p(
        "k10-attempted",
        operator_dict,
        both(at("stage", "attempted"), at("attempts", 1)),
        "is an operator-performed operator_comment that records an attempt",
    ),
    _p(
        "k10-conflict-charged",
        operator_dict,
        both(at("stage", "conflict"), at("reason", "x"), at("attempts", 1)),
        "records an attempt; the controller never issues it",
    ),
    _p(
        "k10-observed-charged",
        operator_dict,
        both(observed_as(K10_EXACT), at("attempts", 1)),
        "records an attempt; the controller never issues it",
    ),
    _p("k10-intended-charged", operator_dict, at("attempts", 1), "intended but records an issued"),
    # The precondition: why the target is allowed, from which issue body, and the binding.
    _p("k10-allowed-empty", operator_dict, at("precondition.allowed_by", []), ALLOWED_BY_RULE),
    _p(
        "k10-allowed-out-of-order",
        operator_dict,
        at("precondition.allowed_by", ["epic", "issue_body"]),
        ALLOWED_BY_RULE,
    ),
    _p(
        "k10-allowed-repeated",
        operator_dict,
        at("precondition.allowed_by", ["issue_body", "issue_body"]),
        ALLOWED_BY_RULE,
    ),
    _p("k10-allowed-unknown", operator_dict, at("precondition.allowed_by", ["x"]), ALLOWED_BY_RULE),
    _p("k10-allowed-text", operator_dict, at("precondition.allowed_by", "epic"), ALLOWED_BY_RULE),
    _p(
        "k10-issue-body-digest",
        operator_dict,
        at("precondition.issue_body_sha256", "A" * 64),
        "issue_body_sha256 must be a 64-character lowercase SHA-256 hex digest",
    ),
    _p(
        "k10-no-binding-key",
        operator_dict,
        without("precondition.binding"),
        r"is missing key\(s\) \['binding'\]",
    ),
    _p(
        "k10-absent-key",
        operator_dict,
        at("precondition.absent", True),
        r"has unknown key\(s\) \['absent'\]",
    ),
    _p(
        "k10-binding-not-an-object",
        operator_dict,
        at("precondition.binding", "comment 400"),
        "binding must be an object",
    ),
    _p(
        "k10-binding-missing-key",
        operator_dict,
        at("precondition.binding", _without(K10_BINDING, "body_sha256")),
        r"binding is missing key\(s\) \['body_sha256'\]",
    ),
    _p(
        "k10-binding-extra-key",
        operator_dict,
        at("precondition.binding", {**K10_BINDING, "author": "someone"}),
        r"binding has unknown key\(s\) \['author'\]",
    ),
    _p(
        "k10-binding-not-a-comment-url",
        operator_dict,
        at("precondition.binding", {**K10_BINDING, "comment_url": ISSUE3}),
        "binding.comment_url is not a GitHub URL of the expected kind",
    ),
    _p(
        "k10-binding-on-another-issue",
        operator_dict,
        at("precondition.binding", evidence_binding(target=ISSUE4)),
        "binding names a comment on another issue than its target",
    ),
    _p(
        "k10-binding-in-another-repository",
        operator_dict,
        at("precondition.binding", evidence_binding(target=OTHER_REPO_ISSUE3)),
        "binding names a comment on another issue than its target",
    ),
    _p(
        "k10-binding-id-text",
        operator_dict,
        at("precondition.binding", {**K10_BINDING, "comment_id": "400"}),
        "binding.comment_id must be an integer",
    ),
    _p(
        "k10-binding-id-bool",
        operator_dict,
        at("precondition.binding", {**K10_BINDING, "comment_id": True}),
        "binding.comment_id must be an integer",
    ),
    _p(
        "k10-binding-id-zero",
        operator_dict,
        at("precondition.binding", {**K10_BINDING, "comment_id": 0}),
        "binding.comment_id must be >= 1",
    ),
    _p(
        "k10-binding-id-not-the-urls",
        operator_dict,
        at("precondition.binding", {**K10_BINDING, "comment_id": 401}),
        "binding names another comment id than its URL's",
    ),
    _p(
        "k10-binding-digest",
        operator_dict,
        at("precondition.binding", {**K10_BINDING, "body_sha256": "x" * 64}),
        "binding.body_sha256 must be a 64-character lowercase SHA-256 hex digest",
    ),
    # The target: another issue of the owner's repository, never its own issue or PR.
    _p("k10-own-issue", _k10(target=ISSUE), as_built, "targets its owner's own issue"),
    _p(
        "k10-another-repository",
        _k10(target=OTHER_REPO_ISSUE3),
        as_built,
        "targets an issue outside its owner's repository",
    ),
    _p("k10-own-pr-number", _k10(target=ISSUE42), as_built, "targets its owner's PR"),
    _p(
        "k10-target-a-pr-url",
        operator_dict,
        both(at("identity.issue_url", PR43), at("target.issue_url", PR43)),
        "issue_url is not a GitHub URL of the expected kind",
    ),
    _p(
        "k10-identity-not-target",
        operator_dict,
        at("target.issue_url", ISSUE4),
        "has a issue that differs between its identity and its target",
    ),
    _p("k10-owner-without-pr", operator_dict, at("owner.pr_url", ""), "has an owner without a PR"),
    _p(
        "k10-review-owner",
        _k10(by=owner(Phase.REVIEW, pr_url=PR)),
        as_built,
        "is a operator_comment effect, which REVIEW never plans",
    ),
    # The marker: the owner's issue and PR, and this record's target.
    _p(
        "k10-marker-for-another-target",
        _k10(marker=render_operator_comment_marker(ISSUE, PR, 1, "R1-F1", ISSUE4)),
        as_built,
        "carries a marker whose target is not its target",
    ),
    _p(
        "k10-marker-for-another-issue",
        _k10(marker=render_operator_comment_marker(ISSUE4, PR, 1, "R1-F1", ISSUE3)),
        as_built,
        "carries a marker for another issue than its owner's",
    ),
    _p(
        "k10-marker-for-another-pr",
        _k10(marker=render_operator_comment_marker(ISSUE, PR43, 1, "R1-F1", ISSUE3)),
        as_built,
        "carries a marker for another PR than its owner's",
    ),
    _p(
        "k10-marker-of-another-kind",
        operator_dict,
        at("identity.marker", ACCEPTANCE_MARKER),
        "must be exactly one well-formed ai-operator-comment marker",
    ),
    # The payload: exactly the controller's rendering of a text the parser accepts.
    _p(
        "k10-body-without-header",
        operator_dict,
        _strip_header,
        "has a payload body that is not the controller's header, its text and its marker",
    ),
    _p(
        "k10-body-of-another-finding",
        operator_dict,
        at("payload.body", operator_comment_body(OPERATOR_TEXT, ISSUE, PR, 1, "R1-F2", ISSUE3)),
        "has a payload body that is not the controller's header, its text and its marker",
    ),
    _p(
        "k10-text-not-stripped",
        _k10(text=f" {OPERATOR_TEXT}"),
        as_built,
        "has an operator text that is not in the parser's stored form",
    ),
    _p("k10-text-blank", _k10(text=""), as_built, "has an invalid operator text"),
    _p(
        "k10-link-to-another-host",
        _k10(text="The report: https://example.com/report"),
        as_built,
        "has an invalid operator text: .*link or URL",
    ),
    _p(
        "k10-link-to-another-repository",
        _k10(text="See https://github.com/other/repo/issues/1 for the check."),
        as_built,
        "has an invalid operator text: .*link or URL",
    ),
    _p(
        "k10-www-autolink",
        _k10(text="See www.example.com for the check."),
        as_built,
        "has an invalid operator text: .*link or URL",
    ),
    _p(
        "k10-image",
        _k10(text="![chart](https://github.com/owner/repo/chart.png)"),
        as_built,
        "has an invalid operator text: .*Markdown image",
    ),
    _p(
        "k10-raw-html",
        _k10(text="It is <b>applied</b>."),
        as_built,
        "has an invalid operator text: .*raw HTML",
    ),
    _p("k10-credential", _k10(text=CREDENTIAL_TEXT), as_built, "is not redaction-invariant"),
    # The observation: a comment on the target, and evidence it can stand for.
    _p(
        "k10-observed-id-not-the-urls",
        operator_dict,
        observed_as({**K10_EXACT, "comment_id": 301}),
        "observed names another comment id than its URL's",
    ),
    _p(
        "k10-observed-on-another-issue",
        operator_dict,
        observed_as({**K10_EXACT, "url": comment_url(ISSUE4, 300)}),
        "is observed on another issue than its target",
    ),
    _p(
        "k10-observed-on-the-pr",
        operator_dict,
        observed_as({**K10_EXACT, "url": comment_url(PR, 300)}),
        "is observed on another issue than its target",
    ),
    _p(
        "k10-exact-of-other-bytes",
        operator_dict,
        observed_as({**K10_EXACT, "body_sha256": OTHER_DIGEST}),
        "is exact evidence whose digest is not the payload's",
    ),
    _p(
        "k10-bound-without-binding",
        operator_dict,
        observed_as(K10_BOUND_EVIDENCE),
        "is bound evidence that is not the recorded binding",
    ),
    _p(
        "k10-bound-to-another-comment",
        _k10(binding=K10_BINDING),
        observed_as({**K10_BOUND_EVIDENCE, "url": comment_url(ISSUE3, 401), "comment_id": 401}),
        "is bound evidence that is not the recorded binding",
    ),
    _p(
        "k10-bound-with-another-digest",
        _k10(binding=K10_BINDING),
        observed_as({**K10_BOUND_EVIDENCE, "body_sha256": OTHER_DIGEST}),
        "is bound evidence that is not the recorded binding",
    ),
    _p(
        "k10-evidence-unknown",
        operator_dict,
        observed_as({**K10_EXACT, "evidence": "approved"}),
        "observed.evidence must be one of",
    ),
    _p(
        "k10-observed-missing-key",
        operator_dict,
        observed_as(_without(K10_EXACT, "comment_id")),
        r"observed is missing key\(s\) \['comment_id'\]",
    ),
    _p(
        "k10-completion",
        operator_dict,
        at("completion", "acceptance_comment_exactly_one_body_equals_payload"),
        "completion must be 'operator_comment_exact_or_bound_in_complete_listing'",
    ),
]


@pytest.mark.parametrize(("base", "mutate", "match"), OPERATOR_CORRUPTIONS)
def test_a_corrupt_operator_comment_fails_loudly(base, mutate, match):
    """Every K10 the controller could not have planned is a StateError naming the
    rule: an attempt, a target that is the run's own issue or PR or outside its
    repository, a marker or payload that is not the rendering, a binding or an
    observation that is not evidence for this target."""
    raw = base()
    mutate(raw)
    with pytest.raises(StateError, match=match) as info:
        EffectRecord.from_dict(raw)
    assert SECRET not in str(info.value)


# -- K10: the plan, its context and its entry ---------------------------------------------


def test_a_plan_led_by_operator_comments_loads_with_its_context_and_entry():
    """K10 records lead the plan; follow-ups (K5, K6) and the push follow. The
    context names the K10s from the finding they resolve, and the entry
    observation allowed each target for the reasons the record keeps."""
    records = [
        operator_record(),
        operator_record(target=EPIC, position=1, allowed_by=("epic",)),
        follow_up_record(finding_id="R1-F2", position=2),
        replace(append_record(finding_ids=("R1-F3",), target=ISSUE4), position=3),
        push_record(position=4),
    ]
    context = {
        **FIX_CONTEXT,
        "resolutions": [
            k10_resolution(),
            fix_resolution("R1-F2", "follow_up_created", follow_up=2),
            fix_resolution("R1-F3", "follow_up_created", follow_up=3),
        ],
    }
    binding = replace(FIX_BINDING, open_finding_ids=("R1-F1", "R1-F2", "R1-F3"))
    loaded = load_phase_effects(
        [r.to_dict() for r in records], operator_observation(), context, binding
    )
    assert loaded.records == tuple(records)
    assert [r.kind for r in loaded.records] == [
        EffectKind.OPERATOR_COMMENT,
        EffectKind.OPERATOR_COMMENT,
        EffectKind.FOLLOW_UP_ISSUE,
        EffectKind.FOLLOW_UP_APPEND,
        EffectKind.PUSH,
    ]
    assert loaded.observation is not None and loaded.observation.operator_targets is not None
    assert loaded.observation.operator_targets.numbers == (3, 4, 1)
    assert isinstance(loaded.context, FixContext)
    assert loaded.context.resolutions[0]["operator_sources"] == [0, 1]
    assert not loaded.published
    # The default case of the load table below loads as well.
    assert len(load_phase_effects(*_k10_case()).records) == len(K10_PLAN)


@pytest.mark.parametrize(
    ("records", "match"),
    [
        pytest.param(
            lambda: [push_record(), operator_record(position=1)],
            "plans an operator comment after another effect",
            id="after-the-push",
        ),
        pytest.param(
            lambda: [
                operator_record(),
                follow_up_record(finding_id="R1-F2", position=1),
                operator_record(target=ISSUE4, position=2),
            ],
            "plans an operator comment after another effect",
            id="not-a-prefix",
        ),
        pytest.param(
            lambda: [acceptance_record(), operator_record(position=1)],
            "plans an acceptance comment and operator comments together",
            id="with-an-acceptance-comment",
        ),
        pytest.param(
            lambda: [operator_record(), acceptance_record(position=1)],
            "plans an acceptance comment after another effect",
            id="before-an-acceptance-comment",
        ),
        pytest.param(
            lambda: [operator_record(target=t, position=i) for i, t in enumerate(FIVE_TARGETS)],
            f"plans 5 operator comments, over {MAX_OPERATOR_ACTIONS_PER_FIX}",
            id="five",
        ),
        pytest.param(
            lambda: [operator_record(), operator_record(position=1)],
            "plans two operator comments with one identity",
            id="one-identity-twice",
        ),
        pytest.param(
            lambda: [
                operator_record().observing(exact_evidence(operator_record())),
                operator_record(
                    finding_id="R1-F2", position=1, binding=evidence_binding(300)
                ).observing(
                    {**K10_BOUND_EVIDENCE, "comment_id": 300, "url": comment_url(ISSUE3, 300)}
                ),
            ],
            "verifies the operator comments at positions 0 and 1 with one comment",
            id="one-comment-twice",
        ),
    ],
)
def test_a_plan_whose_operator_comments_are_not_its_unique_leading_prefix_fails_loudly(
    records, match
):
    """Nothing the controller sends precedes an action the operator may still
    decline; K9 and K10 never share a plan; at most four K10s, each once."""
    with pytest.raises(StateError, match=match):
        load_records([r.to_dict() for r in records()], FIX_BINDING)


def test_two_operator_comments_for_one_issue_in_another_case_are_one_identity():
    """GitHub names owners and repositories case-insensitively, and the
    marker's key folds case: #3 of Owner/Repo is #3 of owner/repo. A plan
    holding both is one identity twice, and never a verifiable plan."""
    upper = "https://github.com/Owner/Repo/issues/3"
    twin = operator_record(target=upper, position=1)
    records = [operator_record(), twin, push_record(position=2)]
    with pytest.raises(StateError, match="plans two operator comments with one identity"):
        load_phase_effects(*_k10_case(records=records))


OPERATOR_PLAN_MISFITS = [
    pytest.param(
        {"resolutions": [k10_resolution(sources=[0])]},
        r"effect_records\[1\] is an operator comment no resolution of the context names",
        id="unnamed",
    ),
    pytest.param(
        {"resolutions": [k10_resolution("R1-F2")], "open_ids": ("R1-F2",)},
        "resolution for R1-F2 names operator effect 0, which is another finding's or round's",
        id="named-by-another-finding",
    ),
    pytest.param(
        {
            "resolutions": [k10_resolution(), k10_resolution("R1-F2", [1])],
            "open_ids": ("R1-F1", "R1-F2"),
        },
        "resolution for R1-F2 names operator effect 1, which is another finding's or round's",
        id="shared-with-another-finding",
    ),
    pytest.param(
        {
            "records": [operator_record(round_=2, finding_id="R2-F1")],
            "resolutions": [k10_resolution("R2-F1", [0])],
            "open_ids": ("R2-F1",),
        },
        "names operator effect 0, which is another finding's or round's",
        id="another-rounds",
    ),
    pytest.param(
        {"resolutions": [k10_resolution(sources=[0, 1, 2])]},
        "names effect 2, which is not an operator comment",
        id="names-the-push",
    ),
    pytest.param(
        {"resolutions": [k10_resolution(sources=[0, 1, 7])]},
        "names plan position 7, which the plan does not hold",
        id="names-no-record",
    ),
    pytest.param(
        {"observation": {**operator_observation(), "operator_targets": None}},
        "operator_targets must be an object",
        id="null-allowed-set",
    ),
    pytest.param(
        {"observation": _without(operator_observation(), "operator_targets")},
        "is an operator comment, but the FIX entry recorded no allowed targets",
        id="no-allowed-set",
    ),
    pytest.param(
        {"observation": {}},
        "is an operator comment, but the FIX entry recorded no allowed targets",
        id="no-entry-observation",
    ),
    pytest.param(
        {"observation": operator_observation(operator_targets((4, 1)))},
        r"effect_records\[0\] targets issue #3, which the FIX entry did not allow",
        id="target-not-allowed",
    ),
    pytest.param(
        {
            "observation": operator_observation(
                operator_targets(
                    allowed={3: ["issue_body"], 4: ["issue_body"], 1: ["issue_body", "epic"]}
                )
            )
        },
        r"effect_records\[1\] records another allowed set than the FIX entry's",
        id="allowed-for-other-reasons",
    ),
    pytest.param(
        {"observation": operator_observation(operator_targets(digest=OTHER_DIGEST))},
        r"effect_records\[0\] records another allowed set than the FIX entry's",
        id="read-from-another-issue-body",
    ),
    pytest.param(
        {"observation": operator_observation(operator_targets((3, 2, 1)))},
        "operator_targets allows the run's own issue or PR",
        id="entry-allows-the-own-issue",
    ),
    pytest.param(
        {"observation": operator_observation(operator_targets((3, 42, 1)))},
        "operator_targets allows the run's own issue or PR",
        id="entry-allows-the-own-pr",
    ),
    pytest.param(
        {"resolutions": [k10_resolution(kind="no_change_with_rationale")]},
        "operator_sources is set exactly when the finding is resolved by operator actions",
        id="sources-on-another-resolution",
    ),
    pytest.param(
        {"resolutions": [k10_resolution(sources=None)]},
        "operator_sources is set exactly when the finding is resolved by operator actions",
        id="operator-acceptance-without-sources",
    ),
    pytest.param(
        {"resolutions": [k10_resolution(sources=[0, 0])]},
        "operator_sources repeats a plan position",
        id="repeated-source",
    ),
    pytest.param(
        {"resolutions": [k10_resolution(sources=[])]},
        f"must be a list of 1 to {MAX_OPERATOR_ACTIONS_PER_FIX} plan positions",
        id="no-source",
    ),
    pytest.param(
        {"resolutions": [k10_resolution(sources=[0, 1, 2, 3, 4])]},
        f"must be a list of 1 to {MAX_OPERATOR_ACTIONS_PER_FIX} plan positions",
        id="five-sources",
    ),
    pytest.param(
        {"resolutions": [fix_resolution("R1-F1", "operator_acceptance", operator="0")]},
        f"must be a list of 1 to {MAX_OPERATOR_ACTIONS_PER_FIX} plan positions",
        id="sources-not-a-list",
    ),
    pytest.param(
        {"resolutions": [k10_resolution(sources=[0, MAX_EFFECTS_PER_PLAN])]},
        rf"operator_sources\[1\] must be in \[0, {MAX_EFFECTS_PER_PLAN - 1}\]",
        id="source-past-the-plan-bound",
    ),
]


@pytest.mark.parametrize(("change", "match"), OPERATOR_PLAN_MISFITS)
def test_an_operator_plan_its_context_or_entry_does_not_explain_fails_loudly(change, match):
    """Each K10 is named by exactly the resolution of its own finding and round,
    and its target is in the set the FIX entry allowed, for the recorded
    reasons and from the recorded issue body; that set never holds the run's
    own issue or PR."""
    with pytest.raises(StateError, match=match):
        load_phase_effects(*_k10_case(**change))


def test_only_a_fix_entry_records_operator_targets():
    """The allowed set is FIX's alone, stored only when set, and round-trips."""
    observation = EntryObservation.from_dict(operator_observation(), FIX_BINDING)
    assert observation.operator_targets == OperatorTargets.from_dict(operator_targets(), "t")
    assert observation.to_dict() == operator_observation()
    plain = _without(operator_observation(), "operator_targets")
    loaded = EntryObservation.from_dict(plain, FIX_BINDING)
    assert loaded.operator_targets is None and loaded.to_dict() == plain
    review = replace(FIX_BINDING, phase=Phase.REVIEW)
    with pytest.raises(StateError, match="operator_targets is recorded by a FIX entry only"):
        EntryObservation.from_dict(operator_observation(phase="REVIEW"), review)


def test_the_largest_plan_with_operator_comments_fits_both_bounds():
    """D2.4 (#243): four operator comments for one finding (at the largest text
    the parser accepts), one follow-up for each of the other 49 findings and
    the push are the record bound, and the plan loads with its context and
    entry within the total size bound. One record more is refused."""
    assert MAX_EFFECTS_PER_PLAN == 54
    assert MAX_EFFECTS_PER_PLAN == (MAX_FINDINGS_PER_REVIEW - 1) + MAX_OPERATOR_ACTIONS_PER_FIX + 1
    text = "x" * MAX_OPERATOR_COMMENT_CHARS
    targets = (ISSUE3, ISSUE4, EPIC, ISSUE43)
    records = [
        operator_record(
            target=t,
            position=i,
            text=text,
            allowed_by=("epic",) if t == EPIC else ("issue_body",),
        )
        for i, t in enumerate(targets)
    ]
    start = len(records)
    others = [f"R1-F{n}" for n in range(2, MAX_FINDINGS_PER_REVIEW + 1)]
    records += [
        follow_up_record(finding_id=fid, position=start + i) for i, fid in enumerate(others)
    ]
    records.append(push_record(position=len(records)))
    assert len(records) == MAX_EFFECTS_PER_PLAN
    context = {
        **FIX_CONTEXT,
        "resolutions": [
            k10_resolution(sources=range(len(targets))),
            *(
                fix_resolution(fid, "follow_up_created", follow_up=start + i)
                for i, fid in enumerate(others)
            ),
        ],
    }
    raw = [r.to_dict() for r in records]
    loaded = load_phase_effects(
        raw,
        operator_observation(operator_targets((3, 4, 43, 1))),
        context,
        replace(FIX_BINDING, open_finding_ids=("R1-F1", *others)),
    )
    assert loaded.records == tuple(records)
    assert plan_size_problem(loaded.records, loaded.context) == ""
    with pytest.raises(
        StateError, match=f"holds {MAX_EFFECTS_PER_PLAN + 1} records, over {MAX_EFFECTS_PER_PLAN}"
    ):
        load_records([*raw, raw[-1]], FIX_BINDING)
    with pytest.raises(StateError, match=rf"position must be in \[0, {MAX_EFFECTS_PER_PLAN - 1}\]"):
        push_record(position=MAX_EFFECTS_PER_PLAN)


# -- K10: the allowed set read at FIX entry -----------------------------------------------


def test_operator_targets_round_trip_through_json():
    """Numbers in order of first mention, each with why it is allowed, and the
    digest of the issue body they were read from; at most MAX_OPERATOR_TARGETS."""
    loaded = OperatorTargets.from_dict(operator_targets(), "t")
    assert loaded == OperatorTargets(
        (3, 4, 1), {3: ("issue_body",), 4: ("issue_body",), 1: ("epic",)}, OPERATOR_BODY_SHA
    )
    for raw in (
        operator_targets(),
        operator_targets((5,), allowed={5: ["issue_body", "epic"]}),
        operator_targets(()),
        operator_targets(range(3, 3 + MAX_OPERATOR_TARGETS)),
    ):
        again = OperatorTargets.from_dict(json.loads(json.dumps(raw)), "t")
        assert again.to_dict() == raw


OPERATOR_TARGET_CORRUPTIONS = [
    pytest.param("3, 4", "must be an object", id="not-an-object"),
    pytest.param(
        _without(operator_targets(), "issue_body_sha256"),
        r"is missing key\(s\) \['issue_body_sha256'\]",
        id="missing-key",
    ),
    pytest.param(
        {**operator_targets(), "epic": 1}, r"has unknown key\(s\) \['epic'\]", id="unknown-key"
    ),
    pytest.param(operator_targets((3, 3, 1)), "numbers repeats a number", id="repeated-number"),
    pytest.param(
        {**operator_targets(), "numbers": "3"},
        f"numbers must be a list of at most {MAX_OPERATOR_TARGETS} numbers",
        id="numbers-not-a-list",
    ),
    pytest.param(
        operator_targets(range(3, 4 + MAX_OPERATOR_TARGETS)),
        f"numbers must be a list of at most {MAX_OPERATOR_TARGETS} numbers",
        id="too-many-numbers",
    ),
    pytest.param(
        {**operator_targets(), "numbers": [3, 4, 0]},
        r"numbers\[2\] must be >= 1, got 0",
        id="number-zero",
    ),
    pytest.param(
        {**operator_targets(), "numbers": [3, 4, True]},
        r"numbers\[2\] must be an integer",
        id="number-bool",
    ),
    pytest.param(
        {**operator_targets(), "numbers": [3, 4, "1"]},
        r"numbers\[2\] must be an integer",
        id="number-text",
    ),
    pytest.param(
        operator_targets((3, 4, 1), allowed={3: ["issue_body"], 4: ["issue_body"]}),
        "allowed_by must name exactly the numbers, each by its decimal text",
        id="allowed-by-misses-a-number",
    ),
    pytest.param(
        operator_targets(
            (3, 4, 1), allowed={3: ["issue_body"], 4: ["issue_body"], 1: ["epic"], 5: ["epic"]}
        ),
        "allowed_by must name exactly the numbers, each by its decimal text",
        id="allowed-by-names-another-number",
    ),
    pytest.param(
        {
            **operator_targets(),
            "allowed_by": {"3": ["issue_body"], "4": ["issue_body"], "01": ["epic"]},
        },
        "allowed_by must name exactly the numbers, each by its decimal text",
        id="allowed-by-key-not-decimal",
    ),
    pytest.param(
        {**operator_targets(), "allowed_by": [["issue_body"], ["issue_body"], ["epic"]]},
        "allowed_by must name exactly the numbers, each by its decimal text",
        id="allowed-by-not-an-object",
    ),
    pytest.param(
        operator_targets((3, 4, 1), allowed={3: ["issue_body"], 4: [], 1: ["epic"]}),
        r"allowed_by\[4\] must be a non-empty list",
        id="no-reason",
    ),
    pytest.param(
        operator_targets((3,), allowed={3: ["epic", "issue_body"]}),
        r"allowed_by\[3\] must be a non-empty list of \('issue_body', 'epic'\), in that order",
        id="reasons-out-of-order",
    ),
    pytest.param(
        operator_targets((3,), allowed={3: ["issue_body", "issue_body"]}),
        r"allowed_by\[3\] must be a non-empty list",
        id="repeated-reason",
    ),
    pytest.param(
        operator_targets((3,), allowed={3: ["policy"]}),
        r"allowed_by\[3\] must be a non-empty list",
        id="unknown-reason",
    ),
    pytest.param(
        operator_targets((3, 4, 1), allowed={3: ["epic"], 4: ["issue_body"], 1: ["epic"]}),
        "allowed_by names more than one EPIC",
        id="two-epics",
    ),
    pytest.param(
        operator_targets(digest="A" * 64),
        "issue_body_sha256 must be a 64-character lowercase SHA-256 hex digest",
        id="digest",
    ),
]


@pytest.mark.parametrize(("raw", "match"), OPERATOR_TARGET_CORRUPTIONS)
def test_corrupt_operator_targets_fail_loudly(raw, match):
    with pytest.raises(StateError, match=match):
        OperatorTargets.from_dict(raw, "entry_observation.operator_targets")


# -- K10: the action id and the binding -----------------------------------------------------


def test_the_operator_action_id_is_deterministic_and_never_an_approvable_id():
    """``X-`` and 16 hex digits over the owner, identity, target and payload:
    the same record is the same id in any process, other bytes, another
    target, finding, round or run are another id, and the ``A-`` form that
    ``unblock --approve-action`` accepts never matches it."""
    record = operator_record()
    again = EffectRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    parts = (record.identity, record.target, record.payload)
    x_id = record.action_id
    assert x_id == again.action_id == operator_action_id(FIX_OWNER, *parts)
    assert x_id.startswith("X-") and len(x_id) == 18
    assert OPERATOR_ACTION_ID_RE.match(x_id) and int(x_id[2:], 16) >= 0
    a_id = acceptance_action_id(FIX_OWNER, *parts)
    assert a_id != x_id
    assert ACTION_ID_RE.match(a_id) and not ACTION_ID_RE.match(x_id)
    assert not OPERATOR_ACTION_ID_RE.match(a_id)
    others = [
        operator_record(text=f"{OPERATOR_TEXT} Again."),
        operator_record(target=ISSUE4),
        operator_record(finding_id="R1-F2"),
        operator_record(round_=2, finding_id="R2-F1"),
        operator_record(by=owner(Phase.FIX, pr_url=PR, run_id="run-2")),
    ]
    ids = {x_id, *(o.action_id for o in others)}
    assert len(ids) == 1 + len(others)
    assert all(OPERATOR_ACTION_ID_RE.match(i) for i in ids)
    # The binding is a precondition: binding evidence never changes the id.
    bound = record.binding_to(K10_BINDING["comment_url"], 400, K10_BINDING["body_sha256"])
    assert bound.action_id == x_id


def test_a_binding_is_recorded_on_an_intended_operator_comment_only():
    """``binding_to`` pins one comment of the target by URL, id and digest,
    validated as a load; a settled K10 or another kind is never bound."""
    record = operator_record()
    url, digest = K10_BINDING["comment_url"], K10_BINDING["body_sha256"]
    bound = record.binding_to(url, 400, digest)
    assert bound.binding == {"comment_url": url, "comment_id": 400, "body_sha256": digest}
    assert bound.precondition == {**record.precondition, "binding": bound.binding}
    assert (bound.stage, bound.attempts, bound.payload) == (Stage.INTENDED, 0, record.payload)
    assert record.binding is None
    copy = bound.binding
    assert copy is not None
    copy["comment_id"] = 1
    assert bound.binding["comment_id"] == 400  # the property hands out a copy
    rebound = bound.binding_to(comment_url(ISSUE3, 401), 401, OTHER_DIGEST)
    assert rebound.binding == {
        "comment_url": comment_url(ISSUE3, 401),
        "comment_id": 401,
        "body_sha256": OTHER_DIGEST,
    }
    for settled in (
        record.observing(exact_evidence(record)),
        record.conflicting("a human edited it"),
        acceptance_record(),
        push_record(),
    ):
        with pytest.raises(StateError, match="cannot be bound to evidence"):
            settled.binding_to(url, 400, digest)
    with pytest.raises(StateError, match="names a comment on another issue than its target"):
        record.binding_to(comment_url(ISSUE4, 400), 400, digest)
    with pytest.raises(StateError, match="names another comment id than its URL's"):
        record.binding_to(url, 401, digest)
    with pytest.raises(StateError, match="must be a 64-character lowercase SHA-256 hex digest"):
        record.binding_to(url, 400, "not a digest")


# -- K10: never attempted, driven or issued; verified -------------------------------------


@dataclass
class Decides:
    """An operation whose reconciliation returns ``decision``; it never writes."""

    decision: Decision
    reconciled: int = 0

    def reconcile(self, record: EffectRecord) -> Decision:
        self.reconciled += 1
        return self.decision

    def issue(self, record: EffectRecord) -> None:
        raise AssertionError("an operation's write was reached")


def test_an_operator_comment_is_never_attempted_driven_or_issued():
    """The controller never issues K10, under any path: ``attempting`` refuses
    it, ``drive`` refuses it before any read, save or write, and the
    operation's ``issue`` raises. Nor is it approved or rebased."""
    fake = FakeGitHub()
    fake.add_issue(ISSUE3, "A related issue")
    record = operator_record()
    for each in (record, record.binding_to(K10_BINDING["comment_url"], 400, OTHER_DIGEST)):
        with pytest.raises(StateError, match="is operator-performed; the controller never issues"):
            each.attempting()
    assert issubclass(OperatorPerformedError, StateError)
    saves = saves_for(fake)
    stub = Decides(Decision(Action.ISSUE))
    with pytest.raises(OperatorPerformedError, match="verified, never driven") as info:
        drive(record, stub, saves)
    assert record.action_id in str(info.value)
    assert stub.reconciled == 0 and saves.log == []
    op = operation_for(record, fake)
    assert isinstance(op, OperatorCommentOp)
    with pytest.raises(OperatorPerformedError, match="verified, never driven"):
        drive(record, op, saves)
    with pytest.raises(OperatorPerformedError, match=f"{record.action_id}.*never posts it"):
        op.issue(record)
    assert saves.log == [] and fake.effect_writes == [] and fake.comments.get(ISSUE3) is None
    assert ("get_issue", ISSUE3) not in fake.calls
    with pytest.raises(StateError, match="cannot be approved"):
        record.approving()
    with pytest.raises(StateError, match="cannot be rebased"):
        record.rebasing("A human's body.")


def test_verify_saves_an_observation_once_and_never_a_hold():
    """A hold changes nothing and saves nothing; the observation is saved as
    made, once; an observed record is never read or saved again."""
    fake = FakeGitHub()
    fake.add_issue(ISSUE3, "A related issue")
    record = operator_record()
    op = OperatorCommentOp(fake, plan=[record])
    saves = saves_for(fake)
    held = verify(record, op, saves)
    assert held.record == record and not held.issued and saves.log == []
    assert f"({record.action_id}): awaiting the operator" in held.held
    url = fake.add_comment(ISSUE3, 300, record.body).url
    observed = verify(record, op, saves)
    assert observed.record.stage is Stage.OBSERVED and not observed.issued and not observed.held
    assert observed.record.observed == {
        "url": url,
        "comment_id": 300,
        "evidence": "exact",
        "body_sha256": sha256_text(record.body),
    }
    assert saves.stages == [(Stage.OBSERVED, 0, 0)]
    calls = len(fake.calls)
    again = verify(observed.record, op, saves)
    assert again.record == observed.record and len(saves.log) == 1
    assert fake.calls[calls:] == [] and fake.effect_writes == []


def test_verify_saves_a_conflict_once():
    """A conflict is saved as made, once; verifying the conflict again saves
    nothing. ``unblock`` reopens it as intended, never attempted."""
    fake = FakeGitHub()
    fake.add_issue(ISSUE3, "A related issue")
    record = operator_record()
    edited = fake.add_comment(ISSUE3, 301, record.body.replace(OPERATOR_TEXT, "Edited.")).url
    op = OperatorCommentOp(fake, plan=[record])
    saves = saves_for(fake)
    first = verify(record, op, saves)
    assert first.record.stage is Stage.CONFLICT and not first.issued
    assert edited in first.record.reason and record.action_id in first.record.reason
    assert saves.stages == [(Stage.CONFLICT, 0, 0)]
    second = verify(first.record, op, saves)
    assert second.record == first.record and len(saves.log) == 1
    reopened = first.record.reopened()
    assert (reopened.stage, reopened.attempts, reopened.reason) == (Stage.INTENDED, 0, "")
    assert fake.effect_writes == []


@pytest.mark.parametrize("action", [Action.ISSUE, Action.REBASE])
def test_verify_refuses_a_decision_to_write(action):
    """No K10 decision ever issues; one that did would be a controller bug,
    refused before anything is saved or sent."""
    stub = Decides(Decision(action, rebase_body="x"))
    log: list[EffectRecord] = []
    with pytest.raises(OperatorPerformedError, match=f"a verification decided to {action.value}"):
        verify(operator_record(), stub, log.append)
    assert log == [] and stub.reconciled == 1


def test_verify_is_for_an_operator_performed_record_only():
    stub = Decides(Decision(Action.OBSERVE, observed={"url": comment_url(ISSUE, 300)}))
    log: list[EffectRecord] = []
    for record in (acceptance_record(), push_record()):
        with pytest.raises(OperatorPerformedError, match="is not operator-performed"):
            verify(record, stub, log.append)
    assert log == [] and stub.reconciled == 0


# -- K10: the verification table (spec section 7.4) ---------------------------------------


def k10_listed(cid: int, body: str, target: str = ISSUE3) -> Listed:
    return Listed(url=comment_url(target, cid), comment_id=cid, body=body)


def decide_k10(
    record: EffectRecord,
    *,
    own: Any = (),
    misplaced: Any = (),
    listing: Any = None,
    defects: Any = (),
    target_problem: str = "",
    taken: Any = None,
) -> Decision:
    """``decide_operator_comment``; the listing defaults to every comment named."""
    return decide_operator_comment(
        record,
        own=list(own),
        misplaced=list(misplaced),
        listing=[*own, *misplaced] if listing is None else list(listing),
        defects=list(defects),
        target_problem=target_problem,
        taken=taken,
    )


CHATTER = k10_listed(299, "A comment carrying no marker.")


def test_exactly_one_comment_with_the_exact_payload_is_exact_evidence():
    record = operator_record()
    exact = k10_listed(300, record.body)
    decision = decide_k10(record, own=[exact], listing=[CHATTER, exact])
    assert decision == Decision(Action.OBSERVE, observed=exact_evidence(record))
    assert record.observing(decision.observed or {}).observed == exact_evidence(record)


@pytest.mark.parametrize(
    ("edit", "only_whitespace"),
    [
        pytest.param(lambda b: b.replace(OPERATOR_TEXT, "Edited by hand."), False, id="text"),
        pytest.param(lambda b: b.replace("is applied", "is applied!"), False, id="punctuation"),
        pytest.param(lambda b: b.replace("\n", "\r\n"), True, id="crlf"),
        pytest.param(lambda b: b.replace("\n", "\r"), True, id="cr"),
        pytest.param(lambda b: b.replace("\n", "  \n"), True, id="trailing-spaces"),
        pytest.param(lambda b: f"{b}\n", True, id="trailing-newline"),
    ],
)
def test_one_comment_with_the_key_and_other_bytes_is_a_conflict(edit, only_whitespace):
    """Byte equality is the comparison of record; the reason says when the
    bytes differ only in line endings or trailing whitespace."""
    record = operator_record()
    body = edit(record.body)
    assert body != record.body
    assert (normalized_comment_text(body) == normalized_comment_text(record.body)) is (
        only_whitespace
    )
    edited = k10_listed(301, body)
    decision = decide_k10(record, own=[edited], listing=[CHATTER, edited])
    assert decision.action is Action.CONFLICT and decision.observed is None
    assert f"({record.action_id}): {edited.url} carries its marker but" in decision.reason
    whitespace = "only in line endings or trailing whitespace"
    assert (whitespace in decision.reason) is only_whitespace
    assert ("its body is not the prepared payload" in decision.reason) is not only_whitespace


def test_two_comments_with_the_key_are_a_conflict_naming_both():
    record = operator_record()
    first, second = k10_listed(300, record.body), k10_listed(301, record.body)
    decision = decide_k10(record, own=[first, second])
    assert decision.action is Action.CONFLICT
    assert f"2 comments carry its marker ({first.url}, {second.url})" in decision.reason


def test_a_misplaced_payload_of_another_action_of_the_plan_is_a_conflict_naming_each():
    record = operator_record()
    sibling = operator_record(target=ISSUE4, position=1)
    misplaced = [k10_listed(302, sibling.body), k10_listed(303, sibling.body)]
    decision = decide_k10(record, misplaced=misplaced)
    assert decision.action is Action.CONFLICT
    assert (
        f"{misplaced[0].url}, {misplaced[1].url} on this issue carries the marker of another "
        "operator action of this plan"
    ) in decision.reason
    assert "a misplaced payload" in decision.reason


def test_defective_operator_markers_in_the_listing_are_a_conflict_naming_them():
    record = operator_record()
    defects = [
        f"comment {comment_url(ISSUE3, 303)}: ai-operator-comment marker payload is not valid JSON",
        f"comment {comment_url(ISSUE3, 304)}: carries 2 ai-operator-comment markers",
    ]
    decision = decide_k10(record, defects=defects)
    assert decision.action is Action.CONFLICT
    assert "the target's comments cannot be read unambiguously" in decision.reason
    assert all(d in decision.reason for d in defects)


def test_the_bound_comment_with_its_digest_unchanged_is_bound_evidence():
    record = operator_record()
    evidence = k10_listed(400, EVIDENCE_TEXT)
    bound = record.binding_to(evidence.url, 400, sha256_text(EVIDENCE_TEXT))
    decision = decide_k10(bound, listing=[CHATTER, evidence])
    assert decision == Decision(Action.OBSERVE, observed=K10_BOUND_EVIDENCE)
    assert bound.observing(decision.observed or {}).observed == K10_BOUND_EVIDENCE
    # The exact payload is exact evidence, whether or not a binding is recorded.
    exact = k10_listed(300, record.body)
    decision = decide_k10(bound, own=[exact], listing=[evidence, exact])
    assert decision == Decision(Action.OBSERVE, observed=exact_evidence(record))


@pytest.mark.parametrize("now", ["edited", "deleted"])
def test_a_bound_comment_that_changed_or_is_gone_is_held_naming_both_digests(now):
    record = operator_record()
    url = comment_url(ISSUE3, 400)
    bound = record.binding_to(url, 400, sha256_text(EVIDENCE_TEXT))
    listing = [CHATTER]
    if now == "edited":
        listing.append(k10_listed(400, f"{EVIDENCE_TEXT} Edited."))
    decision = decide_k10(bound, listing=listing)
    assert decision.action is Action.HOLD and decision.observed is None
    seen = (
        sha256_text(f"{EVIDENCE_TEXT} Edited.") if now == "edited" else "none: the comment is gone"
    )
    assert (
        f"the bound comment {url} changed since it was bound "
        f"(bound sha256 {sha256_text(EVIDENCE_TEXT)}, now {seen})"
    ) in decision.reason


def test_a_comment_another_action_consumes_is_never_this_ones_evidence():
    """PR #254 R2-F1: one comment verifies one action. The exact payload on a
    comment another action consumes is a conflict naming it; a bound comment
    another action consumes is a hold naming it, even with its digest
    unchanged."""
    record = operator_record()
    exact = k10_listed(300, record.body)
    decision = decide_k10(record, own=[exact], taken={300: "X-0123456789abcdef"})
    assert decision.action is Action.CONFLICT and decision.observed is None
    assert (
        f"{exact.url} carries its exact payload but is already the evidence of X-0123456789abcdef"
    ) in decision.reason
    evidence = k10_listed(400, EVIDENCE_TEXT)
    bound = record.binding_to(evidence.url, 400, sha256_text(EVIDENCE_TEXT))
    decision = decide_k10(bound, listing=[evidence], taken={400: "X-0123456789abcdef"})
    assert decision.action is Action.HOLD and decision.observed is None
    assert (
        f"the bound comment {evidence.url} is the evidence of X-0123456789abcdef"
    ) in decision.reason
    # Another comment taken changes nothing.
    assert decide_k10(bound, listing=[evidence], taken={401: "X-0123456789abcdef"}) == Decision(
        Action.OBSERVE, observed=K10_BOUND_EVIDENCE
    )


def test_the_operation_never_verifies_one_comment_for_two_actions():
    """PR #254 R2-F1, over FakeGitHub: a sibling's exact payload on this
    target, a comment a sibling is observed on, and a comment ``reserved``
    for an action verified earlier in the round are each another action's
    evidence, never this record's binding or exact payload."""
    fake, _ = operator_world()
    record = operator_record()
    sibling = operator_record(finding_id="R1-F2", position=1)
    fake.add_comment(ISSUE3, 300, sibling.body)
    fake.add_comment(ISSUE3, 400, EVIDENCE_TEXT)
    claimed = record.binding_to(comment_url(ISSUE3, 300), 300, sha256_text(sibling.body))
    held = OperatorCommentOp(fake, plan=(claimed, sibling)).reconcile(claimed)
    assert held.action is Action.HOLD and f"is the evidence of {sibling.action_id}" in held.reason
    bound = record.binding_to(comment_url(ISSUE3, 400), 400, sha256_text(EVIDENCE_TEXT))
    observed = sibling.binding_to(
        comment_url(ISSUE3, 400), 400, sha256_text(EVIDENCE_TEXT)
    ).observing(K10_BOUND_EVIDENCE)
    plan = (bound, observed)
    held = OperatorCommentOp(fake, plan=plan).reconcile(bound)
    assert held.action is Action.HOLD and f"is the evidence of {sibling.action_id}" in held.reason
    held = OperatorCommentOp(fake, plan=(bound,), reserved={400: "X-0123456789abcdef"}).reconcile(
        bound
    )
    assert held.action is Action.HOLD and "is the evidence of X-0123456789abcdef" in held.reason
    assert OperatorCommentOp(fake, plan=(bound,)).reconcile(bound).action is Action.OBSERVE
    # The exact payload on a comment a sibling is observed on is a conflict.
    fake.add_comment(ISSUE3, 500, record.body)
    url = comment_url(ISSUE3, 500)
    elsewhere = sibling.binding_to(url, 500, sha256_text(record.body))
    plan = (record, elsewhere.observing(exact_evidence(record, 500) | {"evidence": "bound"}))
    conflict = OperatorCommentOp(fake, plan=plan).reconcile(record)
    assert conflict.action is Action.CONFLICT
    assert f"is already the evidence of {sibling.action_id}" in conflict.reason
    assert fake.effect_writes == []


def test_nothing_on_the_target_is_a_hold_awaiting_the_operator():
    record = operator_record()
    decision = decide_k10(record, listing=[CHATTER])
    assert decision == Decision(
        Action.HOLD,
        reason=(
            f"operator_comment effect 0 on {ISSUE3} ({record.action_id}): awaiting the "
            "operator; no comment on the target carries it yet"
        ),
    )


def test_a_target_that_no_longer_qualifies_is_a_hold_naming_it():
    """A transferred, deleted or unreadable target holds: not even the exact
    payload on it is adopted, and nothing was verified."""
    record = operator_record()
    problem = f"issue {ISSUE3} is not found (deleted, or not readable with this token)"
    for own in ([], [k10_listed(300, record.body)]):
        decision = decide_k10(record, own=own, target_problem=problem)
        assert decision.action is Action.HOLD and decision.observed is None
        assert f"({record.action_id}): {problem}; nothing was verified" in decision.reason


def test_a_settled_operator_comment_is_never_decided_again():
    record = operator_record()
    observed = record.observing(exact_evidence(record))
    assert decide_k10(observed) == Decision(Action.DONE, observed=exact_evidence(record))
    conflict = record.conflicting("a human edited it")
    exact = k10_listed(300, record.body)
    settled = Decision(Action.CONFLICT, reason="a human edited it")
    assert decide_k10(conflict, own=[exact]) == settled


def _conflict_over_a_match(match: str, conflict: str) -> tuple[EffectRecord, list[Listed], dict]:
    """A listing holding an exact or bound match (``own`` of the match alone),
    and the decision's arguments once one conflict row is added to it."""
    record = operator_record()
    evidence = k10_listed(400, EVIDENCE_TEXT)
    if match == "bound":
        record = record.binding_to(evidence.url, 400, sha256_text(EVIDENCE_TEXT))
        own: list[Listed] = []
        listing = [evidence]
    else:
        own = [k10_listed(300, record.body)]
        listing = list(own)
    matched = list(own)
    misplaced: list[Listed] = []
    defects: list[str] = []
    if conflict == "misplaced":
        misplaced = [k10_listed(302, operator_record(target=ISSUE4, position=1).body)]
    elif conflict == "two-with-the-key":
        own += [k10_listed(301, record.body), k10_listed(305, record.body)][: 2 - len(own)]
    elif conflict == "other-bytes":
        own = [k10_listed(301, record.body.replace(OPERATOR_TEXT, "Edited."))]
    else:
        defects = [f"comment {comment_url(ISSUE3, 303)}: ai-operator-comment marker is broken"]
    kwargs = {
        "own": own,
        "misplaced": misplaced,
        "listing": [*listing, *(c for c in own if c not in listing), *misplaced],
        "defects": defects,
    }
    return record, matched, kwargs


@pytest.mark.parametrize(
    ("match", "conflict"),
    [
        ("exact", "misplaced"),
        ("exact", "two-with-the-key"),
        ("exact", "defective-marker"),
        ("bound", "misplaced"),
        ("bound", "two-with-the-key"),
        ("bound", "other-bytes"),
        ("bound", "defective-marker"),
    ],
)
def test_a_conflict_row_wins_over_an_exact_or_bound_match(match, conflict):
    """Conflict rows are evaluated first: an exact or bound match never hides
    a misplaced payload, a second comment with the key, other bytes under the
    key or a defective marker."""
    record, matched, kwargs = _conflict_over_a_match(match, conflict)
    without_conflict = decide_k10(record, own=matched, listing=kwargs["listing"])
    assert without_conflict.action is Action.OBSERVE
    assert without_conflict.observed is not None
    assert without_conflict.observed["evidence"] == match
    decision = decide_k10(record, **kwargs)
    assert decision.action is Action.CONFLICT and decision.observed is None


# -- K10: the operation, end to end against FakeGitHub ------------------------------------


def operator_world(*targets: str) -> tuple[FakeGitHub, tuple[EffectRecord, ...]]:
    """A FakeGitHub holding each target issue, and a plan of one K10 per target."""
    targets = targets or (ISSUE3, ISSUE4)
    fake = FakeGitHub()
    for target in targets:
        if target not in fake.issues:
            fake.add_issue(target, "A related issue")
    plan = tuple(operator_record(target=t, position=i) for i, t in enumerate(targets))
    return fake, plan


@pytest.mark.parametrize(
    ("post", "action", "says"),
    [
        pytest.param(lambda r, s: [], Action.HOLD, "awaiting the operator", id="nothing"),
        pytest.param(
            lambda r, s: [(301, r.body.replace(OPERATOR_TEXT, "Edited by hand."))],
            Action.CONFLICT,
            f"{comment_url(ISSUE3, 301)} carries its marker but its body is not the prepared",
            id="other-bytes",
        ),
        pytest.param(
            lambda r, s: [(301, r.body.replace("\n", "\r\n"))],
            Action.CONFLICT,
            f"{comment_url(ISSUE3, 301)} carries its marker but its bytes differ from the "
            "prepared payload only in line endings or trailing whitespace",
            id="line-endings",
        ),
        pytest.param(
            lambda r, s: [(300, r.body), (301, r.body)],
            Action.CONFLICT,
            f"2 comments carry its marker ({comment_url(ISSUE3, 300)}, {comment_url(ISSUE3, 301)})",
            id="two-copies",
        ),
        pytest.param(
            lambda r, s: [(300, r.body), (302, s.body)],
            Action.CONFLICT,
            f"{comment_url(ISSUE3, 302)} on this issue carries the marker of another operator "
            "action of this plan",
            id="misplaced-sibling",
        ),
        pytest.param(
            lambda r, s: [(300, r.body), (303, "Mine.\n\n<!-- ai-operator-comment: {oops} -->")],
            Action.CONFLICT,
            f"comment {comment_url(ISSUE3, 303)}: ai-operator-comment marker payload is not "
            "valid JSON",
            id="defective-marker",
        ),
        pytest.param(
            lambda r, s: [(300, f"{r.body}{APPEND_SEPARATOR}{s.marker}")],
            Action.CONFLICT,
            f"comment {comment_url(ISSUE3, 300)}: carries 2 ai-operator-comment markers",
            id="two-markers-in-one-comment",
        ),
    ],
)
def test_the_operation_decides_on_the_targets_complete_listing(post, action, says):
    """End to end over FakeGitHub: the operation reads the target and its
    complete comment listing, and decides by the table; it never writes."""
    fake, plan = operator_world()
    record, sibling = plan
    fake.add_comment(ISSUE3, 299, CHATTER.body)
    for cid, body in post(record, sibling):
        fake.add_comment(ISSUE3, cid, body)
    decision = OperatorCommentOp(fake, plan=plan).reconcile(record)
    assert decision.action is action and says in decision.reason
    assert record.action_id in decision.reason
    assert ("get_issue", ISSUE3) in fake.calls and ("get_issue_comments", ISSUE3) in fake.calls
    assert fake.effect_writes == []


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_the_exact_payload_on_the_target_is_verified_whoever_posted_it(state):
    """The target may be open or closed; the sibling's payload on its own
    target is no concern of this record."""
    fake, plan = operator_world()
    record, sibling = plan
    fake.issues[ISSUE3] = replace(fake.issues[ISSUE3], state=state)
    fake.add_comment(ISSUE3, 299, CHATTER.body)
    url = fake.add_comment(ISSUE3, 300, record.body).url
    fake.add_comment(ISSUE4, 310, sibling.body)
    saves = saves_for(fake)
    driven = verify(record, OperatorCommentOp(fake, plan=plan), saves)
    assert driven.record.observed == {**exact_evidence(record), "url": url}
    assert saves.stages == [(Stage.OBSERVED, 0, 0)]
    assert not driven.issued and not driven.held and fake.effect_writes == []


@pytest.mark.parametrize("now", ["unchanged", "edited", "deleted"])
def test_a_bound_comment_is_verified_by_its_digest_end_to_end(now):
    fake, plan = operator_world()
    url = comment_url(ISSUE3, 400)
    record = plan[0].binding_to(url, 400, sha256_text(EVIDENCE_TEXT))
    if now != "deleted":
        text = EVIDENCE_TEXT if now == "unchanged" else f"{EVIDENCE_TEXT} Edited."
        fake.add_comment(ISSUE3, 400, text)
    saves = saves_for(fake)
    driven = verify(record, OperatorCommentOp(fake, plan=plan), saves)
    if now == "unchanged":
        assert driven.record.observed == K10_BOUND_EVIDENCE
        assert saves.stages == [(Stage.OBSERVED, 0, 0)]
        return
    assert driven.record == record and saves.log == []
    assert f"the bound comment {url} changed since it was bound" in driven.held
    assert f"bound sha256 {sha256_text(EVIDENCE_TEXT)}" in driven.held


def test_a_misplaced_payload_blocks_the_issue_it_landed_on_not_its_own_action():
    """The sibling's payload posted on #3 is a conflict for #3's action; the
    sibling, whose own target #4 holds nothing, still awaits the operator."""
    fake, plan = operator_world()
    record, sibling = plan
    fake.add_comment(ISSUE3, 300, record.body)
    misplaced = fake.add_comment(ISSUE3, 302, sibling.body).url
    op = OperatorCommentOp(fake, plan=plan)
    saves = saves_for(fake)
    first = verify(record, op, saves)
    assert first.record.stage is Stage.CONFLICT and misplaced in first.record.reason
    second = verify(sibling, op, saves)
    assert second.record == sibling and "awaiting the operator" in second.held
    assert saves.stages == [(Stage.CONFLICT, 0, 0)]


def test_markers_of_earlier_rounds_other_plans_and_siblings_never_block_an_action():
    """Only this record's key and the keys of this plan's actions meant for
    other issues count on a target: an earlier round's action, another PR's
    or run issue's, one for an issue outside the plan, and a sibling meant for
    this same target are other keys, and never conflict."""
    fake, _ = operator_world()
    record = operator_record(round_=2, finding_id="R2-F1")
    sibling_here = operator_record(round_=2, finding_id="R2-F2", position=1)
    plan = (record, sibling_here)
    unrelated = [
        operator_record(round_=1, finding_id="R1-F1").body,
        operator_comment_body(OPERATOR_TEXT, ISSUE, PR43, 2, "R2-F1", ISSUE3),
        operator_comment_body(OPERATOR_TEXT, ISSUE4, PR, 2, "R2-F1", ISSUE3),
        operator_comment_body(OPERATOR_TEXT, ISSUE, PR, 2, "R2-F1", ISSUE44),
        sibling_here.body,
    ]
    for i, body in enumerate(unrelated):
        fake.add_comment(ISSUE3, 290 + i, body)
    op = OperatorCommentOp(fake, plan=plan)
    held = op.reconcile(record)
    assert held.action is Action.HOLD and "awaiting the operator" in held.reason
    url = fake.add_comment(ISSUE3, 300, record.body).url
    observed = op.reconcile(record)
    assert observed == Decision(Action.OBSERVE, observed={**exact_evidence(record), "url": url})
    sibling_seen = op.reconcile(sibling_here)
    assert sibling_seen.action is Action.OBSERVE and sibling_seen.observed is not None
    assert sibling_seen.observed["url"] == comment_url(ISSUE3, 294)


def _deleted(fake: FakeGitHub) -> None:
    del fake.issues[ISSUE3]


def _answers_as_a_pr(fake: FakeGitHub) -> None:
    fake.get_issue_errors[ISSUE3] = GitHubWrongTargetError(
        f"issue {ISSUE3}: `gh` answered with pull request https://github.com/owner/repo/pull/3",
        "https://github.com/owner/repo/pull/3",
    )


def _answers_as_another_issue(fake: FakeGitHub) -> None:
    fake.get_issue_errors[ISSUE3] = GitHubWrongTargetError(
        f"issue {ISSUE3}: `gh` answered with https://github.com/other/repo/issues/9",
        "https://github.com/other/repo/issues/9",
    )


def _moved(fake: FakeGitHub) -> None:
    fake.issues[ISSUE3] = replace(fake.issues[ISSUE3], repository="other/repo")


def _forbidden(fake: FakeGitHub) -> None:
    fake.get_issue_errors[ISSUE3] = GitHubError(
        f"HTTP 403: Resource not accessible ({CREDENTIAL_TEXT})"
    )


@pytest.mark.parametrize(
    ("break_target", "says"),
    [
        pytest.param(
            _deleted,
            f"issue {ISSUE3} is not found (deleted, or not readable with this token)",
            id="deleted",
        ),
        pytest.param(
            _answers_as_a_pr,
            f"issue {ISSUE3} answers as a pull request (https://github.com/owner/repo/pull/3)",
            id="a-pull-request",
        ),
        pytest.param(
            _answers_as_another_issue,
            f"issue {ISSUE3} answers as another object (https://github.com/other/repo/issues/9)",
            id="transferred",
        ),
        pytest.param(
            _moved,
            f"issue {ISSUE3} is no longer in the run's repository owner/repo",
            id="another-repository",
        ),
        pytest.param(
            _forbidden, f"issue {ISSUE3} cannot be read (HTTP 403", id="conclusively-unreadable"
        ),
    ],
)
def test_a_target_that_no_longer_qualifies_is_held_without_reading_its_listing(break_target, says):
    """Every conclusive target problem is a hold naming it: nothing is saved,
    the listing is not read, and even the exact payload is not adopted."""
    fake, plan = operator_world()
    record = plan[0]
    fake.add_comment(ISSUE3, 300, record.body)
    break_target(fake)
    saves = saves_for(fake)
    driven = verify(record, OperatorCommentOp(fake, plan=plan), saves)
    assert driven.record == record and saves.log == [] and not driven.issued
    assert says in driven.held and "nothing was verified" in driven.held
    assert SECRET not in driven.held
    assert ("get_issue_comments", ISSUE3) not in fake.calls


def _issue_unavailable(fake: FakeGitHub) -> None:
    fake.get_issue_errors[ISSUE3] = GitHubUnavailableError("timed out")


def _listing_unavailable(fake: FakeGitHub) -> None:
    fake.comments_error = GitHubUnavailableError("connection reset")


def _listing_refused(fake: FakeGitHub) -> None:
    fake.comments_error = GitHubError("the listing cannot be completed")


@pytest.mark.parametrize(
    ("fail", "error"),
    [
        pytest.param(_issue_unavailable, GitHubUnavailableError, id="target-unavailable"),
        pytest.param(_listing_unavailable, GitHubUnavailableError, id="listing-unavailable"),
        pytest.param(_listing_refused, GitHubError, id="listing-conclusively-unreadable"),
    ],
)
def test_a_target_or_listing_that_cannot_be_read_now_raises_with_nothing_saved(fail, error):
    """A transient failure propagates for the caller to retry; a listing that
    cannot be read to its end is never read as "nothing posted yet"."""
    fake, plan = operator_world()
    record = plan[0]
    fake.add_comment(ISSUE3, 300, record.body)
    fail(fake)
    saves = saves_for(fake)
    with pytest.raises(error):
        verify(record, OperatorCommentOp(fake, plan=plan), saves)
    assert saves.log == [] and fake.effect_writes == []


# -- K10: the operation behind the real client ---------------------------------------------


class IssueConversation(CommentConversation):
    """An issue's conversation that also answers ``gh issue view`` for it.

    ``answers`` is the URL GitHub answers the view with: the issue itself,
    or a pull request or transferred issue in its place.
    """

    def __init__(self, parent_url: str, bodies: list[str], *, answers: str = "") -> None:
        super().__init__(parent_url, bodies)
        self.answers = answers or parent_url
        self.views = 0

    def __call__(self, req: ExecutionRequest) -> ExecutionResult:
        if req.command[1:3] == ["issue", "view"]:
            self.views += 1
            number = int(self.answers.rsplit("/", 1)[1])
            row = {"url": self.answers, "number": number, "title": "t", "state": "OPEN", "body": ""}
            return self._reply(json.dumps(row))
        return super().__call__(req)


def test_an_operator_comment_past_the_first_hundred_is_verified_through_the_real_client():
    """The verification walks every page of the target's conversation: the
    exact payload as the 151st comment is observed, and nothing is posted."""
    record = operator_record()
    conversation = IssueConversation(ISSUE3, FILLER)
    landed = conversation.add(record.body)
    conversation.add("A later comment.")
    github = GitHubClient(runner=conversation, retry_delay_seconds=0)
    saves: list[EffectRecord] = []
    driven = verify(record, OperatorCommentOp(github, plan=[record]), count_saves(saves))
    assert driven.record.observed == {**exact_evidence(record, 5150), "url": landed}
    assert [r.stage for r in saves] == [Stage.OBSERVED]
    assert (conversation.views, conversation.pages_read, conversation.creates) == (1, 2, 0)


@pytest.mark.parametrize(
    ("answered", "says"),
    [
        pytest.param(
            "https://github.com/owner/repo/pull/3",
            "answers as a pull request (https://github.com/owner/repo/pull/3)",
            id="a-pull-request",
        ),
        pytest.param(
            "https://github.com/other/repo/issues/9",
            "answers as another object (https://github.com/other/repo/issues/9)",
            id="transferred",
        ),
    ],
)
def test_a_target_github_answers_with_another_object_is_held_through_the_real_client(
    answered, says
):
    """``gh issue view`` of the target answering with a pull request or a
    transferred issue is GitHubWrongTargetError, a hold naming the answer."""
    record = operator_record()
    conversation = IssueConversation(ISSUE3, [record.body], answers=answered)
    github = GitHubClient(runner=conversation, retry_delay_seconds=0)
    saves: list[EffectRecord] = []
    driven = verify(record, OperatorCommentOp(github, plan=[record]), count_saves(saves))
    assert driven.record == record and saves == []
    assert f"issue {ISSUE3} {says}" in driven.held
    assert (conversation.views, conversation.pages_read) == (1, 0)
