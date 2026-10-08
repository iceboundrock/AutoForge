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
    render_follow_up_marker,
    render_implementation_marker,
    render_progress_marker,
)
from autoforge.effect_ops import (
    AdoptPrOp,
    EffectPending,
    FollowUpAppendOp,
    FollowUpIssueOp,
    ImplementationPrOp,
    ProgressCommentOp,
    PushOp,
    ReplacementPrOp,
    ReviewCommentOp,
    drive,
    operation_for,
    raise_conflict,
)
from autoforge.effects import (
    MAX_BODY_CHARS,
    MAX_CONFLICT_REASON_CHARS,
    MAX_EFFECT_ATTEMPTS,
    MAX_EFFECT_STATE_CHARS,
    MAX_EFFECTS_PER_PLAN,
    MAX_TITLE_CHARS,
    AnalyzeContext,
    Binding,
    EffectKind,
    EffectOwner,
    EffectRecord,
    EntryObservation,
    FixContext,
    Stage,
    compose_append,
    load_phase_effects,
    load_records,
    plan_size_problem,
    progress_comment_body,
    sha256_text,
)
from autoforge.errors import (
    EffectConflictError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
    GitTransportError,
    StateError,
)
from autoforge.executor import ExecutionRequest, ExecutionResult, execute
from autoforge.git_transport import GitRemote, GitTransport
from autoforge.github import GitHubClient
from autoforge.replan_txn import ReplanAttestation, render_marker
from autoforge.result_parser import MAX_FINDINGS_PER_REVIEW
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
            "body": f"Implements {ISSUE}.\n\nCloses #2\n\n{IMPL_MARKER}" if body is None else body,
        },
    )


def adopt_record(base: str = ADOPT_BASE) -> EffectRecord:
    block = f"Closes #2\n\n{IMPL_MARKER}"
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
            "body": f"Deferred {finding_id} of {PR}.\n\n{marker}" if body is None else body,
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
            "body": f"Replaces {PR} for {ISSUE}.\n\nCloses #2\n\n{REPLAN_MARKER}",
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


K2, K3, K4, K5, K6, K7, K8 = (
    ImplementationPrCase(),
    AdoptPrCase(),
    ReviewCommentCase(),
    FollowUpIssueCase(),
    FollowUpAppendCase(),
    ReplacementPrCase(),
    ProgressCommentCase(),
)
CASES = [K2, K3, K4, K5, K6, K7, K8]
CREATE_CASES = [K2, K4, K5, K7, K8]
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


IMPL, PUSH, ADOPT, REVIEWED, FOLLOW, APPEND, REPLACE, PROGRESS = (
    _dict(impl_record),
    _dict(push_record),
    _dict(adopt_record),
    _dict(review_record),
    _dict(follow_up_record),
    _dict(append_record),
    _dict(replacement_record),
    _dict(progress_record),
)
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
        "does not carry its transaction marker",
    ),
    _p(
        "k7-marker-malformed",
        REPLACE,
        at("identity.transaction_marker", "<!-- autoforge-replan-transaction: {} -->"),
        "exactly one well-formed replan transaction marker",
    ),
    _p("k7-watermark-type", REPLACE, at("precondition.watermark", "42"), "must be an integer"),
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
    assert len(impl_record(body=_body_of_length(MAX_BODY_CHARS)).body) == MAX_BODY_CHARS
    with pytest.raises(StateError, match=f"over its bound of {MAX_BODY_CHARS}"):
        impl_record(body=_body_of_length(MAX_BODY_CHARS + 1))
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
            body=_body_of_length(MAX_BODY_CHARS, render_follow_up_marker(PR, f"R1-F{i}")),
        )
        for i in range(1, count + 1)
    ]
    return [r.to_dict() for r in records]


def test_the_total_size_bound_counts_payloads_and_the_context():
    """D2.4: payloads plus the context's stored bound stay under MAX_EFFECT_STATE_CHARS."""
    room = MAX_EFFECT_STATE_CHARS - FixContext.STORED_BOUND
    over = room // MAX_BODY_CHARS + 1
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
