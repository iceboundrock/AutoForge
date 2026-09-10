"""REPLAN_REEXECUTE policy, replacement verification, and crash recovery."""

from __future__ import annotations

import re

import pytest

from autoforge.config import ReplanConfig
from autoforge.errors import (
    ControlResultValidationError,
    GitHubError,
    GitHubUnavailableError,
    StateError,
)
from autoforge.loop_guard import (
    MAX_PERSISTED_FINDINGS_PER_ROUND,
    RESULT_NEEDS_FIX,
    review_record,
)
from autoforge.replan import (
    HistoricalReviewCollector,
    HistoricalReviewData,
    evaluate_replan_policy,
)
from autoforge.replan_txn import (
    MARKER_NAME,
    Disposition,
    MarkerScan,
    ReplanAttestation,
    ReplanStage,
    ReplanTransaction,
    has_close_receipt,
    render_close_receipt,
    render_marker,
    scan_replan_markers,
    select_bound_candidate,
)
from autoforge.result_parser import parse_control_result
from autoforge.transitions import Phase
from tests.conftest import (
    BRANCH,
    ISSUE,
    PR,
    SHA_A,
    SHA_B,
    SHA_C,
    FakeGitHub,
    block,
    comment_url,
    make_engine,
    review_comment_body,
)

REPLACEMENT_PR = "https://github.com/owner/repo/pull/43"
REPLACEMENT_BRANCH = "autoforge/retry-2-2"


def _history(counts: list[int]) -> list[dict]:
    records = []
    for round_number, count in enumerate(counts, 1):
        findings = [
            {
                "id": f"R{round_number}-F{n}",
                "classification": "nit",
                "required_resolution": f"resolve problem {round_number}-{n}",
            }
            for n in range(1, count + 1)
        ]
        records.append(review_record(round_number, SHA_A, RESULT_NEEDS_FIX, findings))
    return records


@pytest.mark.parametrize(
    "round_number,counts,expected",
    [
        (11, [2, 2, 2], "continue_fix"),
        (12, [2, 2], "continue_fix"),
        (12, [2, 1, 2], "replan"),
        (12, [2, 3, 2], "continue_fix"),
        (19, [9, 9, 9], "continue_fix"),
        (20, [9, 9, 9], "replan"),
        (21, [9, 9, 9], "replan"),
    ],
)
def test_replan_policy_threshold_boundaries(round_number, counts, expected):
    decision = evaluate_replan_policy(
        has_actionable_findings=True,
        current_review_round=round_number,
        review_history=_history(counts),
        escalation_count=0,
        config=ReplanConfig(),
    )
    assert decision.action == expected


def test_replan_policy_clean_disabled_and_limit():
    config = ReplanConfig()
    assert (
        evaluate_replan_policy(
            has_actionable_findings=False,
            current_review_round=20,
            review_history=_history([1, 1, 1]),
            escalation_count=0,
            config=config,
        ).action
        == "continue_fix"
    )
    config.enabled = False
    assert (
        evaluate_replan_policy(
            has_actionable_findings=True,
            current_review_round=20,
            review_history=_history([1, 1, 1]),
            escalation_count=0,
            config=config,
        ).action
        == "continue_fix"
    )
    config.enabled = True
    assert (
        evaluate_replan_policy(
            has_actionable_findings=True,
            current_review_round=20,
            review_history=_history([1, 1, 1]),
            escalation_count=config.max_replans_per_issue,
            config=config,
        ).action
        == "block_for_human"
    )


def test_stagnation_below_soft_threshold_does_not_replan():
    """N2: a workflow-stagnation verdict is gated behind soft_threshold."""
    config = ReplanConfig()
    reason = "review rounds 1, 2 requested identical resolutions"
    for round_number in range(1, config.soft_threshold):
        assert (
            evaluate_replan_policy(
                has_actionable_findings=True,
                current_review_round=round_number,
                review_history=_history([1] * round_number),
                escalation_count=0,
                config=config,
                workflow_stagnation_reason=reason,
            ).action
            == "continue_fix"
        ), f"round {round_number} must leave the loop bounds to block"
    decision = evaluate_replan_policy(
        has_actionable_findings=True,
        current_review_round=config.soft_threshold,
        review_history=_history([9] * config.soft_threshold),
        escalation_count=0,
        config=config,
        workflow_stagnation_reason=reason,
    )
    # At the threshold it escalates even though the finding counts are far
    # above max_findings_per_round, which the window rule alone would reject.
    assert decision.action == "replan" and decision.reason == "workflow_stagnation"


# =============================================================================
# Transaction harness
#
# Everything below drives the real engine. The scripted agent reads the
# transaction id out of the prompt it was given and publishes the marker, which
# is exactly the contract `replan_reexecute.md` states.
# =============================================================================

OTHER_PR = "https://github.com/owner/repo/pull/44"  # created after the replacement
EARLIER_PR = "https://github.com/owner/repo/pull/41"  # already open before the replan
TXN_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def _from_prompt(prompt: str, pattern: str) -> str:
    match = re.search(pattern, prompt)
    assert match, f"prompt does not contain {pattern!r}"
    return match.group(1)


def _marker_for_prompt(prompt: str, **override) -> str:
    """The attestation a compliant agent publishes, derived from its prompt."""
    payload = {
        "transaction_id": _from_prompt(prompt, r"`([0-9a-f]{32})`"),
        "execution_attempt": int(_from_prompt(prompt, r'"execution_attempt": (\d+)')),
        "findings_considered": int(
            _from_prompt(prompt, r'"historical_findings_considered": (\d+)')
        ),
        "unique_constraints": 2,
        "tests_passed": True,
    }
    payload.update(override)
    return render_marker(ReplanAttestation(**payload))


def _replacement_body(prompt: str, **override) -> str:
    marker = _marker_for_prompt(prompt, **override)
    return f"## Fresh Reimplementation\n\nReplaces {PR}.\n\n{marker}"


def _replan_payload(**override) -> dict:
    payload = {
        "phase": "REPLAN_REEXECUTE",
        "status": "success",
        "issue_url": ISSUE,
        "previous_pr_url": PR,
        "replacement_pr_url": REPLACEMENT_PR,
        "previous_branch": BRANCH,
        "replacement_branch": REPLACEMENT_BRANCH,
        "previous_head_sha": SHA_A,
        "replacement_head_sha": SHA_B,
        "execution_attempt": 2,
        "historical_findings_considered": 100,
        "unique_failure_constraints": 2,
        "previous_pr_disposition": "superseded",
        "fresh_review_round": 1,
        "verification": {"tests_run": ["pytest"], "tests_passed": True},
    }
    payload.update(override)
    return payload


def _trigger_review_payload() -> dict:
    return {
        "phase": "REVIEW",
        "status": "success",
        "round": 20,
        "reviewed_head_sha": SHA_A,
        "review_comment_url": comment_url(PR, 120),
        "needs_fix_round": True,
        "findings": [{"id": "R20-F1", "classification": "nit", "required_resolution": "x"}],
    }


