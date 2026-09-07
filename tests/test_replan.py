"""REPLAN_REEXECUTE policy, replacement verification, and crash recovery."""

from __future__ import annotations

import pytest

from autoforge.config import ReplanConfig
from autoforge.errors import ControlResultValidationError, VerificationError
from autoforge.loop_guard import RESULT_NEEDS_FIX, review_record
from autoforge.replan import evaluate_replan_policy
from autoforge.result_parser import parse_control_result
from autoforge.transitions import Phase
from tests.conftest import (
    BRANCH,
    ISSUE,
    PR,
    SHA_A,
    SHA_B,
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


def _replan_payload() -> dict:
    return {
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
        "historical_findings_considered": 20,
        "unique_failure_constraints": 2,
        "previous_pr_disposition": "superseded",
        "fresh_review_round": 1,
        "verification": {"tests_run": ["pytest"], "tests_passed": True},
    }


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


def _park_at_hard_threshold(tmp_state_dir, gh, agent):
    eng = make_engine(tmp_state_dir, agent, github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.review_round = 19
    eng.state.review_history = _history([1] * 19)
    return eng


def test_hard_threshold_replaces_pr_and_starts_fresh_review(tmp_state_dir):
    gh = FakeGitHub()

    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(
                {
                    "phase": "REVIEW",
                    "status": "success",
                    "round": 20,
                    "reviewed_head_sha": SHA_A,
                    "review_comment_url": comment_url(PR, 120),
                    "needs_fix_round": True,
                    "findings": [
                        {
                            "id": "R20-F1",
                            "classification": "nit",
                            "required_resolution": "resolve final issue",
                        }
                    ],
                }
            )
        if req.phase == "REPLAN_REEXECUTE":
            gh.add_pr(
                url=REPLACEMENT_PR,
                head_sha=SHA_B,
                branch=REPLACEMENT_BRANCH,
                linked=[2],
            )
            assert "latest default branch" in req.prompt
            assert PR in req.prompt and "R20-F1" in req.prompt
            return block(_replan_payload())
        raise AssertionError(req.phase)

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert gh.prs[PR].state == "CLOSED"
    assert gh.closed_prs and REPLACEMENT_PR in gh.closed_prs[0][1]
    state = eng.state
    assert state.current_pr_url == REPLACEMENT_PR
    assert state.current_branch == REPLACEMENT_BRANCH
    assert state.review_round == 0  # next REVIEW is round 1 under persisted semantics
    assert state.execution_attempt == 2 and state.escalation_count == 1
    assert state.superseded_prs[0]["pr_url"] == PR
    assert eng.provider.calls[-1].profile.name == "replan_reexecute"
    assert eng.provider.calls[-1].profile.model == "openai/gpt-5.6-terra"


@pytest.mark.parametrize(
    "field,value,needle",
    [("base_ref", "release", "base"), ("state", "CLOSED", "expected OPEN")],
)
def test_replacement_verification_rejects_wrong_base_or_closed(tmp_state_dir, field, value, needle):
    gh = FakeGitHub()

    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(
                {
                    "phase": "REVIEW", "status": "success", "round": 20,
                    "reviewed_head_sha": SHA_A,
                    "review_comment_url": comment_url(PR, 120),
                    "needs_fix_round": True,
                    "findings": [
                        {"id": "R20-F1", "classification": "nit", "required_resolution": "x"}
                    ],
                }
            )
        gh.add_pr(url=REPLACEMENT_PR, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, linked=[2])
        setattr(gh.prs[REPLACEMENT_PR], field, value)
        return block(_replan_payload())

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    eng.step()
    with pytest.raises(VerificationError, match=needle):
        eng.step()
    assert gh.prs[PR].state == "OPEN"


@pytest.mark.parametrize(
    "replacement_url,head_claim,expected_error",
    [
        (REPLACEMENT_PR, "c" * 40, "HEAD mismatch"),
        ("https://github.com/other/repo/pull/43", SHA_B, "not in owner/repo"),
    ],
)
def test_replacement_verification_rejects_wrong_head_or_repository(
    tmp_state_dir, replacement_url, head_claim, expected_error
):
    gh = FakeGitHub()

    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(_trigger_review_payload())
        gh.add_pr(url=replacement_url, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, linked=[2])
        payload = _replan_payload()
        payload["replacement_pr_url"] = replacement_url
        payload["replacement_head_sha"] = head_claim
        return block(payload)

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    eng.step()
    with pytest.raises(VerificationError, match=expected_error):
        eng.step()
    assert gh.prs[PR].state == "OPEN"


def test_replan_recovery_does_not_invoke_agent_twice(tmp_state_dir):
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    gh.add_pr(url=REPLACEMENT_PR, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.replan_progress = {
        "stage": "prepared",
        "previous_pr_url": PR,
        "previous_branch": BRANCH,
        "previous_head_sha": SHA_A,
        "previous_review_round": 20,
        "default_branch": "main",
        "escalation": {"trigger": "hard_review_round_threshold"},
    }
    out = eng.step()
    assert out.next_phase == "REVIEW" and "recovered" in out.message
    assert eng.provider.calls == []
    assert eng.state.current_pr_url == REPLACEMENT_PR and gh.prs[PR].state == "CLOSED"


def test_replan_recovery_finishes_after_old_pr_was_already_closed(tmp_state_dir):
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, state="CLOSED", linked=[2])
    gh.add_pr(url=REPLACEMENT_PR, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    eng.state.replan_progress = {
        "stage": "replacement_verified",
        "previous_pr_url": PR,
        "previous_branch": BRANCH,
        "previous_head_sha": SHA_A,
        "previous_review_round": 20,
        "replacement_pr_url": REPLACEMENT_PR,
        "replacement_branch": REPLACEMENT_BRANCH,
        "replacement_head_sha": SHA_B,
        "default_branch": "main",
        "escalation": {"trigger": "hard_review_round_threshold"},
    }
    assert eng.step().next_phase == "REVIEW"
    assert eng.provider.calls == [] and eng.state.current_pr_url == REPLACEMENT_PR
