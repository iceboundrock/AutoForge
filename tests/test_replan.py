"""REPLAN_REEXECUTE policy, replacement verification, and crash recovery."""

from __future__ import annotations

import re

import pytest

from autoforge.config import ReplanConfig
from autoforge.errors import (
    ControlResultValidationError,
    GitHubError,
    GitHubUnavailableError,
    VerificationError,
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
        "historical_findings_considered": 100,
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
    # This history can reach round 20 under the defaults: the changing count
    # avoids both workflow stagnation rules and its last three counts exceed
    # the soft-trigger finding limit.
    eng.state.review_history = _history([3 + (round_number % 2) for round_number in range(19)])
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


def test_replan_rejects_failed_self_reported_tests(tmp_state_dir):
    gh = FakeGitHub()

    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(_trigger_review_payload())
        gh.add_pr(url=REPLACEMENT_PR, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, linked=[2])
        payload = _replan_payload()
        payload["verification"]["tests_passed"] = False
        return block(payload)

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    eng.step()
    with pytest.raises(VerificationError, match="tests_passed=false"):
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


def test_replan_recovery_with_multiple_candidates_blocks(tmp_state_dir):
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    gh.add_pr(url=REPLACEMENT_PR, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, linked=[2])
    gh.add_pr(
        url="https://github.com/owner/repo/pull/44",
        head_sha="d" * 40,
        branch="autoforge/2-retry-3",
        linked=[2],
    )
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
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
    assert out.next_phase == "BLOCKED"
    assert "multiple replacement PR candidates" in eng.state.block_reason
    assert eng.provider.calls == []


def test_replan_prompt_uses_controller_attempt_and_history(tmp_state_dir):
    eng = make_engine(tmp_state_dir, [], github=FakeGitHub())
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.execution_attempt = 2
    eng.state.replan_progress = {
        "historical_finding_count": 4,
        "previous_pr_url": PR,
        "previous_branch": BRANCH,
        "previous_head_sha": SHA_A,
    }
    prompt = eng.render_prompt_for(Phase.REPLAN_REEXECUTE)
    assert '"execution_attempt": 3' in prompt
    assert '"historical_findings_considered": 4' in prompt


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
    """N6: the controller may only demand the findings it actually rendered."""
    findings = [
        {"id": f"R1-F{n}", "classification": "nit", "required_resolution": f"fix {n}"}
        for n in range(1, 151)
    ]
    record = review_record(1, SHA_A, RESULT_NEEDS_FIX, findings)
    assert record["finding_count"] == 150
    assert len(record["findings"]) == MAX_PERSISTED_FINDINGS_PER_ROUND
    gh = FakeGitHub()
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    data = HistoricalReviewCollector(gh).collect(PR, [record], [])
    assert data.recorded_finding_count == len(data.findings) == MAX_PERSISTED_FINDINGS_PER_ROUND


def _rejecting_agent(gh, mutate):
    """REVIEW at the hard threshold, then a replacement the controller refuses."""

    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(_trigger_review_payload())
        gh.add_pr(url=REPLACEMENT_PR, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, linked=[2])
        payload = _replan_payload()
        mutate(payload)
        return block(payload)

    return agent


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda p: p["verification"].__setitem__("tests_passed", False), "tests_passed=false"),
        (lambda p: p.__setitem__("execution_attempt", 5), "execution_attempt must be 2"),
        (
            lambda p: p.update({"historical_findings_considered": 0,
                                "unique_failure_constraints": 0}),
            "historical finding",
        ),
        (lambda p: p.__setitem__("previous_head_sha", "f" * 40), "previous_head_sha"),
    ],
)
def test_rejected_replacement_is_not_laundered_by_recovery(tmp_state_dir, mutate, needle):
    """N1: stepping past an _apply_replan rejection must not activate the replacement."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _rejecting_agent(gh, mutate))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    with pytest.raises(VerificationError, match=needle):
        eng.step()
    assert eng.state.replan_progress["stage"] == "replacement_rejected"

    # The rejection survives a resume: recovery blocks instead of accepting the
    # replacement whose objective GitHub facts all still look valid.
    calls_before = len(eng.provider.calls)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "rejected by controller verification" in eng.state.block_reason
    assert needle in eng.state.block_reason
    assert len(eng.provider.calls) == calls_before  # the agent is not re-invoked
    assert gh.prs[PR].state == "OPEN"  # the previous PR was never closed
    assert eng.state.current_pr_url == PR
    assert eng.state.superseded_prs == []


def test_replan_recovery_propagates_transient_github_failure(tmp_state_dir):
    """N4: a flaky `gh pr list` must not permanently block a healthy replan."""
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
    real = gh.find_open_prs_for_issue
    calls = {"n": 0}

    def flaky(issue):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GitHubUnavailableError("gh: connection reset")
        return real(issue)

    gh.find_open_prs_for_issue = flaky
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert eng.state.phase == Phase.REPLAN_REEXECUTE and not eng.state.block_reason
    # Resuming after the blip completes the replan normally.
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR and gh.prs[PR].state == "CLOSED"


def test_conclusive_github_failure_still_blocks_replan_recovery(tmp_state_dir):
    """The transient carve-out above must not weaken the conclusive path."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    eng.state.replan_progress = {
        "stage": "prepared",
        "previous_pr_url": PR,
        "previous_branch": BRANCH,
        "previous_head_sha": SHA_A,
        "previous_review_round": 20,
        "default_branch": "main",
        "escalation": {"trigger": "hard_review_round_threshold"},
    }

    def denied(issue):
        raise GitHubError("HTTP 403: Resource not accessible by integration")

    gh.find_open_prs_for_issue = denied
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "cannot list replacement PR candidates" in eng.state.block_reason
    assert gh.prs[PR].state == "OPEN"