def _park_at_hard_threshold(tmp_state_dir, gh, agent):
    eng = make_engine(tmp_state_dir, agent, github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.review_round = 19
    # This history can reach round 20 under the defaults: the changing count
    # avoids both workflow stagnation rules and its last three counts exceed
    # the soft-trigger finding limit.
    eng.state.review_history = _history([3 + (round_number % 2) for round_number in range(19)])
    return eng


def _replan_agent(gh, *, payload_over=None, marker_over=None, body=None, url=REPLACEMENT_PR):
    """REVIEW at the hard threshold, then a replacement PR the agent publishes."""

    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(_trigger_review_payload())
        assert req.phase == "REPLAN_REEXECUTE", req.phase
        gh.add_pr(
            url=url,
            head_sha=SHA_B,
            branch=REPLACEMENT_BRANCH,
            linked=[2],
            body=(
                body
                if body is not None
                else _replacement_body(req.prompt, **(marker_over or {}))
            ),
        )
        # A compliant agent repeats the marker's numbers in its CONTROL_RESULT;
        # `marker_over` moves only the marker, so a test can make the two
        # disagree deliberately.
        payload = _replan_payload(
            replacement_pr_url=url,
            historical_findings_considered=int(
                _from_prompt(req.prompt, r'"historical_findings_considered": (\d+)')
            ),
            unique_failure_constraints=2,
        )
        payload.update(payload_over or {})
        return block(payload)

    return agent


def _txn(eng) -> ReplanTransaction:
    return ReplanTransaction.from_dict(eng.state.replan_transaction)


def _closed_by_controller(gh, pr_url: str = PR, txn_id: str = TXN_ID) -> None:
    """A source PR as this transaction's own close leaves it: CLOSED + receipt."""
    gh.prs[pr_url].state = "CLOSED"
    gh.add_comment(pr_url, 900, f"Superseded.\n\n{render_close_receipt(txn_id)}")


def _closed_by_a_human(gh, pr_url: str = PR) -> None:
    """A source PR someone else closed: CLOSED, and no receipt anywhere."""
    gh.prs[pr_url].state = "CLOSED"
    gh.add_comment(pr_url, 901, "Closing this, we are going a different way.")


def _seed(eng, stage: ReplanStage, **over) -> ReplanTransaction:
    """Persist a transaction at ``stage``, as a crash at that point would leave it."""
    txn = ReplanTransaction(
        transaction_id=TXN_ID,
        stage=stage,
        issue_url=ISSUE,
        decision_head_sha=SHA_A,
        decision_branch=BRANCH,
        source_pr_url=PR,
        source_branch=BRANCH,
        source_head_sha=SHA_A,
        source_review_round=20,
        base_branch="main",
        evidence_finding_count=3,
        preexisting_pr_urls=[PR],
        pr_number_watermark=42,
        expected_execution_attempt=2,
        escalation={"trigger": "hard_review_round_threshold"},
    )
    if stage in (ReplanStage.VERIFIED, ReplanStage.SUPERSEDE_INTENT, ReplanStage.SUPERSEDED):
        txn.replacement_pr_url = REPLACEMENT_PR
        txn.replacement_branch = REPLACEMENT_BRANCH
        txn.replacement_head_sha = SHA_B
        txn.attested_findings_considered = 4
        txn.attested_unique_constraints = 2
    for key, value in over.items():
        setattr(txn, key, value)
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_issue_url = ISSUE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.replan_transaction = txn.to_dict()
    return txn


def _seeded_engine(tmp_state_dir, gh, stage, *, marker=True, **over):
    eng = make_engine(tmp_state_dir, ["the replan agent must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    body = (
        render_marker(
            ReplanAttestation(
                transaction_id=TXN_ID,
                execution_attempt=2,
                findings_considered=4,
                unique_constraints=2,
                tests_passed=True,
            )
        )
        if marker
        else "no marker here"
    )
    gh.add_pr(
        url=REPLACEMENT_PR, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, linked=[2], body=body
    )
    txn = _seed(eng, stage, **over)
    return eng, txn


def _assert_source_untouched(eng, gh):
    """The controller performed no destructive write and adopted nothing."""
    assert gh.prs[PR].state == "OPEN"
    assert gh.closed_prs == []
    assert gh.merges == []
    assert eng.state.current_pr_url == PR
    assert eng.state.current_head_sha == SHA_A
    assert eng.state.superseded_prs == []
    assert eng.state.escalation_count == 0


# =============================================================================
# Result schema (parser-level, before the controller ever sees the payload)
# =============================================================================


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda p: p.update(replacement_pr_url=PR), "replacement_pr_url must differ"),
        (lambda p: p.update(replacement_branch=BRANCH), "replacement_branch must differ"),
        (lambda p: p.update(fresh_review_round=2), "fresh_review_round"),
        (lambda p: p.pop("replacement_head_sha"), "replacement_head_sha"),
    ],
)
def test_replan_result_schema_rejects_invalid_cross_fields(mutate, needle):
    payload = _replan_payload()
    mutate(payload)
    with pytest.raises(ControlResultValidationError, match=needle):
        parse_control_result(block(payload), Phase.REPLAN_REEXECUTE)


# =============================================================================
# Happy path and policy routing
# =============================================================================


def test_hard_threshold_replaces_pr_and_starts_fresh_review(tmp_state_dir):
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert _txn(eng).stage is ReplanStage.PENDING
    assert _txn(eng).transaction_id == ""  # the id is created by the replan step

    out = eng.step()
    assert out.next_phase == "REVIEW"
    state = eng.state
    assert gh.prs[PR].state == "CLOSED"
    assert gh.closed_prs and REPLACEMENT_PR in gh.closed_prs[0][1]
    assert state.current_pr_url == REPLACEMENT_PR
    assert state.current_branch == REPLACEMENT_BRANCH
    assert state.current_head_sha == SHA_B
    assert state.review_round == 0  # next REVIEW is round 1 under persisted semantics
    assert state.review_history == [] and state.open_findings == []
    assert state.execution_attempt == 2 and state.escalation_count == 1
    assert state.replan_transaction == {}  # the transaction is retired on activation
    superseded = state.superseded_prs[0]
    assert superseded["pr_url"] == PR and superseded["head_sha"] == SHA_A
    assert superseded["replacement_pr_url"] == REPLACEMENT_PR
    assert len(superseded["transaction_id"]) == 32
    assert eng.provider.calls[-1].profile.name == "replan_reexecute"
    assert eng.provider.calls[-1].profile.model == "openai/gpt-5.6-terra"


def test_default_stagnation_routes_to_replan_through_engine(tmp_state_dir):
    """An identical-resolution streak at/after soft_threshold is a replan trigger."""
    gh = FakeGitHub()
    rounds = {"number": 0}

    def agent(req):
        if req.phase == "REVIEW":
            rounds["number"] += 1
            number = rounds["number"]
            sha = gh.prs[PR].head_sha
            gh.add_comment(
                PR,
                300 + number,
                review_comment_body(number, sha, True, [f"R{number}-F1"]),
            )
            return block(
                {
                    "phase": "REVIEW",
                    "status": "success",
                    "round": number,
                    "reviewed_head_sha": sha,
                    "review_comment_url": comment_url(PR, 300 + number),
                    "needs_fix_round": True,
                    "findings": [
                        {
                            "id": f"R{number}-F1",
                            "classification": "non-blocked",
                            "required_resolution": "add a regression test",
                        }
                    ],
                }
            )
        if req.phase == "FIX":
            gh.set_head(SHA_B)
            return block(
                {
                    "phase": "FIX",
                    "status": "success",
                    "previous_head_sha": SHA_A,
                    "new_head_sha": SHA_B,
                    "resolutions": [{"finding_id": "R1-F1", "resolution": "fixed"}],
                }
            )
        raise AssertionError(req.phase)

    eng = make_engine(tmp_state_dir, agent, github=gh)
    # Stagnation only escalates from soft_threshold onwards; lower it so the
    # streak is reached in two rounds instead of twelve.
    eng.config.review.replan.soft_threshold = 2
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    assert eng.step().next_phase == "FIX"
    assert eng.step().next_phase == "REVIEW"
    out = eng.step()
    assert out.next_phase == "REPLAN_REEXECUTE"
    assert "workflow_stagnation" in out.message


def test_replan_limit_blocks_through_engine(tmp_state_dir):
    gh = FakeGitHub()

    def agent(req):
        assert req.phase == "REVIEW"
        gh.add_comment(PR, 401, review_comment_body(2, SHA_A, True, ["R2-F1"]))
        return block(
            {
                "phase": "REVIEW",
                "status": "success",
                "round": 2,
                "reviewed_head_sha": SHA_A,
                "review_comment_url": comment_url(PR, 401),
                "needs_fix_round": True,
                "findings": [
                    {
                        "id": "R2-F1",
                        "classification": "non-blocked",
                        "required_resolution": "add a regression test",
                    }
                ],
            }
        )

    eng = make_engine(tmp_state_dir, agent, github=gh)
    eng.config.review.replan.soft_threshold = 2
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = SHA_A
    eng.state.review_round = 1
    eng.state.escalation_count = eng.config.review.replan.max_replans_per_issue
    eng.state.review_history = [
        review_record(
            1,
            SHA_A,
            RESULT_NEEDS_FIX,
            [
                {
                    "id": "R1-F1",
                    "classification": "non-blocked",
                    "required_resolution": "add a regression test",
                }
            ],
        )
    ]
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "replan_limit_exceeded" in eng.state.block_reason


# =============================================================================
# I1 evidence completeness: a replan is never traded for reduced findings
# =============================================================================


def test_truncated_round_evidence_refuses_replan_in_policy():
    config = ReplanConfig()
    history = _history([3] * 19)
    # One round could not be persisted in full; nothing else about the history
    # changes, so the same input would otherwise have escalated (see below).
    assert (
        evaluate_replan_policy(
            has_actionable_findings=True,
            current_review_round=config.hard_threshold,
            review_history=history,
            escalation_count=0,
            config=config,
        ).action
        == "replan"
    )
    history[7]["evidence_truncated"] = True
    decision = evaluate_replan_policy(
        has_actionable_findings=True,
        current_review_round=config.hard_threshold,
        review_history=history,
        escalation_count=0,
        config=config,
    )
    assert decision.action == "block_for_human"
    assert decision.reason == "replan_evidence_truncated"
    assert (decision.metadata or {})["truncated_evidence_rounds"] == [8]


def test_review_beyond_the_persisted_finding_bound_blocks_instead_of_replanning(tmp_state_dir):
    """I1 end-to-end: >100 findings cannot supersede the PR that carries them."""
    finding_ids = [f"R20-F{n}" for n in range(1, MAX_PERSISTED_FINDINGS_PER_ROUND + 2)]
    gh = FakeGitHub()

    def agent(req):
        assert req.phase == "REVIEW", f"{req.phase} must not be invoked"
        gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, finding_ids))
        payload = _trigger_review_payload()
        payload["findings"] = [
            {"id": fid, "classification": "blocked", "required_resolution": f"resolve {fid}"}
            for fid in finding_ids
        ]
        return block(payload)

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    out = eng.step()

    assert out.next_phase == "BLOCKED"
    assert "replan_evidence_truncated" in eng.state.block_reason
    assert "round(s) 20" in eng.state.block_reason
    _assert_source_untouched(eng, gh)
    assert len(eng.state.open_findings) == len(finding_ids)

    # And a `resume` straight into REPLAN_REEXECUTE cannot launder it: the
    # checkpoint that would authorise a replacement is refused too.
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.replan_transaction = ReplanTransaction(
        stage=ReplanStage.PENDING, escalation={"trigger": "hard_review_round_threshold"}
    ).to_dict()
    calls_before = len(eng.provider.calls)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "incomplete copy of the review" in eng.state.block_reason
    assert len(eng.provider.calls) == calls_before  # no replacement agent ran
    _assert_source_untouched(eng, gh)
    # The refusal is persisted, so a further resume replays it.
    assert _txn(eng).stage is ReplanStage.REJECTED
    eng.state.phase = Phase.REPLAN_REEXECUTE
    assert eng.step().next_phase == "BLOCKED"
    assert len(eng.provider.calls) == calls_before


def test_attestation_below_the_preserved_finding_count_is_refused(tmp_state_dir):
    """I1: the acknowledgement requirement is never lowered, on either channel."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(
        tmp_state_dir,
        gh,
        _replan_agent(
            gh,
            payload_over={"historical_findings_considered": 0, "unique_failure_constraints": 0},
            marker_over={"findings_considered": 0, "unique_constraints": 0},
        ),
    )
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "historical finding" in eng.state.block_reason
    _assert_source_untouched(eng, gh)


def test_marker_may_not_understate_findings_even_when_the_payload_is_honest(tmp_state_dir):
    """The published marker is the authority; a generous CONTROL_RESULT cannot cover it."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(
        tmp_state_dir, gh, _replan_agent(gh, marker_over={"findings_considered": 0})
    )
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "BLOCKED"
    assert "historical finding" in eng.state.block_reason
    _assert_source_untouched(eng, gh)


# =============================================================================
# I2 causal replacement provenance
# =============================================================================


def test_marker_binds_the_replacement_and_shape_alone_does_not(tmp_state_dir):
    """A single unmarked open PR is never adopted, however convincing it looks."""
    gh = FakeGitHub()
    eng, txn = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    # The PR is open, in this repository, linked to the issue, on a fresh
    # branch off the right base -- every shape signal a replacement has.
    candidate = gh.prs[REPLACEMENT_PR]
    assert candidate.is_open and candidate.linked_issue_numbers == [2]
    assert select_bound_candidate([candidate], txn).disposition is Disposition.NONE

    calls_before = len(eng.provider.calls)
    with pytest.raises(ControlResultValidationError):
        eng.step()  # falls through to the agent; the stub returns nothing usable
    # The replan was restarted rather than satisfied by the bystander.
    assert len(eng.provider.calls) > calls_before
    assert gh.prs[PR].state == "OPEN" and gh.closed_prs == []
    assert eng.state.current_pr_url == PR
    assert eng.state.superseded_prs == []


def test_preexisting_pr_is_never_adopted_even_carrying_a_copied_marker(tmp_state_dir):
    """I2: a PR that existed before the transaction cannot be its replacement."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    marker = render_marker(
        ReplanAttestation(
            transaction_id=TXN_ID,
            execution_attempt=2,
            findings_considered=4,
            unique_constraints=2,
            tests_passed=True,
        )
    )
    gh.add_pr(
        url=OTHER_PR, head_sha=SHA_C, branch="autoforge/2-older", linked=[2], body=marker
    )
    _seed(eng, ReplanStage.PREPARED, preexisting_pr_urls=[PR, OTHER_PR])
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "already existed" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_unrelated_preexisting_pr_does_not_block_a_healthy_replan(tmp_state_dir):
    """The converse of I2: an unmarked bystander is ignored, not treated as ambiguity."""
    gh = FakeGitHub()
    gh.add_pr(url=EARLIER_PR, head_sha=SHA_C, branch="autoforge/2-older", linked=[2])
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert gh.prs[EARLIER_PR].state == "OPEN"  # untouched, and never adopted
    assert gh.closed_prs == [(PR, gh.closed_prs[0][1])]
    assert eng.state.replan_transaction == {}  # retired on activation


def test_a_pr_predating_the_transaction_is_refused_even_if_it_was_never_an_issue_pr(
    tmp_state_dir,
):
    """I2: the snapshot of *issue* PRs is not the boundary; the PR number is.

    A PR that was open at PREPARED but neither linked to the issue nor named
    with the controller's prefix is absent from ``preexisting_pr_urls``. If the
    agent later adds both the issue link and the marker to it, only the
    creation-order watermark can still prove it is not a replacement.
    """
    gh = FakeGitHub()
    eng, txn = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    assert EARLIER_PR not in txn.preexisting_pr_urls
    gh.add_pr(
        url=EARLIER_PR,
        head_sha=SHA_C,
        branch="chore/unrelated-cleanup",
        linked=[2],  # the link the agent added afterwards
        body=render_marker(
            ReplanAttestation(
                transaction_id=TXN_ID,
                execution_attempt=2,
                findings_considered=4,
                unique_constraints=2,
                tests_passed=True,
            )
        ),
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "already existed" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_r8f2_a_closed_replacement_is_rejected_not_reimplemented(tmp_state_dir):
    """R8-F2: a marked replacement that was closed before recovery is evidence.

    The first agent creates the replacement, publishes the marker, and the PR
    is then closed before the controller resumes. The open listing is empty
    for this transaction, but the exhaustive all-states listing still finds
    the closed claimant. It must be durably rejected with the PR named --
    never treated as absent, which would invoke the agent a second time.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    gh.add_pr(
        url=REPLACEMENT_PR,
        head_sha=SHA_B,
        branch=REPLACEMENT_BRANCH,
        linked=[2],
        body=render_marker(
            ReplanAttestation(
                transaction_id=TXN_ID,
                execution_attempt=2,
                findings_considered=4,
                unique_constraints=2,
                tests_passed=True,
            )
        ),
        state="CLOSED",
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert REPLACEMENT_PR in eng.state.block_reason
    assert "CLOSED" in eng.state.block_reason
    assert "already exists" in eng.state.block_reason or "must be decided by a human" in (
        eng.state.block_reason
    )
    assert eng.provider.calls == []  # no second implementation attempt
    assert gh.prs[PR].state == "OPEN"  # source untouched
    assert eng.state.current_pr_url == PR
    assert eng.state.superseded_prs == []


def test_r8f2_a_preexisting_closed_pr_with_a_copied_marker_is_rejected(tmp_state_dir):
    """R8-F2: a closed pre-existing PR carrying the marker is not absence either."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    gh.add_pr(
        url=EARLIER_PR,
        head_sha=SHA_C,
        branch="chore/unrelated-cleanup",
        linked=[2],
        body=render_marker(
            ReplanAttestation(
                transaction_id=TXN_ID,
                execution_attempt=2,
                findings_considered=4,
                unique_constraints=2,
                tests_passed=True,
            )
        ),
        state="CLOSED",
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "already existed" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_a_transaction_without_a_watermark_can_bind_nothing(tmp_state_dir):
    """Without the watermark, provenance would fall back to shape."""
    txn = ReplanTransaction(
        stage=ReplanStage.PREPARED, transaction_id=TXN_ID, source_pr_url=PR, issue_url=ISSUE
    )
    selection = select_bound_candidate([], txn)
    assert selection.disposition is Disposition.REJECTED
    assert "no pull-request number watermark" in selection.reason


def test_the_watermark_is_checkpointed_before_the_agent_is_invoked(tmp_state_dir):
    """Ordering is the whole point: nothing under the watermark can carry the id."""
    gh = FakeGitHub()
    gh.add_pr(url=EARLIER_PR, head_sha=SHA_C, branch="chore/unrelated", linked=[])
    seen = {}
    inner = _replan_agent(gh)

    def agent(req):
        if req.phase == "REPLAN_REEXECUTE":
            seen["txn"] = _txn(eng)  # persisted PREPARED, before the agent acted
        return inner(req)

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "REVIEW"
    # Both PRs that existed at PREPARED (#41, #42) are under the watermark;
    # the replacement the agent then created (#43) is above it.
    assert seen["txn"].pr_number_watermark == 42
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert gh.prs[EARLIER_PR].state == "OPEN"


def test_marker_for_a_different_transaction_is_not_a_replacement(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    gh.prs[REPLACEMENT_PR].body = render_marker(
        ReplanAttestation(
            transaction_id="f" * 32,
            execution_attempt=2,
            findings_considered=4,
            unique_constraints=2,
            tests_passed=True,
        )
    )
    with pytest.raises(ControlResultValidationError):
        eng.step()  # falls through to the agent; the stub returns nothing usable
    assert gh.prs[PR].state == "OPEN" and gh.closed_prs == []
    assert eng.state.current_pr_url == PR


def test_two_bound_candidates_are_ambiguous_and_fail_closed(tmp_state_dir):
    """I7: the controller never guesses which of several claimants is real."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    gh.add_pr(
        url=OTHER_PR,
        head_sha=SHA_C,
        branch="autoforge/2-retry-3",
        linked=[2],
        body=gh.prs[REPLACEMENT_PR].body,
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "claim replan transaction" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


UNUSABLE_MARKERS = [
    # A JSON object the schema rejects.
    '<!-- autoforge-replan-transaction: {"transaction_id": "nope"} -->',
    # Payloads that are not JSON at all, or not an object. These must be
    # *unusable*, never invisible: a marker recognised only when it parses
    # would turn "this PR carries a broken attestation" into "this PR does not
    # claim the transaction", i.e. into another implementation attempt.
    "<!-- autoforge-replan-transaction: not-json -->",
    "<!-- autoforge-replan-transaction: -->",
    "<!-- autoforge-replan-transaction: [1, 2] -->",
]


@pytest.mark.parametrize("marker", UNUSABLE_MARKERS)
def test_malformed_marker_is_a_conclusive_rejection(tmp_state_dir, marker):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    gh.prs[REPLACEMENT_PR].body = marker
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "unusable replan marker" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


@pytest.mark.parametrize("marker", UNUSABLE_MARKERS)
def test_a_valid_marker_does_not_excuse_an_unusable_one_beside_it(tmp_state_dir, marker):
    """Fail closed: one body was supposed to carry exactly one attestation."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    gh.prs[REPLACEMENT_PR].body += "\n" + marker
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "alongside an unusable one" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_a_transaction_without_an_id_can_bind_nothing(tmp_state_dir):
    """Defence in depth: an empty id must not match an empty/absent marker."""
    txn = ReplanTransaction(stage=ReplanStage.PREPARED, source_pr_url=PR, issue_url=ISSUE)
    selection = select_bound_candidate([], txn)
    assert selection.disposition is Disposition.REJECTED
    assert "no usable transaction id" in selection.reason


def test_agent_claiming_an_unmarked_pr_is_rejected(tmp_state_dir):
    """The CONTROL_RESULT never selects the candidate; only the marker does."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh, body="no marker"))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "BLOCKED"
    assert "carries the marker" in eng.state.block_reason
    _assert_source_untouched(eng, gh)


def test_agent_claiming_a_different_pr_than_the_marked_one_is_rejected(tmp_state_dir):
    gh = FakeGitHub()

    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(_trigger_review_payload())
        gh.add_pr(
            url=REPLACEMENT_PR,
            head_sha=SHA_B,
            branch=REPLACEMENT_BRANCH,
            linked=[2],
            body=_replacement_body(req.prompt),
        )
        gh.add_pr(url=OTHER_PR, head_sha=SHA_C, branch="autoforge/2-retry-3", linked=[2])
        return block(
            _replan_payload(
                replacement_pr_url=OTHER_PR,
                replacement_branch="autoforge/2-retry-3",
                replacement_head_sha=SHA_C,
            )
        )

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "BLOCKED"
    assert "the PR bound to replan transaction" in eng.state.block_reason
    _assert_source_untouched(eng, gh)


def test_prompt_carries_the_transaction_id_and_the_exact_marker(tmp_state_dir):
    gh = FakeGitHub()
    seen = {}

    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(_trigger_review_payload())
        seen["prompt"] = req.prompt
        gh.add_pr(
            url=REPLACEMENT_PR,
            head_sha=SHA_B,
            branch=REPLACEMENT_BRANCH,
            linked=[2],
            body=_replacement_body(req.prompt),
        )
        return block(
            _replan_payload(
                historical_findings_considered=int(
                    _from_prompt(req.prompt, r'"historical_findings_considered": (\d+)')
                )
            )
        )

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    eng.step()
    eng.step()
    prompt = seen["prompt"]
    txn_id = _from_prompt(prompt, r"`([0-9a-f]{32})`")
    assert eng.state.superseded_prs[0]["transaction_id"] == txn_id
    assert MARKER_NAME in prompt
    assert txn_id in prompt


# =============================================================================
# I3 source checkpoint integrity
# =============================================================================


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda gh: gh.set_head(SHA_C, PR), "advanced from the checkpointed HEAD"),
        (
            lambda gh: setattr(gh.prs[PR], "head_ref", "somebody/else"),
            "moved from the checkpointed branch",
        ),
        (lambda gh: setattr(gh.prs[PR], "state", "CLOSED"), "expected OPEN at the checkpoint"),
        (lambda gh: setattr(gh.prs[PR], "state", "MERGED"), "already MERGED"),
    ],
)
def test_source_drift_refuses_to_close_the_source_pr(tmp_state_dir, mutate, needle):
    """I3: the source must still be exactly the implementation that was rejected."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    mutate(gh)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    assert gh.closed_prs == [] and gh.merges == []
    assert eng.state.current_pr_url == PR
    assert eng.state.superseded_prs == []
    assert eng.provider.calls == []


def test_a_human_closing_the_source_is_not_mistaken_for_our_supersede(tmp_state_dir):
    """The close side effect has an owner: without recorded intent it is not ours."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.prs[PR].state = "CLOSED"  # closed by a human, outside this transaction
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "it was not closed by this transaction" in eng.state.block_reason
    assert eng.state.current_pr_url == PR  # the replacement was never activated
    assert eng.state.superseded_prs == []


# =============================================================================
# I4 target checkpoint integrity
# =============================================================================


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda gh: gh.set_head(SHA_C, REPLACEMENT_PR), "advanced from the verified HEAD"),
        (
            lambda gh: setattr(gh.prs[REPLACEMENT_PR], "state", "CLOSED"),
            "expected OPEN",
        ),
        (
            lambda gh: setattr(gh.prs[REPLACEMENT_PR], "head_ref", "autoforge/other"),
            "moved from the checkpointed branch",
        ),
        (
            lambda gh: setattr(gh.prs[REPLACEMENT_PR], "base_ref", "release"),
            "verified default branch",
        ),
        (
            lambda gh: setattr(gh.prs[REPLACEMENT_PR], "linked_issue_numbers", []),
            "not linked to issue",
        ),
    ],
)
def test_target_drift_after_the_checkpoint_blocks_instead_of_closing(
    tmp_state_dir, mutate, needle
):
    """I4/I8: the checkpoint is revalidated immediately before the irreversible write."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    mutate(gh)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    assert gh.prs[PR].state == "OPEN" and gh.closed_prs == []
    assert eng.state.current_pr_url == PR and eng.state.current_head_sha == SHA_A
    assert eng.state.superseded_prs == []
    assert eng.provider.calls == []


def _restated_marker(findings: int, unique: int) -> str:
    return render_marker(
        ReplanAttestation(
            transaction_id=TXN_ID,
            execution_attempt=2,
            findings_considered=findings,
            unique_constraints=unique,
            tests_passed=True,
        )
    )


@pytest.mark.parametrize(
    "body,needle",
    [
        ("the marker is gone", "no longer carries the marker"),
        (_restated_marker(4, 2) + "\n" + UNUSABLE_MARKERS[1], "unusable replan marker"),
        (_restated_marker(4, 2) * 2, "carries 2 markers"),
        (_restated_marker(9, 2), "now attests findings_considered=9"),
        (_restated_marker(4, 1), "unique_constraints=1"),
    ],
)
def test_the_marker_is_revalidated_on_the_last_read_before_the_close(
    tmp_state_dir, body, needle
):
    """R6-F2: the objective facts are not provenance -- the marker is.

    Everything ``verify_target_pr`` checks (identity, state, branch, base,
    linkage, HEAD) survives an edit that removes or rewrites the attestation,
    so a body edited after ``VERIFIED`` would otherwise let the controller
    close the source for a PR that no longer proves it belongs to this replan.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.prs[REPLACEMENT_PR].body = body
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    assert gh.prs[PR].state == "OPEN" and gh.closed_prs == []
    assert eng.state.current_pr_url == PR
    assert eng.state.superseded_prs == []


# =============================================================================
# The close window: GitHub has no conditional close, so the compare is
# completed after the write and a checkpoint that moved is compensated.
# =============================================================================


@pytest.mark.parametrize(
    "race,needle",
    [
        (lambda gh: gh.set_head(SHA_C, PR), "inside the close window"),
        (
            lambda gh: setattr(gh.prs[PR], "head_ref", "somebody/else"),
            "moved from the checkpointed branch",
        ),
        (lambda gh: gh.set_head(SHA_C, REPLACEMENT_PR), "advanced from the verified HEAD"),
        (
            lambda gh: setattr(gh.prs[REPLACEMENT_PR], "body", "marker removed"),
            "no longer carries the marker",
        ),
        (
            lambda gh: setattr(gh.prs[REPLACEMENT_PR], "linked_issue_numbers", []),
            "not linked to issue",
        ),
    ],
)
def test_a_mutation_racing_the_close_is_undone_instead_of_accepted(tmp_state_dir, race, needle):
    """R6-F1: a change landing between the last read and ``gh pr close``.

    ``gh pr close`` takes no precondition, so this window cannot be closed by
    checking harder beforehand. The comparison is completed afterwards, and a
    checkpoint that moved inside the window makes the close *wrong* -- so it is
    undone and the run stops, rather than the replacement being activated on
    facts that had already changed.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.close_race = race
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    assert "the close was undone" in eng.state.block_reason
    assert [url for url, _ in gh.closed_prs] == [PR]
    assert [url for url, _ in gh.reopened_prs] == [PR]
    assert gh.prs[PR].state == "OPEN"  # the source is back, branch never deleted
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert eng.state.current_pr_url == PR and eng.state.current_head_sha == SHA_A
    assert eng.state.superseded_prs == [] and eng.state.escalation_count == 0


@pytest.mark.parametrize(
    "setup,needle",
    [
        (
            lambda gh: setattr(gh, "reopen_error", "HTTP 403: Resource not accessible"),
            "could not be reopened",
        ),
        (
            lambda gh: setattr(gh, "reopen_leaves_closed", True),
            "is still CLOSED after the reopen attempt",
        ),
    ],
)
def test_an_undo_that_did_not_land_is_reported_as_such(tmp_state_dir, setup, needle):
    """The controller never claims a compensation it did not observe."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.close_race = lambda g: g.set_head(SHA_C, PR)
    setup(gh)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    assert "reopened by hand" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert eng.state.superseded_prs == []


def test_a_transient_failure_while_undoing_the_close_stays_resumable(tmp_state_dir):
    """The undo is a GitHub write like any other: unknown is not refused."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.close_race = lambda g: g.set_head(SHA_C, PR)
    gh.reopen_error = GitHubUnavailableError("gh: 502 Bad Gateway")
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    # F2: the *decision* to undo is durable even though the write is unknown,
    # so the resume can only finish undoing -- it can never supersede instead.
    txn = _txn(eng)
    assert txn.stage is ReplanStage.COMPENSATING
    assert "was never reviewed" in txn.compensation_reason
    assert gh.prs[PR].state == "CLOSED"
    gh.reopen_error = ""
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "the close was undone" in eng.state.block_reason
    assert gh.prs[PR].state == "OPEN"
    assert len(gh.closed_prs) == 1  # the resume re-reads, it never closes again
    assert eng.state.superseded_prs == []


def test_a_clean_close_window_still_supersedes(tmp_state_dir):
    """The post-close comparison must not make the healthy path any harder."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert gh.prs[PR].state == "CLOSED" and gh.reopened_prs == []
    assert eng.state.current_pr_url == REPLACEMENT_PR


@pytest.mark.parametrize(
    "field,value,needle",
    [
        ("base_ref", "release", "verified default branch"),
        # A closed or unlinked PR is not even a candidate, so the refusal is
        # "nothing carries the marker" -- still fail-closed, never adopted.
        ("state", "CLOSED", "carries the marker"),
        # Found repository-wide and refused on its merits, not skipped by an
        # issue-shaped filter: see the crash-recovery test for why that matters.
        ("linked_issue_numbers", [], "not linked to issue #2"),
    ],
)
def test_target_facts_are_checked_on_the_apply_path_too(tmp_state_dir, field, value, needle):
    gh = FakeGitHub()

    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(_trigger_review_payload())
        gh.add_pr(
            url=REPLACEMENT_PR,
            head_sha=SHA_B,
            branch=REPLACEMENT_BRANCH,
            linked=[2],
            body=_replacement_body(req.prompt),
        )
        setattr(gh.prs[REPLACEMENT_PR], field, value)
        return block(_replan_payload())

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    eng.step()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    _assert_source_untouched(eng, gh)


def test_replacement_in_another_repository_is_refused(tmp_state_dir):
    foreign = "https://github.com/other/repo/pull/43"
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh, url=foreign))
    eng.step()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    # A PR in another repository is never even a candidate for this issue.
    assert "carries the marker" in eng.state.block_reason
    _assert_source_untouched(eng, gh)


@pytest.mark.parametrize(
    "payload_over,needle",
    [
        ({"replacement_head_sha": SHA_C}, "replacement HEAD mismatch"),
        ({"replacement_branch": "autoforge/claimed-else"}, "replacement branch mismatch"),
        ({"previous_head_sha": "f" * 40}, "previous_head_sha"),
        ({"previous_branch": "autoforge/2-other"}, "previous_branch"),
        ({"execution_attempt": 5}, "execution_attempt must be 2"),
    ],
)
def test_agent_claims_are_cross_checked_against_the_checkpoint(
    tmp_state_dir, payload_over, needle
):
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh, payload_over=payload_over))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    _assert_source_untouched(eng, gh)


@pytest.mark.parametrize(
    "payload_over",
    [
        {"historical_findings_considered": 99},
        {"unique_failure_constraints": 0},
    ],
)
def test_control_result_counts_must_equal_the_published_marker(tmp_state_dir, payload_over):
    """The two channels must tell one story, even when each passes on its own.

    ``historical_findings_considered=99`` clears the preserved-count floor and
    ``unique_failure_constraints=0`` is individually harmless, but neither
    matches the attestation the controller read back from GitHub -- so the
    numbers were not produced by one honest accounting.
    """
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh, payload_over=payload_over))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "disagrees with the replan marker" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    _assert_source_untouched(eng, gh)


@pytest.mark.parametrize(
    "payload_over,marker_over,needle",
    [
        ({"verification": {"tests_run": [], "tests_passed": False}}, {}, "tests_passed=false"),
        ({}, {"tests_passed": False}, "tests_passed=false"),
        ({}, {"execution_attempt": 9}, "execution_attempt"),
        ({}, {"unique_constraints": 9999}, "unique_constraints"),
    ],
)
def test_failed_or_inconsistent_attestation_is_refused(
    tmp_state_dir, payload_over, marker_over, needle
):
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(
        tmp_state_dir, gh, _replan_agent(gh, payload_over=payload_over, marker_over=marker_over)
    )
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    _assert_source_untouched(eng, gh)


# =============================================================================
# I5 rejection monotonicity / I9 crash idempotency
#
# Crash windows, in lifecycle order. Each seeds the transaction exactly as a
# crash at that point would leave it and asserts what `resume` may and may not
# do. "The agent must not run" is asserted by the scripted provider itself.
# =============================================================================


def test_w1_crash_before_prepare_still_prepares_and_invokes_once(tmp_state_dir):
    """Window 1: PENDING persisted, nothing checkpointed."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert _txn(eng).stage is ReplanStage.PENDING
    assert eng.step().next_phase == "REVIEW"
    replan_calls = [c for c in eng.provider.calls if c.phase == "REPLAN_REEXECUTE"]
    assert len(replan_calls) == 1


def test_w2_crash_after_prepare_before_invocation_invokes_the_agent(tmp_state_dir):
    """Window 2: PREPARED with no PR anywhere -> the agent still has to run."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["the agent runs exactly once"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    _seed(eng, ReplanStage.PREPARED)
    with pytest.raises(ControlResultValidationError):
        eng.step()  # the scripted stub is not a valid CONTROL_RESULT
    assert eng.provider.calls  # the agent runs: nothing was bound to the transaction
    assert gh.prs[PR].state == "OPEN" and gh.closed_prs == []


def test_w3_crash_between_the_agents_write_and_its_control_result(tmp_state_dir):
    """Window 3: the replacement exists and is marked; the agent must not re-run."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert eng.provider.calls == []  # I9: no second implementation attempt
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert gh.prs[PR].state == "CLOSED"
    assert len(gh.closed_prs) == 1


def test_w3b_a_marked_replacement_the_agent_never_linked_is_found_not_reimplemented(
    tmp_state_dir,
):
    """Window 3b / R6-F3: creating the PR and linking it are separate writes.

    A crash between them leaves a PR that carries the transaction marker but
    matches no issue-shaped filter. Discovering the candidate set through such
    a filter would report "nothing exists" and start a *second* implementation
    attempt on top of the first -- the one outcome crash idempotency forbids.
    Found repository-wide, it is refused on its merits instead, with the PR
    named for the human who has to reconcile it.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    replacement = gh.prs[REPLACEMENT_PR]
    replacement.linked_issue_numbers = []  # `gh pr create` landed, the link did not
    replacement.head_ref = "wip/rewrite"  # ... and the branch says nothing either
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "not linked to issue #2" in eng.state.block_reason
    assert REPLACEMENT_PR in eng.state.block_reason
    assert eng.provider.calls == []  # no second implementation attempt
    _assert_source_untouched(eng, gh)


def test_w4_recovery_holds_the_same_bar_as_the_control_result_path(tmp_state_dir):
    """Window 4: the agent published a replacement whose attestation is bad.

    There is no CONTROL_RESULT to consult after the crash, so this is exactly
    the case a shape-based recovery used to wave through. The marker carries
    the attestation, so recovery refuses it on the same grounds ``_apply_replan``
    would have.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    gh.prs[REPLACEMENT_PR].body = render_marker(
        ReplanAttestation(
            transaction_id=TXN_ID,
            execution_attempt=2,
            findings_considered=4,
            unique_constraints=2,
            tests_passed=False,  # the replacement's own tests did not pass
        )
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "tests_passed=false" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)
    assert _txn(eng).stage is ReplanStage.REJECTED


def test_w5_crash_after_verification_completes_the_supersede(tmp_state_dir):
    """Window 5: VERIFIED persisted, the close not yet attempted."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    assert eng.step().next_phase == "REVIEW"
    assert eng.provider.calls == []
    assert gh.prs[PR].state == "CLOSED" and len(gh.closed_prs) == 1
    assert eng.state.current_pr_url == REPLACEMENT_PR


def test_w6_crash_after_intent_but_before_the_close_landed(tmp_state_dir):
    """Window 6: intent recorded, source still OPEN -> the close is retried once."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="2026-01-01T00:00:00+00:00"
    )
    assert eng.step().next_phase == "REVIEW"
    assert len(gh.closed_prs) == 1
    assert gh.prs[PR].state == "CLOSED"
    assert eng.state.current_pr_url == REPLACEMENT_PR


def test_w7_crash_after_the_close_landed_adopts_it_without_closing_again(tmp_state_dir):
    """Window 7: intent plus the published receipt prove this close was ours."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_controller(gh)  # the write landed before the crash
    assert eng.step().next_phase == "REVIEW"
    assert gh.closed_prs == []  # I9: exactly once, never twice
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert eng.state.superseded_prs[0]["pr_url"] == PR


def test_w8_crash_after_supersede_before_activation(tmp_state_dir):
    """Window 8: SUPERSEDED persisted; activation is the only work left."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDED, superseded_at="2026-01-01T00:00:00+00:00"
    )
    gh.prs[PR].state = "CLOSED"
    assert eng.step().next_phase == "REVIEW"
    assert gh.closed_prs == []
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert eng.state.review_round == 0 and eng.state.escalation_count == 1


def test_w9_activation_is_idempotent_across_a_repeated_resume(tmp_state_dir):
    """Window 9: a resume after activation must not count the supersede twice."""
    gh = FakeGitHub()
    eng, txn = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDED, superseded_at="2026-01-01T00:00:00+00:00"
    )
    gh.prs[PR].state = "CLOSED"
    eng.step()
    escalations = eng.state.escalation_count
    superseded = list(eng.state.superseded_prs)
    # Replay the same durable transaction as a duplicated crash-resume would.
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.replan_transaction = txn.to_dict()
    eng.step()
    assert eng.state.superseded_prs == superseded  # one entry, not two
    assert eng.state.escalation_count == escalations + 1  # only the replay counter moves
    assert gh.closed_prs == []


@pytest.mark.parametrize(
    "payload_over,marker_over,needle",
    [
        ({}, {"tests_passed": False}, "tests_passed=false"),
        ({"execution_attempt": 5}, {}, "execution_attempt must be 2"),
        (
            {"historical_findings_considered": 0, "unique_failure_constraints": 0},
            {"findings_considered": 0, "unique_constraints": 0},
            "historical finding",
        ),
        ({"previous_head_sha": "f" * 40}, {}, "previous_head_sha"),
    ],
)
def test_w10_rejected_replacement_is_not_laundered_by_a_resume(
    tmp_state_dir, payload_over, marker_over, needle
):
    """Window 10 / I5: a persisted REJECTED decision is replayed, never re-derived."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(
        tmp_state_dir, gh, _replan_agent(gh, payload_over=payload_over, marker_over=marker_over)
    )
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED

    # The rejection survives a resume even though every objective GitHub fact
    # about the replacement still looks perfectly valid.
    calls_before = len(eng.provider.calls)
    eng.state.phase = Phase.REPLAN_REEXECUTE
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    assert len(eng.provider.calls) == calls_before  # the agent is not re-invoked
    _assert_source_untouched(eng, gh)


def test_an_unknown_persisted_stage_fails_closed(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    eng.state.replan_transaction["stage"] = "from_a_future_version"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "unknown stage" in eng.state.block_reason
    assert gh.closed_prs == [] and eng.state.current_pr_url == PR


def test_replan_reexecute_without_a_transaction_fails_loudly(tmp_state_dir):
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    with pytest.raises(StateError, match="without a replan transaction"):
        eng.step()
    assert eng.provider.calls == []


# =============================================================================
# I6 unknown is not rejection / I8 destructive-write outcomes
# =============================================================================


def test_transient_github_failure_while_listing_candidates_stays_resumable(tmp_state_dir):
    """I6: a flaky `gh pr list` must not permanently block a healthy replan."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    real = gh.list_open_prs
    calls = {"n": 0}

    def flaky(repo, limit=100, *, strict=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GitHubUnavailableError("gh: connection reset")
        return real(repo, limit, strict=strict)

    gh.list_open_prs = flaky
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert eng.state.phase == Phase.REPLAN_REEXECUTE and not eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.PREPARED  # nothing was decided
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR and gh.prs[PR].state == "CLOSED"


def test_transient_github_failure_before_the_close_stays_resumable(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.get_pr_failures = 1
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert eng.state.phase == Phase.REPLAN_REEXECUTE and not eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.VERIFIED
    assert gh.closed_prs == []
    assert eng.step().next_phase == "REVIEW"


def test_transient_failure_during_the_close_leaves_recorded_intent(tmp_state_dir):
    """I8: the intent is durable before the write, so the outcome is resolvable."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.close_error = GitHubUnavailableError("gh: 502 Bad Gateway")
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert _txn(eng).stage is ReplanStage.SUPERSEDE_INTENT
    assert _txn(eng).close_intent_at
    assert eng.state.phase == Phase.REPLAN_REEXECUTE
    # The close never landed; the retry completes it exactly once.
    gh.close_error = ""
    assert eng.step().next_phase == "REVIEW"
    assert gh.prs[PR].state == "CLOSED"


def test_conclusive_github_failure_while_listing_candidates_blocks(tmp_state_dir):
    """The transient carve-out must not weaken the conclusive path."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)

    def denied(repo, limit=100, *, strict=False):
        raise GitHubError("HTTP 403: Resource not accessible by integration")

    gh.list_open_prs = denied
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "cannot list replacement PR candidates" in eng.state.block_reason
    _assert_source_untouched(eng, gh)


def test_a_truncated_candidate_listing_blocks_instead_of_replanning_again(tmp_state_dir):
    """"No candidate" decides whether the agent runs again, so it must be complete.

    A listing that hit its limit cannot distinguish "the replacement does not
    exist" from "it was past the limit"; adopting the former would start a
    second implementation attempt on top of the first.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    gh.pr_listing_truncated = True
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "cannot list replacement PR candidates" in eng.state.block_reason
    assert "truncated" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_a_truncated_listing_at_prepare_blocks_before_any_agent_runs(tmp_state_dir):
    gh = FakeGitHub()
    eng = _pending_at_the_source(tmp_state_dir, gh)
    gh.pr_listing_truncated = True
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "cannot checkpoint the replan source" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_a_failing_close_blocks_and_does_not_activate(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.close_error = "HTTP 403: Resource not accessible by integration"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "closing source PR" in eng.state.block_reason
    assert gh.prs[PR].state == "OPEN"
    assert eng.state.current_pr_url == PR and eng.state.superseded_prs == []


def test_a_close_that_silently_left_the_pr_open_blocks(tmp_state_dir):
    """`gh` exiting 0 is not proof: the state is re-read from GitHub."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.close_leaves_open = True
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "expected CLOSED" in eng.state.block_reason
    assert eng.state.current_pr_url == PR and eng.state.superseded_prs == []


def test_conclusive_failure_reading_the_source_at_prepare_blocks(tmp_state_dir):
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.replan_transaction = ReplanTransaction(
        stage=ReplanStage.PENDING, escalation={"trigger": "hard_review_round_threshold"}
    ).to_dict()
    gh.get_pr_error = GitHubError("HTTP 404: Not Found")
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "cannot checkpoint the replan source" in eng.state.block_reason
    assert eng.provider.calls == []


def _pending_at_the_source(tmp_state_dir, gh):
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.replan_transaction = ReplanTransaction(
        stage=ReplanStage.PENDING,
        decision_head_sha=SHA_A,
        decision_branch=BRANCH,
        escalation={"trigger": "hard_review_round_threshold"},
    ).to_dict()
    return eng


def test_conclusive_failure_collecting_the_review_evidence_blocks(tmp_state_dir):
    """I5: a conclusive failure is a persisted refusal, not an endless retry."""
    gh = FakeGitHub()
    eng = _pending_at_the_source(tmp_state_dir, gh)
    gh.comments_error = GitHubError("HTTP 403: Resource not accessible by integration")
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "cannot collect the review evidence" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_transient_failure_collecting_the_review_evidence_stays_resumable(tmp_state_dir):
    """I6: the evidence is unread, not incomplete -- nothing is decided."""
    gh = FakeGitHub()
    eng = _pending_at_the_source(tmp_state_dir, gh)
    gh.comments_error = GitHubUnavailableError("gh: connection reset")
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert eng.state.phase == Phase.REPLAN_REEXECUTE and not eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.PENDING  # no id was burned, nothing refused
    assert eng.provider.calls == []


def test_a_source_pr_that_moved_before_prepare_is_refused(tmp_state_dir):
    """I3 at the other end: the checkpoint is taken from GitHub, not from state."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, state="CLOSED", linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.replan_transaction = ReplanTransaction(
        stage=ReplanStage.PENDING, escalation={"trigger": "hard_review_round_threshold"}
    ).to_dict()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "only an OPEN PR at a readable HEAD can be superseded" in eng.state.block_reason
    assert eng.provider.calls == []


@pytest.mark.parametrize(
    "drift,needle",
    [
        (lambda pr: setattr(pr, "head_sha", SHA_B), "never reviewed against this decision"),
        (lambda pr: setattr(pr, "head_ref", "autoforge/2-hand-edited"), "is on branch"),
    ],
)
def test_source_moving_between_the_review_and_the_prepare_is_refused(
    tmp_state_dir, drift, needle
):
    """I3 at the decision point: the checkpoint may only capture what was reviewed.

    Between the review that routed here and the REPLAN_REEXECUTE step, a human
    can push to the source branch. Checkpointing the *current* HEAD would let
    the controller close a revision this replan decision never saw.
    """
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    txn = _txn(eng)
    assert txn.stage is ReplanStage.PENDING
    assert txn.decision_head_sha == SHA_A and txn.decision_branch == BRANCH

    drift(gh.prs[PR])
    calls_before = len(eng.provider.calls)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    assert len(eng.provider.calls) == calls_before  # no replacement was attempted
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert gh.prs[PR].state == "OPEN" and gh.closed_prs == []
    assert eng.state.superseded_prs == []


def test_a_transaction_that_never_recorded_its_decision_point_is_refused(tmp_state_dir):
    """A hand-edited or pre-upgrade journal cannot prove what was reviewed."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.replan_transaction = ReplanTransaction(
        stage=ReplanStage.PENDING, escalation={"trigger": "hard_review_round_threshold"}
    ).to_dict()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "does not record the reviewed HEAD" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_a_checkpoint_that_disagrees_with_the_decision_point_never_closes(tmp_state_dir):
    """Defence in depth on the close itself, not only on the prepare."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED, decision_head_sha=SHA_C)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "is not the reviewed HEAD" in eng.state.block_reason
    _assert_source_untouched(eng, gh)


# =============================================================================
# Prompt rendering and untrusted-data handling
# =============================================================================


def test_replan_prompt_uses_controller_checkpoint_not_agent_input(tmp_state_dir):
    eng = make_engine(tmp_state_dir, [], github=FakeGitHub())
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.execution_attempt = 2
    _seed(eng, ReplanStage.PREPARED, evidence_finding_count=4)
    prompt = eng.render_prompt_for(Phase.REPLAN_REEXECUTE)
    assert '"execution_attempt": 2' in prompt
    assert '"historical_findings_considered": 4' in prompt
    assert TXN_ID in prompt
    assert MARKER_NAME in prompt


def test_replan_prompt_before_the_checkpoint_shows_placeholders(tmp_state_dir):
    """Dry-run/plan rendering must not invent a transaction id."""
    eng = make_engine(tmp_state_dir, [], github=FakeGitHub())
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.replan_transaction = ReplanTransaction(stage=ReplanStage.PENDING).to_dict()
    prompt = eng.render_prompt_for(Phase.REPLAN_REEXECUTE)
    assert "(generated at execution)" in prompt
    assert not re.search(r"`[0-9a-f]{32}`", prompt)


@pytest.mark.parametrize("length", list(range(3, 13)))
def test_historical_rendering_cannot_close_untrusted_fence(length):
    """No residual run of 3+ tildes may survive, at any run length or indent."""
    run = "~" * length
    history = HistoricalReviewData(
        findings=[
            {"id": "R1-F1", "round": 1, "classification": "nit", "required_resolution": run}
        ],
        observations=[f"{run}\nuntrusted", f"   {run}mermaid\nx"],
        verification_failures=[run],
    )
    rendered = "\n".join(
        (
            history.render_findings(),
            history.render_observations(),
            history.render_verification_failures(),
        )
    )
    assert not re.search(r"~{3,}", rendered)
    # The text itself is preserved, only broken up.
    assert "untrusted" in rendered and "mermaid" in rendered


def test_recorded_finding_count_matches_what_the_prompt_shows(tmp_state_dir):
    """The controller demands exactly the findings it actually rendered."""
    findings = [
        {"id": f"R1-F{n}", "classification": "nit", "required_resolution": f"fix {n}"}
        for n in range(1, MAX_PERSISTED_FINDINGS_PER_ROUND + 1)
    ]
    record = review_record(1, SHA_A, RESULT_NEEDS_FIX, findings)
    assert record["finding_count"] == len(record["findings"]) == MAX_PERSISTED_FINDINGS_PER_ROUND
    gh = FakeGitHub()
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    data = HistoricalReviewCollector(gh).collect(PR, [record], [])
    assert data.recorded_finding_count == len(data.findings) == MAX_PERSISTED_FINDINGS_PER_ROUND


# =============================================================================
# Marker parsing (pure)
# =============================================================================


def test_marker_scanner_rejects_payloads_that_are_not_attestations():
    bad = [
        "<!-- autoforge-replan-transaction: {not json} -->",
        '<!-- autoforge-replan-transaction: {"transaction_id": "SHORT"} -->',
        '<!-- autoforge-replan-transaction: {"transaction_id": "' + "a" * 32 + '"} -->',
        '<!-- autoforge-replan-transaction: {"transaction_id": "'
        + "a" * 32
        + '", "execution_attempt": 1, "findings_considered": 1, "unique_constraints": 1,'
        ' "tests_passed": "yes"} -->',
    ]
    for body in bad:
        scan = scan_replan_markers(body)
        assert scan.attestations == [] and scan.malformed, body
    # A marker is recognised by its *name*, not by a payload that happens to
    # parse: a complete marker carrying anything else is unusable, never
    # absent. Reporting it as absent would downgrade a conclusive refusal to
    # "this PR does not claim the transaction".
    for body in (
        "<!-- autoforge-replan-transaction: not json -->",
        "<!-- autoforge-replan-transaction: -->",
        "<!--autoforge-replan-transaction:[1, 2]-->",
        '<!-- autoforge-replan-transaction: "a string" -->',
    ):
        scan = scan_replan_markers(body)
        assert scan.attestations == [] and len(scan.malformed) == 1, body
    # Text that is not a marker at all stays invisible.
    assert scan_replan_markers("<!-- unrelated: {} -->\nprose") == MarkerScan([], [])


def test_marker_scanner_reads_a_well_formed_attestation():
    attestation = ReplanAttestation(
        transaction_id=TXN_ID,
        execution_attempt=3,
        findings_considered=7,
        unique_constraints=4,
        tests_passed=True,
    )
    scan = scan_replan_markers(f"intro\n{render_marker(attestation)}\noutro")
    assert scan.attestations == [attestation] and scan.malformed == []


def test_marker_regex_cannot_swallow_the_rest_of_a_body():
    """An unterminated marker must not consume an adjacent valid one."""
    good = render_marker(
        ReplanAttestation(
            transaction_id=TXN_ID,
            execution_attempt=2,
            findings_considered=1,
            unique_constraints=1,
            tests_passed=True,
        )
    )
    scan = scan_replan_markers('<!-- autoforge-replan-transaction: {"a": 1}\n' + good)
    assert [a.transaction_id for a in scan.attestations] == [TXN_ID]


# =============================================================================
# Round 7: ownership of the close, durable compensation, checked activation,
# and complete marker classification.
# =============================================================================


def test_f1_the_controller_close_publishes_an_ownership_receipt(tmp_state_dir):
    """The close carries the only durable proof that the controller made it.

    R8-F1: the receipt is posted *after* the close is observed, never inside
    the `gh pr close --comment` that predates it.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    assert eng.step().next_phase == "REVIEW"
    (_, close_comment) = gh.closed_prs[0]
    assert render_close_receipt(TXN_ID) not in close_comment
    assert REPLACEMENT_PR in close_comment  # the close still names the replacement
    assert gh.commented_prs and gh.commented_prs[0][0] == PR
    assert render_close_receipt(TXN_ID) in gh.commented_prs[0][1]
    assert has_close_receipt([c.body for c in gh.get_pr_comments(PR)], TXN_ID)


def test_f1_a_human_close_inside_the_intent_window_is_never_adopted(tmp_state_dir):
    """Crash before `gh pr close`, a human closes the source, then resume.

    The persisted intent proves only that the controller *meant* to close. A
    source that is CLOSED without this transaction's receipt was closed by
    somebody else, and superseding on it would activate a replacement on the
    strength of an action the controller never took.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_a_human(gh)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "carries no close receipt" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert eng.state.current_pr_url == PR  # the replacement was not activated
    assert eng.state.superseded_prs == []
    assert gh.reopened_prs == []  # never undo a close the controller did not make


def test_f1_a_receipt_for_another_transaction_does_not_count(tmp_state_dir):
    """Attribution is per transaction, not "some AutoForge close happened"."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_controller(gh, txn_id="f" * 32)
    assert eng.step().next_phase == "BLOCKED"
    assert "carries no close receipt" in eng.state.block_reason
    assert eng.state.superseded_prs == []


def test_f1_an_unreadable_comment_list_is_unknown_not_unowned(tmp_state_dir):
    """A transient read cannot decide ownership; a conclusive one refuses."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_controller(gh)
    gh.comments_error = GitHubUnavailableError("gh: 502 Bad Gateway")
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert _txn(eng).stage is ReplanStage.SUPERSEDE_INTENT  # still resumable
    gh.comments_error = GitHubError("gh: not found")
    assert eng.step().next_phase == "BLOCKED"
    assert "could not be read" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED


def test_r8f1_a_receipt_that_predates_the_close_proves_nothing(tmp_state_dir):
    """R8-F1: `gh pr close --comment` posts before the close lands.

    The closing command posts its comment, the process is interrupted before
    the close lands, and a human then closes the unchanged source. The
    pre-close comment is already present, but no receipt was ever posted
    (receipts are post-close only). Resume must block without activating the
    replacement -- it must not mistake the pre-close comment for ownership.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.close_error = GitHubUnavailableError("`gh pr close` failed: connection reset")
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    # The close comment predates the close (Fake posts it before raising),
    # the source is still OPEN, and no receipt exists anywhere.
    assert gh.prs[PR].state == "OPEN"
    assert gh.closed_prs  # the comment was posted
    assert render_close_receipt(TXN_ID) not in gh.closed_prs[0][1]
    assert not has_close_receipt([c.body for c in gh.get_pr_comments(PR)], TXN_ID)
    assert _txn(eng).stage is ReplanStage.SUPERSEDE_INTENT

    # A human now closes the unchanged source; resume must not adopt it.
    gh.close_error = ""
    _closed_by_a_human(gh)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "carries no close receipt" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert eng.state.current_pr_url == PR  # replacement inactive
    assert eng.state.superseded_prs == []
    assert gh.reopened_prs == []
    assert len(gh.commented_prs) == 0  # the resume never posts a receipt


def test_r8f1_a_conclusive_close_failure_is_never_adopted(tmp_state_dir):
    """R8-F1: our close failed, so a CLOSED source afterwards is someone else's.

    A human closes the source inside the write window; our close then fails
    conclusively. The old code ignored the failure once the source read back
    CLOSED and adopted on the pre-close comment's receipt. The fix rejects as
    soon as our close is known not to have landed, without posting a receipt.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.close_race = lambda g: _closed_by_a_human(g)
    gh.close_error = "denied: cannot close this PR"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "closing source PR" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert eng.state.current_pr_url == PR
    assert eng.state.superseded_prs == []
    assert gh.commented_prs == []  # no receipt without an observed close
    # The human's close is left exactly as found -- never reopened, never adopted.
    assert gh.prs[PR].state == "CLOSED"
    assert gh.reopened_prs == []


def test_r8f1_an_open_source_already_carrying_a_receipt_is_not_closed_again(
    tmp_state_dir,
):
    """R8-F1: receipt + OPEN at INTENT means a prior close landed then reopened."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_controller(gh)  # CLOSED + receipt, as our own close leaves it
    gh.prs[PR].state = "OPEN"  # ... then someone reopened it before resume
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "already carries the close receipt" in eng.state.block_reason
    assert "reopened" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert gh.closed_prs == []  # never a second close
    assert eng.state.superseded_prs == []


def test_f2_a_crash_after_the_reopen_lands_resumes_into_the_undo(tmp_state_dir):
    """The compensation is a decision, and decisions are persisted before writes.

    Reopen succeeds, the process dies before the rejection is saved, and the
    drift that caused the undo settles back. Without a durable ``COMPENSATING``
    record the resume would find an OPEN source and a valid replacement and
    close it a second time -- laundering a refusal into a supersede.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.close_race = lambda g: g.set_head(SHA_C, PR)

    def die(*_args, **_kwargs):
        raise RuntimeError("process died after the reopen landed")

    eng._reject_replan = die  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        eng.step()
    assert gh.prs[PR].state == "OPEN"  # the undo landed
    crashed = _txn(eng)
    assert crashed.stage is ReplanStage.COMPENSATING

    gh.close_race = None
    gh.set_head(SHA_A, PR)  # the stray push is reverted: the drift is gone
    resumed = make_engine(tmp_state_dir, ["the replan agent must not run"], github=gh)
    resumed.state.phase = Phase.REPLAN_REEXECUTE
    resumed.state.current_issue_url = ISSUE
    resumed.state.current_pr_url = PR
    resumed.state.replan_transaction = crashed.to_dict()
    out = resumed.step()
    assert out.next_phase == "BLOCKED"
    assert "the close was undone" in resumed.state.block_reason
    assert len(gh.closed_prs) == 1  # never a second close
    assert len(gh.reopened_prs) == 1  # the source was already open
    assert resumed.state.superseded_prs == []


def test_f2_a_lost_reopen_response_is_replayed_not_re_superseded(tmp_state_dir):
    """``COMPENSATING`` over a still-CLOSED source finishes the undo."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir,
        gh,
        ReplanStage.COMPENSATING,
        compensating_at="2026-01-01T00:00:00+00:00",
        compensation_reason="source PR advanced inside the close window",
        replacement_pr_url=REPLACEMENT_PR,
        replacement_branch=REPLACEMENT_BRANCH,
        replacement_head_sha=SHA_B,
        attested_findings_considered=4,
        attested_unique_constraints=2,
    )
    _closed_by_controller(gh)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "advanced inside the close window" in eng.state.block_reason
    assert "the close was undone" in eng.state.block_reason
    assert gh.prs[PR].state == "OPEN"
    assert gh.closed_prs == []  # the resume closes nothing
    assert eng.state.superseded_prs == []


def test_f2_a_compensation_that_cannot_be_confirmed_names_the_manual_step(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir,
        gh,
        ReplanStage.COMPENSATING,
        compensating_at="2026-01-01T00:00:00+00:00",
        compensation_reason="replacement PR body no longer carries the marker",
        replacement_pr_url=REPLACEMENT_PR,
        replacement_branch=REPLACEMENT_BRANCH,
        replacement_head_sha=SHA_B,
    )
    _closed_by_controller(gh)
    gh.reopen_leaves_closed = True
    assert eng.step().next_phase == "BLOCKED"
    assert "reopened by hand" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert eng.state.superseded_prs == []


def _drift_replacement(gh, **fields) -> None:
    for name, value in fields.items():
        setattr(gh.prs[REPLACEMENT_PR], name, value)


@pytest.mark.parametrize(
    "drift,needle",
    [
        (lambda gh: _drift_replacement(gh, state="CLOSED"), "CLOSED"),
        (lambda gh: _drift_replacement(gh, head_sha=SHA_C), "advanced from the verified HEAD"),
        (lambda gh: _drift_replacement(gh, body=""), "no longer carries"),
        (lambda gh: _drift_replacement(gh, base_ref="release"), "base"),
    ],
)
def test_f3_a_replacement_that_drifted_before_activation_is_not_installed(
    tmp_state_dir, drift, needle
):
    """``SUPERSEDED`` is persisted before activation; that window is re-checked.

    A crash there leaves a journal that says "activate this PR" while GitHub
    may no longer agree. The source is legitimately closed, so this is not
    compensable -- it blocks with both PRs named.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDED, superseded_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_controller(gh)
    drift(gh)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "can no longer be activated" in eng.state.block_reason
    assert needle in eng.state.block_reason
    assert eng.state.current_pr_url == PR  # nothing was installed
    assert eng.state.superseded_prs == []
    assert _txn(eng).stage is ReplanStage.REJECTED


def test_f3_a_source_reopened_before_activation_blocks(tmp_state_dir):
    """Both checkpoints are re-derived, not just the replacement's."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDED, superseded_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_controller(gh)
    gh.prs[PR].state = "OPEN"  # a human reopened it before the resume
    assert eng.step().next_phase == "BLOCKED"
    assert "expected CLOSED" in eng.state.block_reason
    assert eng.state.superseded_prs == []


def test_f3_a_transient_read_before_activation_stays_resumable(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDED, superseded_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_controller(gh)
    gh.get_pr_failures = 1
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert _txn(eng).stage is ReplanStage.SUPERSEDED  # nothing was decided
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR


@pytest.mark.parametrize(
    "payload",
    ["<invalid>", "<!DOCTYPE html>", '{"transaction_id": "<script>"}', "a > b < c"],
)
def test_f4_a_complete_marker_with_angle_brackets_is_malformed_not_absent(payload):
    """Classification is by the marker's *name*, never by its payload's shape."""
    scan = scan_replan_markers(f"intro\n<!-- {MARKER_NAME}: {payload} -->\noutro")
    assert scan.attestations == [] and len(scan.malformed) == 1, payload


def _txn_for_marker_tests() -> ReplanTransaction:
    return ReplanTransaction(
        transaction_id=TXN_ID,
        stage=ReplanStage.PREPARED,
        source_pr_url=PR,
        issue_url=ISSUE,
        pr_number_watermark=42,
    )


def test_f4_an_angle_bracket_marker_is_a_refusal_not_another_agent_run():
    """As the only marker it must reject: NONE would re-invoke the agent."""
    gh = FakeGitHub()
    gh.add_pr(
        url=OTHER_PR,
        head_sha=SHA_B,
        branch="other",
        linked=[2],
        body=f"<!-- {MARKER_NAME}: <x> -->",
    )
    selection = select_bound_candidate([gh.prs[OTHER_PR]], _txn_for_marker_tests())
    assert selection.disposition is Disposition.REJECTED
    assert "unusable replan marker" in selection.reason


def test_f4_an_angle_bracket_marker_beside_a_valid_one_still_refuses():
    """It must not slip past the "nothing marker-shaped beside it" rule."""
    good = render_marker(
        ReplanAttestation(
            transaction_id=TXN_ID,
            execution_attempt=2,
            findings_considered=4,
            unique_constraints=2,
            tests_passed=True,
        )
    )
    gh = FakeGitHub()
    gh.add_pr(
        url=OTHER_PR,
        head_sha=SHA_B,
        branch="other",
        linked=[2],
        body=f"<!-- {MARKER_NAME}: <x> -->\n{good}",
    )
    selection = select_bound_candidate([gh.prs[OTHER_PR]], _txn_for_marker_tests())
    assert selection.disposition is Disposition.REJECTED
    assert "alongside an unusable one" in selection.reason


def test_f4_an_unterminated_marker_with_brackets_still_cannot_swallow():
    """Widening the payload must not reintroduce the swallowing hazard."""
    good = render_marker(
        ReplanAttestation(
            transaction_id=TXN_ID,
            execution_attempt=2,
            findings_considered=1,
            unique_constraints=1,
            tests_passed=True,
        )
    )
    scan = scan_replan_markers(f"<!-- {MARKER_NAME}: <unterminated\n{good}")
    assert [a.transaction_id for a in scan.attestations] == [TXN_ID]
    assert scan.malformed == []
