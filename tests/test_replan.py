"""REPLAN_REEXECUTE policy, replacement verification, and crash recovery."""

from __future__ import annotations

import json
import re
import time

import pytest

from autoforge.claims import render_implementation_marker
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
    stagnation_reason,
    truncated_evidence_rounds,
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
    find_non_open_claimant,
    has_close_receipt,
    render_close_receipt,
    render_marker,
    scan_replan_markers,
    select_bound_candidate,
)
from autoforge.result_parser import (
    MAX_FINDING_RESOLUTION_CHARS,
    MAX_FINDINGS_PER_REVIEW,
    parse_control_result,
)
from autoforge.state import load_state
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
# The replacement is the issue's implementation PR: it carries the marker
# ANALYZE_EXECUTE identifies the issue's PR by, beside the transaction marker.
IMPLEMENTATION_MARKER = render_implementation_marker(ISSUE)


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
# A well-formed attestation belonging to a *different* replan: valid in every
# way except that it is not this transaction's provenance.
OTHER_TXN_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


def _ours_marker(findings: int = 4, unique: int = 2, txn_id: str = TXN_ID) -> str:
    """The marker a compliant replacement publishes for this transaction."""
    return render_marker(
        ReplanAttestation(
            transaction_id=txn_id,
            execution_attempt=2,
            findings_considered=findings,
            unique_constraints=unique,
            tests_passed=True,
        )
    )


def _foreign_marker() -> str:
    """A perfectly valid marker that simply belongs to another transaction."""
    return _ours_marker(txn_id=OTHER_TXN_ID)


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
    implementation = _from_prompt(prompt, r"(<!-- ai-implementation: \{[^\n]*\} -->)")
    return f"## Fresh Reimplementation\n\nReplaces {PR}.\n\n{implementation}\n{marker}"


def _replacement(marker: str) -> str:
    """A replacement PR body publishing ``marker`` beside the implementation marker."""
    return f"{IMPLEMENTATION_MARKER}\n{marker}"


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
                body if body is not None else _replacement_body(req.prompt, **(marker_over or {}))
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
    txn = _seed_txn(stage, **over)
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_issue_url = ISSUE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.replan_transaction = txn.to_dict()
    return txn


def _seed_txn(stage: ReplanStage, **over) -> ReplanTransaction:
    """A well-formed transaction at ``stage``, as a crash at that point would leave it."""
    txn = ReplanTransaction(
        # The id is created together with PREPARED; PENDING carries none.
        transaction_id="" if stage is ReplanStage.PENDING else TXN_ID,
        stage=stage,
        issue_url=ISSUE,
        decision_pr_url=PR,
        decision_head_sha=SHA_A,
        decision_branch=BRANCH,
        source_pr_url=PR,
        source_branch=BRANCH,
        source_head_sha=SHA_A,
        source_review_round=20,
        base_branch="main",
        evidence_finding_count=3,
        rendered_findings="- R20-F1 (round 20, blocked): fix it",
        rendered_observations="(none)",
        rendered_verification_failures="(none)",
        preexisting_pr_urls=[PR],
        pr_number_watermark=42,
        expected_execution_attempt=2,
        escalation={"trigger": "hard_review_round_threshold"},
    )
    after_verified = (
        ReplanStage.VERIFIED,
        ReplanStage.SUPERSEDE_INTENT,
        ReplanStage.COMPENSATING,
        ReplanStage.SUPERSEDED,
    )
    if stage in after_verified:
        txn.replacement_pr_url = REPLACEMENT_PR
        txn.replacement_branch = REPLACEMENT_BRANCH
        txn.replacement_head_sha = SHA_B
        txn.attested_findings_considered = 4
        txn.attested_unique_constraints = 2
    if stage in after_verified[1:]:
        txn.close_intent_at = "2026-01-01T00:00:00+00:00"
    if stage is ReplanStage.COMPENSATING:
        txn.compensating_at = "2026-01-01T00:00:01+00:00"
        txn.compensation_reason = "the replan checkpoint no longer held at the close"
    if stage is ReplanStage.SUPERSEDED:
        txn.superseded_at = "2026-01-01T00:00:01+00:00"
    for key, value in over.items():
        setattr(txn, key, value)
    return txn


def _seeded_engine(tmp_state_dir, gh, stage, *, marker=True, **over):
    eng = make_engine(tmp_state_dir, ["the replan agent must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    body = _replacement(_ours_marker()) if marker else "no marker here"
    gh.add_pr(url=REPLACEMENT_PR, head_sha=SHA_B, branch=REPLACEMENT_BRANCH, linked=[2], body=body)
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
        # `Owner/REPO/pull/42` *is* PR 42: GitHub identity, not URL spelling.
        (
            lambda p: p.update(replacement_pr_url="https://github.com/Owner/REPO/pull/42"),
            "replacement_pr_url must differ",
        ),
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


REPLAN_REQUIRED_FIELDS = [
    "issue_url",
    "previous_pr_url",
    "replacement_pr_url",
    "previous_branch",
    "replacement_branch",
    "previous_head_sha",
    "replacement_head_sha",
    "execution_attempt",
    "historical_findings_considered",
    "unique_failure_constraints",
    "fresh_review_round",
    "previous_pr_disposition",
    "verification",
]


@pytest.mark.parametrize("field", REPLAN_REQUIRED_FIELDS)
def test_replan_result_schema_requires_every_field(field):
    """Every REPLAN_REEXECUTE field is required; none defaults into a valid result."""
    payload = _replan_payload()
    del payload[field]
    with pytest.raises(ControlResultValidationError, match=f"missing required field {field!r}"):
        parse_control_result(block(payload), Phase.REPLAN_REEXECUTE)
    payload = _replan_payload()
    payload[field] = None
    with pytest.raises(ControlResultValidationError, match=field):
        parse_control_result(block(payload), Phase.REPLAN_REEXECUTE)


@pytest.mark.parametrize(
    "verification,needle",
    [
        ({"tests_passed": True}, "verification.tests_run must be a list"),
        ({"tests_run": "pytest", "tests_passed": True}, "verification.tests_run must be a list"),
        ({"tests_run": ["pytest"]}, "verification.tests_passed must be a boolean"),
        ({"tests_run": ["pytest"], "tests_passed": "yes"}, "tests_passed must be a boolean"),
        ("passed", "verification must be an object"),
    ],
)
def test_replan_result_schema_requires_a_complete_verification_object(verification, needle):
    payload = _replan_payload(verification=verification)
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
    assert gh.merges == []  # a replan closes; it never merges anything
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


def test_review_beyond_the_parser_bound_is_refused_before_it_can_be_persisted(tmp_state_dir):
    """#34: an oversized round is rejected at the parser, so it never marks history.

    The parser bound is at or below the persisted bound, which is why the
    truncation marker below can only come from state the controller did not
    itself accept.
    """
    finding_ids = [f"R20-F{n}" for n in range(1, MAX_FINDINGS_PER_REVIEW + 2)]
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
    eng.config.execution.max_correction_attempts = 0
    with pytest.raises(ControlResultValidationError, match="accepts at most"):
        eng.step()
    assert eng.state.phase == Phase.REVIEW and eng.state.review_round == 19
    assert eng.state.open_findings == []
    assert len(eng.state.review_history) == 19
    assert truncated_evidence_rounds(eng.state.review_history) == []
    _assert_source_untouched(eng, gh)


def test_redaction_growth_never_marks_an_accepted_round_truncated(tmp_state_dir):
    """#33: the one way an accepted review could still exceed the persisted bound.

    A resolution exactly at the parser bound that quotes a secret grows under
    redaction (a short token becomes the 14-character marker) before it is
    persisted. The persisted bound absorbs that growth, so the round is
    retained complete, the replan is not refused, every finding is rendered
    into the replan prompt and the acknowledgement count covers it.
    """
    gh = FakeGitHub()
    # Long enough that the marker cannot fit inside the parser bound.
    quoted = "remove the hard-coded header Authorization: Bearer s3cr3t "
    resolution = (quoted * (MAX_FINDING_RESOLUTION_CHARS // len(quoted) + 1))[
        :MAX_FINDING_RESOLUTION_CHARS
    ]
    assert len(resolution) == MAX_FINDING_RESOLUTION_CHARS
    replan = _replan_agent(gh)

    def agent(req):
        if req.phase != "REVIEW":
            return replan(req)
        gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
        payload = _trigger_review_payload()
        payload["findings"][0]["required_resolution"] = resolution
        return block(payload)

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    record = eng.state.review_history[-1]
    persisted = record["findings"][0]["required_resolution"]
    assert len(persisted) > MAX_FINDING_RESOLUTION_CHARS  # it did grow ...
    assert "s3cr3t" not in persisted and "***REDACTED***" in persisted
    assert "evidence_truncated" not in record  # ... and was retained whole
    assert truncated_evidence_rounds(eng.state.review_history) == []
    # Every finding of every round, the grown one included, is what the
    # replacement must acknowledge.
    expected = sum(r["finding_count"] for r in eng.state.review_history)
    assert expected == sum(len(r.get("findings", [])) for r in eng.state.review_history)

    assert eng.step().next_phase == "REVIEW"
    prompt = eng.provider.calls[-1].prompt
    assert "s3cr3t" not in prompt and persisted in prompt
    assert f'"historical_findings_considered": {expected},' in prompt
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert eng.state.superseded_prs[0]["pr_url"] == PR


def test_review_beyond_the_persisted_finding_bound_blocks_instead_of_replanning(tmp_state_dir):
    """I1 end-to-end: a truncated round cannot supersede the PR that carries it.

    Such a round can no longer be produced through the parser (above); it is
    state persisted before the parser bounds existed, or edited outside the
    controller. The persisted-evidence guard still refuses to replan on it.
    """
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    legacy = [
        {"id": f"R8-F{n}", "classification": "blocked", "required_resolution": f"resolve {n}"}
        for n in range(1, MAX_PERSISTED_FINDINGS_PER_ROUND + 2)
    ]
    eng.state.review_history[7] = review_record(8, SHA_A, RESULT_NEEDS_FIX, legacy)
    assert eng.state.review_history[7]["evidence_truncated"] is True
    out = eng.step()

    assert out.next_phase == "BLOCKED"
    assert "replan_evidence_truncated" in eng.state.block_reason
    assert "round(s) 8" in eng.state.block_reason
    _assert_source_untouched(eng, gh)
    assert len(eng.state.open_findings) == 1

    # And a `resume` straight into REPLAN_REEXECUTE cannot launder it: the
    # checkpoint that would authorise a replacement is refused too.
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.replan_transaction = ReplanTransaction(
        stage=ReplanStage.PENDING,
        issue_url=ISSUE,
        decision_pr_url=PR,
        decision_head_sha=SHA_A,
        decision_branch=BRANCH,
        escalation={"trigger": "hard_review_round_threshold"},
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
    gh.add_pr(url=OTHER_PR, head_sha=SHA_C, branch="autoforge/2-older", linked=[2], body=marker)
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
        body=_ours_marker(),
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
        body=_ours_marker(),
        state="CLOSED",
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert REPLACEMENT_PR in eng.state.block_reason
    assert "CLOSED" in eng.state.block_reason
    assert (
        "a first replacement attempt already exists and must be decided by a human"
        in eng.state.block_reason
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
        body=_ours_marker(),
        state="CLOSED",
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "already existed" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_r9f2_a_marker_on_the_source_pr_rejects_instead_of_reimplementing(tmp_state_dir):
    """R9-F2: the PR being superseded is excluded from adoption, not from reading.

    The prompt says putting the marker on the superseded PR rejects the replan.
    Skipping the source's body before scanning it turns that into "no candidate
    exists" -- and that observation is what decides whether the agent runs
    again, so marker-bearing work would exist while a second implementation
    attempt started on top of it.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    gh.prs[PR].body = "The original implementation.\n\n" + _ours_marker()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert PR in eng.state.block_reason
    assert "belongs on the replacement" in eng.state.block_reason
    assert eng.provider.calls == []  # no second implementation attempt
    _assert_source_untouched(eng, gh)


def test_r9f2_a_closed_source_carrying_the_marker_is_found_by_the_all_states_scan(
    tmp_state_dir,
):
    """The source rule holds on the recovery listing too, not just the open one."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    gh.prs[PR].body = _ours_marker()
    gh.prs[PR].state = "CLOSED"  # a human closed it while the controller was down
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "belongs on the replacement" in eng.state.block_reason
    assert eng.provider.calls == []
    assert eng.state.superseded_prs == []


def test_r9f2_an_older_replans_marker_on_the_source_does_not_block_a_healthy_replan(
    tmp_state_dir,
):
    """A source PR may itself be an earlier replan's replacement.

    That older, valid marker is legitimate history and names a different
    transaction, so it must neither be adopted nor refused -- otherwise every
    second replan on a chain would block.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    gh.prs[PR].body = "Replacement for an earlier replan.\n\n" + _foreign_marker()
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert gh.prs[PR].state == "CLOSED"


def test_r9f3_a_closed_malformed_claimant_is_rejected_not_reimplemented(tmp_state_dir):
    """R9-F3: the all-states scan must classify unusable markers, like the open one.

    A post-watermark PR carrying a complete but unusable marker, then closed
    before recovery, is invisible to the open listing. Reading only valid
    attestations there reports absence, and absence invokes the agent again.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    gh.add_pr(
        url=REPLACEMENT_PR,
        head_sha=SHA_B,
        branch=REPLACEMENT_BRANCH,
        linked=[2],
        body=UNUSABLE_MARKERS[0],
        state="CLOSED",
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert REPLACEMENT_PR in eng.state.block_reason
    assert "unusable replan marker" in eng.state.block_reason
    assert "CLOSED" in eng.state.block_reason
    assert eng.provider.calls == []  # no second implementation attempt
    _assert_source_untouched(eng, gh)


def test_r9f3_an_unusable_marker_on_a_preexisting_closed_pr_is_still_ignored(
    tmp_state_dir,
):
    """Symmetry with the open path: a PR predating the id is evidence of nothing.

    It cannot be adopted and it must not block either, so the agent is invoked
    exactly once -- the marker garbage belongs to somebody else's PR.
    """
    gh = FakeGitHub()
    gh.add_pr(
        url=EARLIER_PR,
        head_sha=SHA_C,
        branch="chore/unrelated-cleanup",
        linked=[2],
        body=UNUSABLE_MARKERS[0],
        state="CLOSED",
    )
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR


def test_an_unusable_marker_on_a_preexisting_open_pr_is_ignored_by_the_open_listing(
    tmp_state_dir,
):
    """The OPEN counterpart: garbage on a PR that predates the id neither adopts nor blocks."""
    gh = FakeGitHub()
    gh.add_pr(
        url=EARLIER_PR,
        head_sha=SHA_C,
        branch="chore/unrelated-cleanup",
        linked=[2],
        body=UNUSABLE_MARKERS[0],
        state="OPEN",
    )
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert gh.prs[EARLIER_PR].state == "OPEN"  # left exactly as found
    replan_calls = [c for c in eng.provider.calls if c.phase == "REPLAN_REEXECUTE"]
    assert len(replan_calls) == 1


def test_r9f4_a_valid_marker_for_another_transaction_beside_ours_is_refused(
    tmp_state_dir,
):
    """R9-F4: "exactly one marker" counts every attestation, not just ours.

    Counting only our own leaves a body that publishes two provenances at once
    passing every objective check -- and provenance naming two transactions
    proves neither.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    gh.prs[REPLACEMENT_PR].body += "\n" + _foreign_marker()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "valid marker(s) for other transactions" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_r9f4_a_foreign_marker_on_an_unrelated_pr_does_not_block_a_healthy_replan(
    tmp_state_dir,
):
    """The rule is about *our* candidate's body, not about the repository."""
    gh = FakeGitHub()
    gh.add_pr(url=OTHER_PR, head_sha=SHA_C, branch="other", linked=[2], body=_foreign_marker())
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR


def test_n9_a_pre_close_refusal_does_not_assert_that_the_source_is_open(tmp_state_dir):
    """Issue #37 N9: before the close the controller vouches for its own writes only.

    Whether the source is *open* is a GitHub fact a human can change at any
    time; the block text says what the transaction did (nothing destructive)
    and never claims what GitHub currently shows.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    gh.add_pr(
        url=REPLACEMENT_PR,
        head_sha=SHA_B,
        branch=REPLACEMENT_BRANCH,
        linked=[2],
        body=UNUSABLE_MARKERS[0],
    )
    assert eng.step().next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert f"This transaction did not close PR {PR}" in reason
    assert "nothing was closed or merged by the controller" in reason
    assert "stays open" not in reason
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


@pytest.mark.parametrize("unusable", UNUSABLE_MARKERS, ids=range(len(UNUSABLE_MARKERS)))
def test_an_unusable_marker_on_the_pre_existing_source_is_evidence_of_nothing(
    tmp_state_dir, unusable
):
    """The source predates the transaction id, so it gets the watermark rule.

    A pre-transaction PR is evidence of nothing about the new transaction:
    its unusable marker neither adopts nor blocks, exactly as on every other
    pre-existing PR. The healthy replan proceeds, and the source is closed
    only through the checkpointed supersede.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    gh.prs[PR].body = "Some description.\n\n" + unusable
    assert gh.prs[PR].number <= eng.state.replan_transaction["pr_number_watermark"]
    out = eng.step()
    assert out.next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert gh.prs[PR].state == "CLOSED"


def test_an_unusable_marker_on_the_pre_existing_source_is_ignored_by_the_all_states_scan(
    tmp_state_dir,
):
    """The recovery listing classifies the source by age exactly as the open one."""
    gh = FakeGitHub()
    eng, txn = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    gh.prs[PR].body = UNUSABLE_MARKERS[0]
    gh.prs[PR].state = "CLOSED"  # a human closed it while the controller was down
    listing = find_non_open_claimant(list(gh.prs.values()), txn)
    assert listing.disposition is Disposition.NONE, listing.reason
    # And through the engine: no claimant found, so the phase falls through
    # to the agent (the stub returns nothing usable) instead of blocking on
    # the source's broken marker.
    calls_before = len(eng.provider.calls)
    with pytest.raises(ControlResultValidationError):
        eng.step()
    assert len(eng.provider.calls) > calls_before
    assert eng.state.superseded_prs == []


def test_an_unusable_marker_on_a_source_that_postdates_the_transaction_still_blocks(
    tmp_state_dir,
):
    """The exemption is the classification, not the source role.

    A source whose number is above the watermark and outside the snapshot
    cannot happen in a consistent transaction, but if the persisted
    checkpoint says so, its botched attestation is evidence of a first
    attempt and is refused like any other post-watermark PR's. (The snapshot
    stays non-empty: an empty one is refused on load as an incomplete
    checkpoint before any marker is looked at.)
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    txn = dict(eng.state.replan_transaction)
    txn["pr_number_watermark"] = gh.prs[PR].number - 1
    txn["preexisting_pr_urls"] = ["https://github.com/owner/repo/pull/41"]
    eng.state.replan_transaction = txn
    eng.save()
    gh.prs[PR].body = UNUSABLE_MARKERS[0]
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "unusable replan marker" in eng.state.block_reason
    assert PR in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


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


# -- the replacement is the issue's implementation PR ------------------------------
# ANALYZE_EXECUTE identifies the issue's PR by the `ai-implementation` marker
# alone, so a replacement without it would be activated here and be
# invisible to the next entry after a lost state file, which launches a
# second implementation. The target predicate holds the replacement to the
# same marker at binding, at the close and at activation.


def _replacement_without(tmp_state_dir, gh, implementation: str | None):
    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(_trigger_review_payload())
        lines = [_marker_for_prompt(req.prompt)]
        if implementation is not None:
            lines.insert(0, implementation)
        gh.add_pr(
            url=REPLACEMENT_PR,
            head_sha=SHA_B,
            branch=REPLACEMENT_BRANCH,
            linked=[2],
            body="\n".join(lines),
        )
        return block(_replan_payload())

    return _park_at_hard_threshold(tmp_state_dir, gh, agent)


def test_a_replacement_without_the_issues_implementation_marker_is_refused(tmp_state_dir):
    gh = FakeGitHub()
    eng = _replacement_without(tmp_state_dir, gh, None)
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "BLOCKED"
    assert "does not carry the ai-implementation marker for issue #2" in eng.state.block_reason
    _assert_source_untouched(eng, gh)
    assert _txn(eng).stage is ReplanStage.REJECTED


def test_a_replacement_marked_as_another_issues_implementation_is_refused(tmp_state_dir):
    gh = FakeGitHub()
    other = render_implementation_marker("https://github.com/owner/repo/issues/3")
    eng = _replacement_without(tmp_state_dir, gh, other)
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "BLOCKED"
    assert "not of issue #2" in eng.state.block_reason
    _assert_source_untouched(eng, gh)


def test_a_replacement_with_an_unreadable_implementation_marker_is_refused(tmp_state_dir):
    gh = FakeGitHub()
    eng = _replacement_without(tmp_state_dir, gh, "<!-- ai-implementation: {broken -->")
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "BLOCKED"
    assert "unreadable ai-implementation marker" in eng.state.block_reason
    _assert_source_untouched(eng, gh)


def test_an_activated_replacement_is_what_a_fresh_analyze_entry_finds(tmp_state_dir):
    """The read-back never accepts what the next entry could not find again:
    with the state file gone, ANALYZE_EXECUTE for the same issue adopts the
    activated replacement (the superseded PR is closed) instead of launching
    a second implementation."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR and gh.prs[PR].state == "CLOSED"
    fresh = make_engine(tmp_state_dir / "fresh", ["never"], github=gh)
    fresh.state.phase = Phase.ANALYZE_EXECUTE
    out = fresh.step()
    assert out.next_phase == "REVIEW" and fresh.provider.calls == []
    assert fresh.state.current_pr_url == REPLACEMENT_PR
    assert fresh.state.current_head_sha == SHA_B


@pytest.mark.parametrize(
    "payload_over",
    [
        {"previous_pr_url": "https://github.com/Owner/REPO/pull/42"},
        {"replacement_pr_url": "https://github.com/Owner/REPO/pull/43"},
    ],
    ids=["previous", "replacement"],
)
def test_n1_agent_claimed_pr_urls_are_compared_by_identity(tmp_state_dir, payload_over):
    """Issue #37 N1: `Owner/REPO` is this repository as GitHub sees it.

    The agent's `previous_pr_url` is checked against the checkpoint and its
    `replacement_pr_url` against the marker-bound PR; both are identity
    comparisons (repository case-insensitively, then the number), never
    string equality of canonical forms that keep the agent's spelling.
    """
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh, payload_over=payload_over))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR  # the run's spelling, not the agent's
    assert eng.state.superseded_prs[0]["pr_url"] == PR


def test_n1_an_agent_claiming_another_pr_of_the_same_number_elsewhere_is_refused(tmp_state_dir):
    """Identity is repository *and* number: a different repository is a mismatch."""
    gh = FakeGitHub()
    over = {"previous_pr_url": "https://github.com/other/repo/pull/42"}
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh, payload_over=over))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "BLOCKED"
    assert "previous_pr_url" in eng.state.block_reason
    assert "does not match the checkpoint" in eng.state.block_reason
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
def test_target_drift_after_the_checkpoint_blocks_instead_of_closing(tmp_state_dir, mutate, needle):
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
    return _ours_marker(findings, unique)


@pytest.mark.parametrize(
    "body,needle",
    [
        (_replacement("the marker is gone"), "no longer carries the marker"),
        (
            _replacement(_restated_marker(4, 2) + "\n" + UNUSABLE_MARKERS[1]),
            "alongside an unusable one",
        ),
        (
            _replacement(_restated_marker(4, 2) + "\n" + _foreign_marker()),
            "valid marker(s) for other transactions",
        ),
        (_replacement(_restated_marker(4, 2) * 2), "carries 2 markers"),
        (_replacement(_restated_marker(9, 2)), "now attests findings_considered=9"),
        (_replacement(_restated_marker(4, 1)), "unique_constraints=1"),
    ],
)
def test_the_marker_is_revalidated_on_the_last_read_before_the_close(tmp_state_dir, body, needle):
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
            lambda gh: setattr(gh.prs[REPLACEMENT_PR], "body", _replacement("marker removed")),
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


@pytest.mark.parametrize(
    "race,needle",
    [
        (lambda gh: gh.set_head(SHA_C, PR), "advanced from the checkpointed HEAD"),
        (
            lambda gh: setattr(gh.prs[PR], "head_ref", "somebody/else"),
            "moved from the checkpointed branch",
        ),
    ],
)
def test_r9f1_source_drift_after_the_receipt_is_undone_not_merely_blocked(
    tmp_state_dir, race, needle
):
    """R9-F1: the confirmation must compare reads it took *itself*.

    The writing step reads the source back to prove the close landed, then
    publishes the receipt -- a whole GitHub round trip. Comparing the snapshot
    from *before* that write would let a source which moved inside the gap
    reach ``SUPERSEDED``; the later activation check would notice and block,
    but blocking leaves a controller close standing over a checkpoint it no
    longer satisfies. Source drift before ``SUPERSEDED`` belongs on the durable
    compensation path.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.comment_race = race  # lands while the receipt is being published
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert needle in eng.state.block_reason
    assert "the close was undone" in eng.state.block_reason
    assert [url for url, _ in gh.reopened_prs] == [PR]
    assert gh.prs[PR].state == "OPEN"
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert eng.state.current_pr_url == PR
    assert eng.state.superseded_prs == [] and eng.state.escalation_count == 0


def _break_the_confirmations_source_read(gh) -> None:
    """Make the *confirmation's* own re-read of the source fail conclusively.

    The ownership check reads the source's comments first, and the fake reads
    the PR to serve them, so the first read after the receipt has to succeed:
    the read under test is the next one -- the one
    :meth:`_confirm_supersede` takes to compare the checkpoint.
    """
    original = gh.get_pr
    remaining = [1]

    def failing(url: str):
        if url == PR and not remaining:
            raise GitHubError("HTTP 451: `gh pr view` refused")
        if url == PR:
            remaining.pop()
        return original(url)

    gh.get_pr = failing


def test_r9f1_an_unreadable_source_after_the_close_is_undone_not_adopted(tmp_state_dir):
    """A checkpoint that cannot be confirmed is not a checkpoint that held.

    Symmetric with the replacement side: an unproven close is undone, exactly
    as one proven wrong is. A *transient* failure would stay resumable instead.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.comment_race = _break_the_confirmations_source_read
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "could not be re-read after the close" in eng.state.block_reason
    assert "reopened by hand" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert eng.state.superseded_prs == []


def _break_the_confirmations_target_read(gh, error: Exception, *, once: bool = False) -> None:
    """Make the confirmation's re-read of the *replacement* fail with ``error``.

    Installed from ``comment_race`` so that it applies only after the close
    receipt landed -- the pre-close verification read of the replacement
    must succeed, or the close would never be attempted.
    """
    original = gh.get_pr

    def failing(url: str):
        if url == REPLACEMENT_PR:
            if once:
                gh.get_pr = original
            raise error
        return original(url)

    gh.get_pr = failing


def test_t5_an_unreadable_replacement_after_the_close_is_undone_not_adopted(tmp_state_dir):
    """Issue #37 T5: the replacement side of the unconfirmable-checkpoint rule.

    Symmetric with the source side: a conclusive failure to re-read the
    replacement after the close means the close cannot be shown to have
    been correct, so it is undone -- the source is reopened and the run
    blocks -- rather than kept on the strength of the pre-close read.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.comment_race = lambda fake: _break_the_confirmations_target_read(
        fake, GitHubError("HTTP 451: `gh pr view` refused")
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert f"replacement PR {REPLACEMENT_PR} could not be re-read after the close" in reason
    assert "the close was undone" in reason and "is open again" in reason
    assert len(gh.closed_prs) == 1 and len(gh.reopened_prs) == 1
    assert gh.prs[PR].state == "OPEN"
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert eng.state.current_pr_url == PR and eng.state.superseded_prs == []


def test_t5_a_transient_replacement_read_after_the_close_stays_resumable(tmp_state_dir):
    """A transient failure is not drift: nothing is decided, and the resume confirms."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.comment_race = lambda fake: _break_the_confirmations_target_read(
        fake, GitHubUnavailableError("HTTP 502: bad gateway"), once=True
    )
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert _txn(eng).stage is ReplanStage.SUPERSEDE_INTENT  # nothing was decided
    assert gh.reopened_prs == [] and gh.prs[PR].state == "CLOSED"
    # The resume finds its own receipt on the closed source, confirms both
    # checkpoints, and activates -- without a second close.
    assert eng.step().next_phase == "REVIEW"
    assert len(gh.closed_prs) == 1 and gh.reopened_prs == []
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert eng.state.superseded_prs[0]["pr_url"] == PR


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
def test_agent_claims_are_cross_checked_against_the_checkpoint(tmp_state_dir, payload_over, needle):
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


def _restart(eng, gh, script=None):
    """A new controller process over the state ``eng`` persisted: save, construct, load."""
    eng.save()
    eng2 = make_engine(eng.paths.state_dir, script or ["the replan agent must not run"], github=gh)
    eng2.load()
    assert eng2.state.phase is Phase.REPLAN_REEXECUTE
    return eng2


def test_t1_prepared_survives_a_restart_and_invokes_the_agent_once(tmp_state_dir):
    """Issue #37 T1: PREPARED, persisted, loaded by a fresh engine -> one invocation.

    The in-memory windows prove the reducer; this proves the journal round
    trip: the transaction id the fresh process hands the agent is the one the
    crashed process persisted, so the marker the agent publishes binds.
    """
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["crashed here"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    _seed(eng, ReplanStage.PREPARED)
    eng2 = _restart(eng, gh, _replan_agent(gh))
    assert _txn(eng2).stage is ReplanStage.PREPARED
    assert _txn(eng2).transaction_id == TXN_ID
    assert eng2.step().next_phase == "REVIEW"
    replan_calls = [c for c in eng2.provider.calls if c.phase == "REPLAN_REEXECUTE"]
    assert len(replan_calls) == 1
    assert TXN_ID in replan_calls[0].prompt
    assert eng.provider.calls == []  # the crashed process never ran it
    assert eng2.state.current_pr_url == REPLACEMENT_PR
    assert len(eng2.state.superseded_prs) == 1
    assert len(gh.closed_prs) == 1


def test_t1_prepared_with_a_bound_replacement_adopts_it_after_a_restart(tmp_state_dir):
    """PREPARED plus a marker-bearing PR: the fresh process binds, never re-invokes."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    eng2 = _restart(eng, gh)
    assert eng2.step().next_phase == "REVIEW"
    assert eng2.provider.calls == []
    assert eng2.state.current_pr_url == REPLACEMENT_PR


def test_t1_supersede_intent_with_the_receipt_adopts_the_close_after_a_restart(tmp_state_dir):
    """SUPERSEDE_INTENT reloaded: the persisted intent plus the receipt on GitHub
    prove the close was this transaction's, so it is adopted and never repeated."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_controller(gh)
    eng2 = _restart(eng, gh)
    assert _txn(eng2).close_intent_at == "2026-01-01T00:00:00+00:00"
    assert eng2.step().next_phase == "REVIEW"
    assert gh.closed_prs == [] and gh.reopened_prs == []
    assert eng2.provider.calls == []
    assert eng2.state.current_pr_url == REPLACEMENT_PR
    assert eng2.state.superseded_prs[0]["transaction_id"] == TXN_ID


def test_t1_supersede_intent_over_an_open_source_refuses_after_a_restart(tmp_state_dir):
    """The reload must not turn a recorded intent into a licence to close."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="2026-01-01T00:00:00+00:00"
    )
    eng2 = _restart(eng, gh)
    assert eng2.step().next_phase == "BLOCKED"
    assert "carries no close receipt" in eng2.state.block_reason
    assert gh.closed_prs == [] and gh.prs[PR].state == "OPEN"
    assert _txn(eng2).stage is ReplanStage.REJECTED
    persisted = load_state(eng2.paths.state_file).replan_transaction
    assert persisted["stage"] == ReplanStage.REJECTED.value


def test_t1_compensating_replays_the_reopen_after_a_restart(tmp_state_dir):
    """COMPENSATING reloaded: the persisted drift is what the reopen is for; the
    fresh process reopens the still-closed source and blocks with it open."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.COMPENSATING)
    _closed_by_controller(gh)
    eng2 = _restart(eng, gh)
    assert _txn(eng2).compensation_reason == "the replan checkpoint no longer held at the close"
    assert eng2.step().next_phase == "BLOCKED"
    assert "the replan checkpoint no longer held at the close" in eng2.state.block_reason
    assert "the close was undone" in eng2.state.block_reason
    assert "is open again" in eng2.state.block_reason
    assert len(gh.reopened_prs) == 1 and gh.prs[PR].state == "OPEN"
    assert gh.closed_prs == []  # never closed again
    assert _txn(eng2).stage is ReplanStage.REJECTED
    assert eng2.state.current_pr_url == PR and eng2.state.superseded_prs == []


def test_t1_superseded_activates_after_a_restart(tmp_state_dir):
    """SUPERSEDED reloaded: activation re-derives both checkpoints and installs."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDED, superseded_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_controller(gh)
    eng2 = _restart(eng, gh)
    assert eng2.step().next_phase == "REVIEW"
    assert gh.closed_prs == [] and gh.reopened_prs == []
    assert eng2.state.current_pr_url == REPLACEMENT_PR
    assert eng2.state.review_round == 0 and eng2.state.escalation_count == 1
    persisted = load_state(eng2.paths.state_file)
    assert persisted.replan_transaction == {}
    assert persisted.superseded_prs[0]["pr_url"] == PR


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
    gh.prs[REPLACEMENT_PR].body = _replacement(
        render_marker(
            ReplanAttestation(
                transaction_id=TXN_ID,
                execution_attempt=2,
                findings_considered=4,
                unique_constraints=2,
                tests_passed=False,  # the replacement's own tests did not pass
            )
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
    """Window 6: intent recorded, source OPEN, no receipt -> refused, never retried.

    From local state this is the same evidence as "the close landed, the
    receipt was lost to a crash, and a human reopened the PR" (R11-F1), so
    the write is never repeated from a resume; the refusal is durable and the
    source is left exactly as found.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="2026-01-01T00:00:00+00:00"
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "carries no close receipt" in eng.state.block_reason
    assert "cannot tell the two apart" in eng.state.block_reason
    assert "had already begun closing" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert gh.closed_prs == [] and gh.reopened_prs == [] and gh.commented_prs == []
    assert gh.prs[PR].state == "OPEN"
    assert eng.state.current_pr_url == PR
    assert eng.state.superseded_prs == []
    # Replayed, not re-derived: a second resume neither closes nor re-decides.
    eng.state.phase = Phase.REPLAN_REEXECUTE
    calls_before = len(gh.calls)
    assert eng.step().next_phase == "BLOCKED"
    assert gh.closed_prs == [] and len(gh.calls) == calls_before


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
    """Window 9: a resume after activation must not count the supersede twice.

    Activation installs the replacement and clears the transaction in one
    atomic save, so a journal that still names the old source while the run
    already holds the replacement was not left by a crash: it no longer
    belongs to the run and is refused (#35 R3-F1), moving no counter at all.
    """
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
    assert eng.step().next_phase == "BLOCKED"
    assert "is not the run's current PR" in eng.state.block_reason
    assert eng.state.superseded_prs == superseded  # one entry, not two
    assert eng.state.escalation_count == escalations  # nothing is counted twice
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert gh.closed_prs == [] and gh.reopened_prs == []


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
    # The close never landed, but the journal cannot know that: an OPEN source
    # under a recorded intent is refused, never retried (R11-F1).
    gh.close_error = ""
    assert eng.step().next_phase == "BLOCKED"
    assert "carries no close receipt" in eng.state.block_reason
    assert gh.prs[PR].state == "OPEN"
    assert len(gh.closed_prs) == 1  # the one failed attempt; never a second
    assert _txn(eng).stage is ReplanStage.REJECTED


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
    """ "No candidate" decides whether the agent runs again, so it must be complete.

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


def test_t6_a_truncated_all_states_listing_refuses_instead_of_reinvoking(tmp_state_dir):
    """Issue #37 T6: the exhaustive listing is what proves "no candidate exists".

    The open listing is complete and empty for this transaction; the
    all-states listing -- the last check before the agent would be invoked
    again -- cannot be completed. That is a refusal: a closed claimant is
    sitting past the truncation point, and reporting "absent" would start a
    second implementation attempt on top of it.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    gh.add_pr(
        url=REPLACEMENT_PR,
        head_sha=SHA_B,
        branch=REPLACEMENT_BRANCH,
        linked=[2],
        body=_ours_marker(),
        state="CLOSED",
    )
    strict_seen: list[bool] = []

    def truncated(repo, *, strict=False):
        gh.calls.append(("list_all_prs", repo, strict))
        strict_seen.append(strict)
        raise GitHubError(
            f"{repo} has at least 1000 pull requests, so the listing may be truncated and "
            "the set of candidates cannot be established"
        )

    gh.list_all_prs = truncated
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "cannot list replacement PR candidates" in eng.state.block_reason
    assert "truncated" in eng.state.block_reason
    assert strict_seen == [True]  # the exhaustive listing was read strictly, once
    assert ("list_open_prs", "owner/repo", True) in gh.calls
    assert eng.provider.calls == []  # no second implementation attempt
    assert _txn(eng).stage is ReplanStage.REJECTED
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
        stage=ReplanStage.PENDING,
        issue_url=ISSUE,
        decision_pr_url=PR,
        decision_head_sha=SHA_A,
        decision_branch=BRANCH,
        escalation={"trigger": "hard_review_round_threshold"},
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
        issue_url=ISSUE,
        decision_pr_url=PR,
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
        stage=ReplanStage.PENDING,
        issue_url=ISSUE,
        decision_pr_url=PR,
        decision_head_sha=SHA_A,
        decision_branch=BRANCH,
        escalation={"trigger": "hard_review_round_threshold"},
    ).to_dict()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert (
        "only an OPEN PR at a readable HEAD and branch can be superseded" in eng.state.block_reason
    )
    assert eng.provider.calls == []


@pytest.mark.parametrize(
    "drift,needle",
    [
        (lambda pr: setattr(pr, "head_sha", SHA_B), "never reviewed against this decision"),
        (lambda pr: setattr(pr, "head_ref", "autoforge/2-hand-edited"), "is on branch"),
    ],
)
def test_source_moving_between_the_review_and_the_prepare_is_refused(tmp_state_dir, drift, needle):
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
    assert txn.decision_pr_url == PR
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
    """A hand-edited or pre-upgrade journal cannot prove what was reviewed.

    The journal is refused on load (the decision point is what PENDING
    records), and the verifier refuses the same gap again on its own.
    """
    from autoforge.github import PRInfo
    from autoforge.replan_txn import verify_decision_point

    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    txn = ReplanTransaction(
        stage=ReplanStage.PENDING, escalation={"trigger": "hard_review_round_threshold"}
    )
    eng.state.replan_transaction = txn.to_dict()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "decision_head_sha is required at stage 'pending'" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)
    source = PRInfo(url=PR, number=42, title="PR", state="OPEN", head_sha=SHA_A, head_ref=BRANCH)
    assert "does not record the PR whose review decided it" in verify_decision_point(source, txn)
    txn.decision_pr_url = PR
    assert "does not record the reviewed HEAD" in verify_decision_point(source, txn)


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


@pytest.mark.parametrize(
    "stage",
    [
        ReplanStage.PENDING,
        ReplanStage.PREPARED,
        ReplanStage.VERIFIED,
        ReplanStage.SUPERSEDE_INTENT,
        ReplanStage.COMPENSATING,
        ReplanStage.SUPERSEDED,
    ],
    ids=lambda s: s.value,
)
def test_t3_a_dry_run_from_replan_reexecute_neither_writes_nor_invokes(tmp_state_dir, stage):
    """Issue #37 T3: dry-run is a controller invariant in the destructive phase too.

    From every stage a crash can leave, a dry-run step renders the plan and
    nothing else: no agent, no `gh` call of any kind (so no close, reopen or
    comment), no journal movement and no state write.
    """
    gh = FakeGitHub()
    eng, txn = _seeded_engine(tmp_state_dir, gh, stage)
    if stage in (ReplanStage.SUPERSEDE_INTENT, ReplanStage.COMPENSATING, ReplanStage.SUPERSEDED):
        _closed_by_controller(gh)
    eng.save()
    state_bytes = eng.paths.state_file.read_bytes()
    calls_before = list(gh.calls)
    out = eng.step(dry_run=True)
    assert out.dry_run and out.plan is not None
    assert out.plan.template == "replan_reexecute.md"
    assert eng.provider.calls == []
    assert gh.calls == calls_before
    assert gh.closed_prs == [] and gh.reopened_prs == [] and gh.commented_prs == []
    assert eng.paths.state_file.read_bytes() == state_bytes
    assert eng.state.phase is Phase.REPLAN_REEXECUTE
    assert eng.state.replan_transaction == txn.to_dict()
    assert not eng.paths.logs_dir.exists()


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
        findings=[{"id": "R1-F1", "round": 1, "classification": "nit", "required_resolution": run}],
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


_HOSTILE_BODIES = {
    "unterminated marker then blanks": f"<!-- {MARKER_NAME}: " + " " * 65000,
    "unterminated payload then blanks": f"<!-- {MARKER_NAME}: a" + " " * 65000,
    "many unterminated markers with blank runs": (f"<!-- {MARKER_NAME}: a" + " " * 3000) * 20,
    "unterminated marker then text": f"<!-- {MARKER_NAME}: " + "a" * 65000,
    "named openers only": f"<!-- {MARKER_NAME}:" * 1900,
}


@pytest.mark.parametrize("shape", sorted(_HOSTILE_BODIES))
def test_scanning_a_hostile_body_takes_linear_time(shape):
    """Same pattern shape, same cost contract as `claims.MarkerKind.pattern`:
    the candidate listing scans every open PR body in the repository."""
    body = _HOSTILE_BODIES[shape]
    assert len(body) <= 65536
    started = time.perf_counter()
    result = scan_replan_markers(body)
    assert time.perf_counter() - started < 1.0, shape
    assert result.attestations == [] and result.malformed == []


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


def test_r11f1_a_close_that_lost_its_receipt_to_a_crash_is_never_repeated(tmp_state_dir):
    """R11-F1: close landed -> crash before the receipt -> human reopen -> resume.

    The source is OPEN with no receipt, exactly as a crash *before* the close
    would leave it. The old resume treated that as an unattempted write and
    closed again, overriding the human's reopen. It must block instead: no
    second close, no activation, no reopen, and a refusal that survives a
    further resume.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)

    def die(*_args, **_kwargs):
        raise RuntimeError("process died after the close landed, before the receipt")

    eng.github.comment_pr = die  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        eng.step()
    assert gh.prs[PR].state == "CLOSED"  # the close landed ...
    assert gh.commented_prs == []  # ... but no receipt was published
    assert not has_close_receipt([c.body for c in gh.get_pr_comments(PR)], TXN_ID)
    crashed = _txn(eng)
    assert crashed.stage is ReplanStage.SUPERSEDE_INTENT

    gh.prs[PR].state = "OPEN"  # a human reopens it before the resume
    resumed = make_engine(tmp_state_dir, ["the replan agent must not run"], github=gh)
    resumed.state.phase = Phase.REPLAN_REEXECUTE
    resumed.state.current_issue_url = ISSUE
    resumed.state.current_pr_url = PR
    resumed.state.replan_transaction = crashed.to_dict()
    out = resumed.step()
    assert out.next_phase == "BLOCKED"
    assert "carries no close receipt" in resumed.state.block_reason
    assert "reopened by a human" in resumed.state.block_reason
    assert _txn(resumed).stage is ReplanStage.REJECTED
    assert len(gh.closed_prs) == 1  # never a second close
    assert gh.prs[PR].state == "OPEN"  # the human's reopen stands
    assert gh.reopened_prs == [] and gh.commented_prs == []
    assert resumed.state.current_pr_url == PR  # the replacement was not activated
    assert resumed.state.superseded_prs == []
    assert resumed.state.escalation_count == 0
    # The refusal is durable: another resume replays it without touching GitHub.
    resumed.state.phase = Phase.REPLAN_REEXECUTE
    calls_before = len(gh.calls)
    assert resumed.step().next_phase == "BLOCKED"
    assert len(gh.calls) == calls_before and len(gh.closed_prs) == 1


def test_r11f1_an_unreadable_comment_list_over_an_open_source_is_unknown_then_refused(
    tmp_state_dir,
):
    """The OPEN-at-intent refusal keeps the transient/conclusive split."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="2026-01-01T00:00:00+00:00"
    )
    gh.comments_error = GitHubUnavailableError("gh: 502 Bad Gateway")
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert _txn(eng).stage is ReplanStage.SUPERSEDE_INTENT  # still resumable
    gh.comments_error = GitHubError("gh: not found")
    assert eng.step().next_phase == "BLOCKED"
    assert "a prior close cannot be ruled out" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert gh.closed_prs == [] and gh.prs[PR].state == "OPEN"


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
        (lambda gh: _drift_replacement(gh, body=IMPLEMENTATION_MARKER), "no longer carries"),
        (
            lambda gh: _drift_replacement(gh, body=_ours_marker()),
            "does not carry the ai-implementation marker",
        ),
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


@pytest.mark.parametrize(
    "drift,needle",
    [
        (lambda gh: setattr(gh.prs[PR], "state", "OPEN"), "expected CLOSED"),
        (lambda gh: gh.set_head(SHA_C, PR), "advanced from the checkpointed HEAD"),
        (
            lambda gh: setattr(gh.prs[PR], "head_ref", "somebody/else"),
            "moved from the checkpointed branch",
        ),
    ],
    ids=["reopened", "head", "branch"],
)
def test_f3_a_source_that_drifted_before_activation_blocks(tmp_state_dir, drift, needle):
    """Both checkpoints are re-derived, not just the replacement's (issue #37 T4).

    A reopen, a push, or a branch move on the closed source inside the
    activation window is terminal, not compensable: the close was confirmed
    correct against the last reads before it, so the source is not reopened
    and the replacement is not installed.
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDED, superseded_at="2026-01-01T00:00:00+00:00"
    )
    _closed_by_controller(gh)
    drift(gh)
    assert eng.step().next_phase == "BLOCKED"
    assert "can no longer be activated" in eng.state.block_reason
    assert needle in eng.state.block_reason
    assert eng.state.superseded_prs == []
    assert eng.state.current_pr_url == PR  # nothing was installed
    assert gh.reopened_prs == [] and gh.closed_prs == []  # terminal: no compensation
    assert _txn(eng).stage is ReplanStage.REJECTED


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


def test_n5_a_marker_whose_payload_contains_an_opener_is_not_a_complete_marker():
    """Issue #37 N5, pinned: the payload may not span a comment delimiter.

    `<!-- autoforge-replan-transaction: {"x": "<!--"} -->` is not matched as a
    marker at all: the inner `<!--` ends the payload, so the outer text is an
    unterminated marker (evidence of nothing) and the inner one is not a
    named marker. That is the documented rule (AGENTS.md: "the payload may
    not span a comment delimiter"), and it is what keeps an unterminated
    marker from swallowing a valid one that follows it. Treating such a body
    as *malformed* instead would be a change of marker semantics, not a bug
    fix, and is deliberately not made here.
    """
    good = render_marker(
        ReplanAttestation(
            transaction_id=TXN_ID,
            execution_attempt=2,
            findings_considered=1,
            unique_constraints=1,
            tests_passed=True,
        )
    )
    spanning = f'<!-- {MARKER_NAME}: {{"transaction_id": "<!--"}} -->'
    alone = scan_replan_markers(spanning)
    assert alone.attestations == [] and alone.malformed == []
    scan = scan_replan_markers(f"{spanning}\n{good}")
    assert [a.transaction_id for a in scan.attestations] == [TXN_ID]
    assert scan.malformed == []


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


# =============================================================================
# Issue #35: a corrupt journal is refused, never crashed on (F2), the source
# must have a readable branch (F1), SHA comparison is one rule (N2), and a
# malformed URL is a refusal rather than an escaped ConfigurationError (N3)
# =============================================================================

CORRUPT_TXN_FIELDS = [
    ("pr_number_watermark", "12"),
    ("pr_number_watermark", -1),
    ("evidence_finding_count", True),
    ("escalation", ["hard_review_round_threshold"]),
    ("preexisting_pr_urls", PR),
    ("preexisting_pr_urls", [PR, 41]),
    ("source_pr_url", 42),
    ("source_pr_url", "not a url"),
    ("source_pr_url", ISSUE),  # an issue URL where a PR URL belongs
    ("issue_url", PR),
    ("replacement_pr_url", ["https://github.com/owner/repo/pull/43"]),
    ("rendered_findings", None),
    ("stage", 7),
    ("stage", None),
]


@pytest.mark.parametrize("name,value", CORRUPT_TXN_FIELDS, ids=lambda v: repr(v))
def test_f2_a_corrupt_field_becomes_a_rejected_transaction(name, value):
    """Every malformed persisted field is a named defect and a terminal stage."""
    data = _seed_dict(ReplanStage.VERIFIED)
    data[name] = value
    txn = ReplanTransaction.from_dict(data)
    assert txn.stage is ReplanStage.REJECTED
    assert txn.journal_defects and name in txn.journal_defects[0]
    assert "persisted replan transaction is corrupt" in txn.rejection_reason
    assert name in txn.rejection_reason and "recorded stage" in txn.rejection_reason
    # The unreadable field is replaced in memory only; nothing else is lost.
    if name not in ("stage", "source_pr_url"):
        assert txn.source_pr_url == PR
    assert txn.transaction_id == TXN_ID
    assert "journal_defects" not in txn.to_dict()


def _seed_dict(stage: ReplanStage) -> dict:
    """A well-formed persisted transaction at ``stage``."""
    return _seed_txn(stage).to_dict()


def test_f2_from_dict_round_trips_a_well_formed_journal_without_defects():
    data = _seed_dict(ReplanStage.SUPERSEDE_INTENT)
    data["from_a_future_version"] = {"ignored": True}  # unknown keys stay ignored
    txn = ReplanTransaction.from_dict(data)
    assert txn.journal_defects == [] and txn.stage is ReplanStage.SUPERSEDE_INTENT
    del data["from_a_future_version"]
    assert txn.to_dict() == data


def test_f2_a_recorded_rejection_survives_inside_the_corruption_reason():
    data = _seed_dict(ReplanStage.REJECTED)
    data["rejection_reason"] = "the replacement attests tests_passed=false"
    data["escalation"] = "oops"
    txn = ReplanTransaction.from_dict(data)
    assert txn.stage is ReplanStage.REJECTED
    assert "escalation must be an object" in txn.rejection_reason
    assert "recorded rejection: the replacement attests tests_passed=false" in txn.rejection_reason


def test_f2_a_non_object_journal_still_raises():
    with pytest.raises(ValueError, match="must be a JSON object"):
        ReplanTransaction.from_dict(["not", "an", "object"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name,value",
    [
        ("pr_number_watermark", "12"),  # str.__le__ on an int: TypeError before #35
        ("escalation", ["hard_review_round_threshold"]),  # list.get: AttributeError before #35
        ("source_pr_url", "not a url"),  # ConfigurationError from get_pr before #35
        ("replacement_pr_url", 43),
    ],
    ids=lambda v: repr(v),
)
def test_f2_a_corrupt_journal_blocks_on_resume_instead_of_crashing(tmp_state_dir, name, value):
    """A resume over a corrupt transaction fails closed: no crash, no close, no agent."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    eng.state.replan_transaction[name] = value
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert "persisted replan transaction is corrupt" in reason and name in reason
    # The journal could not be read, so the block must not claim to know what
    # happened to the source PR on the strength of defaulted fields.
    assert "cannot be determined from local state" in reason
    assert "nothing was closed or merged" not in reason
    _assert_source_untouched(eng, gh)
    assert eng.provider.calls == []
    # The corrupt evidence stays on disk, unlaundered, and a second resume
    # replays the same refusal from it.
    assert load_state(eng.paths.state_file).replan_transaction[name] == value
    eng.state.phase = Phase.REPLAN_REEXECUTE
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.block_reason == reason
    assert gh.closed_prs == [] and eng.provider.calls == []


def test_f2_a_corrupt_journal_at_pending_never_reaches_prepare(tmp_state_dir):
    """PENDING is where the agent would be invoked; corruption must stop before it."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    eng.state.replan_transaction["escalation"] = "hard_review_round_threshold"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "escalation must be an object" in eng.state.block_reason
    assert load_state(eng.paths.state_file).replan_transaction["transaction_id"] == ""
    assert [c.phase for c in eng.provider.calls] == ["REVIEW"]
    _assert_source_untouched(eng, gh)


def test_f1_a_source_without_a_readable_branch_is_refused_at_prepare(tmp_state_dir):
    """An empty head_ref would checkpoint source_branch="" and enforce nothing later."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch="", linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    eng.state.current_branch = ""
    eng.state.current_head_sha = SHA_A
    eng.state.replan_transaction = ReplanTransaction(
        stage=ReplanStage.PENDING,
        issue_url=ISSUE,
        decision_pr_url=PR,
        decision_head_sha=SHA_A,
        decision_branch=BRANCH,
        escalation={"trigger": "hard_review_round_threshold"},
    ).to_dict()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "branch (unreadable)" in eng.state.block_reason
    assert "only an OPEN PR at a readable HEAD and branch can be superseded" in (
        eng.state.block_reason
    )
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert _txn(eng).transaction_id == ""
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_n2_sha_comparison_is_case_insensitive_and_never_vacuous():
    from autoforge.github import PRInfo
    from autoforge.replan_txn import verify_closed_source, verify_source_checkpoint

    txn = ReplanTransaction(
        transaction_id=TXN_ID,
        stage=ReplanStage.VERIFIED,
        issue_url=ISSUE,
        decision_head_sha=SHA_A.upper(),
        source_pr_url=PR,
        source_branch=BRANCH,
        source_head_sha=SHA_A,
        base_branch="main",
    )
    upper = PRInfo(
        url=PR, number=42, title="PR", state="OPEN", head_sha=SHA_A.upper(), head_ref=BRANCH
    )
    assert verify_source_checkpoint(upper, txn) == ""
    unreadable = PRInfo(url=PR, number=42, title="PR", state="CLOSED", head_sha="", head_ref=BRANCH)
    txn.source_head_sha = ""
    assert "advanced from the checkpointed HEAD" in verify_closed_source(unreadable, txn)


def test_n3_a_malformed_pr_url_from_the_listing_is_a_refusal_not_a_crash(tmp_state_dir):
    from autoforge.github import PRInfo

    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    gh.prs["garbage"] = PRInfo(
        url="garbage", number=99, title="?", state="OPEN", head_sha=SHA_B, repository="owner/repo"
    )
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.replan_transaction = ReplanTransaction(
        stage=ReplanStage.PENDING,
        issue_url=ISSUE,
        decision_pr_url=PR,
        decision_head_sha=SHA_A,
        decision_branch=BRANCH,
        escalation={"trigger": "hard_review_round_threshold"},
    ).to_dict()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "cannot checkpoint the replan source" in eng.state.block_reason
    assert "garbage" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED and eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_n3_verify_target_pr_reports_an_unusable_issue_url_instead_of_raising():
    from autoforge.github import PRInfo
    from autoforge.replan_txn import verify_target_pr

    txn = _txn_for_marker_tests()
    txn.issue_url = "not an issue url"
    txn.base_branch = "main"
    pr = PRInfo(
        url=REPLACEMENT_PR,
        number=43,
        title="PR",
        state="OPEN",
        head_sha=SHA_B,
        base_ref="main",
        head_ref=REPLACEMENT_BRANCH,
        repository="owner/repo",
        linked_issue_numbers=[2],
    )
    drift = verify_target_pr(pr, txn, "owner/repo", require_checkpoint_head=False)
    assert "no usable issue URL" in drift


# =============================================================================
# Issue #35, review round 2: an *incomplete* journal is corruption (R2-F1),
# and semantic values are validated rather than normalised away (R2-F2)
# =============================================================================


def _pr(url: str = PR, *, state: str = "OPEN", head_sha: str = SHA_A, head_ref: str = BRANCH):
    from autoforge.github import PRInfo

    return PRInfo(url=url, number=42, title="PR", state=state, head_sha=head_sha, head_ref=head_ref)


def test_r2f1_required_fields_accumulate_along_the_lifecycle():
    from autoforge.replan_txn import required_fields_at

    prepared = required_fields_at(ReplanStage.PREPARED)
    assert "decision_head_sha" in prepared and "source_branch" in prepared
    assert "evidence_finding_count" in prepared and "rendered_findings" in prepared
    assert set(prepared) < set(required_fields_at(ReplanStage.VERIFIED))
    assert set(required_fields_at(ReplanStage.VERIFIED)) < set(
        required_fields_at(ReplanStage.SUPERSEDE_INTENT)
    )
    for terminal in (ReplanStage.COMPENSATING, ReplanStage.SUPERSEDED):
        assert set(required_fields_at(ReplanStage.SUPERSEDE_INTENT)) < set(
            required_fields_at(terminal)
        )
    assert "superseded_at" not in required_fields_at(ReplanStage.COMPENSATING)
    assert "compensation_reason" not in required_fields_at(ReplanStage.SUPERSEDED)
    assert required_fields_at(ReplanStage.REJECTED) == ()


@pytest.mark.parametrize(
    "stage,name",
    [
        (ReplanStage.PENDING, "decision_pr_url"),
        (ReplanStage.PENDING, "decision_head_sha"),
        (ReplanStage.PENDING, "decision_branch"),
        (ReplanStage.PENDING, "escalation"),
        (ReplanStage.PREPARED, "transaction_id"),
        (ReplanStage.PREPARED, "source_branch"),
        (ReplanStage.PREPARED, "source_head_sha"),
        (ReplanStage.PREPARED, "source_review_round"),
        (ReplanStage.PREPARED, "base_branch"),
        (ReplanStage.PREPARED, "evidence_finding_count"),
        (ReplanStage.PREPARED, "rendered_findings"),
        (ReplanStage.PREPARED, "rendered_observations"),
        (ReplanStage.PREPARED, "rendered_verification_failures"),
        (ReplanStage.PREPARED, "preexisting_pr_urls"),
        (ReplanStage.PREPARED, "pr_number_watermark"),
        (ReplanStage.PREPARED, "expected_execution_attempt"),
        (ReplanStage.VERIFIED, "source_branch"),
        (ReplanStage.VERIFIED, "evidence_finding_count"),
        (ReplanStage.VERIFIED, "replacement_pr_url"),
        (ReplanStage.VERIFIED, "replacement_branch"),
        (ReplanStage.VERIFIED, "replacement_head_sha"),
        (ReplanStage.VERIFIED, "attested_findings_considered"),
        (ReplanStage.SUPERSEDE_INTENT, "close_intent_at"),
        (ReplanStage.SUPERSEDE_INTENT, "source_branch"),
        (ReplanStage.SUPERSEDE_INTENT, "rendered_findings"),
        (ReplanStage.SUPERSEDE_INTENT, "attested_findings_considered"),
        (ReplanStage.COMPENSATING, "compensation_reason"),
        (ReplanStage.SUPERSEDED, "superseded_at"),
        (ReplanStage.SUPERSEDED, "replacement_branch"),
        (ReplanStage.SUPERSEDED, "evidence_finding_count"),
    ],
    ids=lambda v: v.value if isinstance(v, ReplanStage) else v,
)
def test_r2f1_a_field_the_stage_requires_may_be_neither_empty_nor_missing(stage, name):
    """Type-valid but incomplete is still corrupt: the checkpoint was never proven."""
    empty = _seed_dict(stage)
    empty[name] = type(empty[name])()  # "", 0, [] or {} -- the dataclass default
    txn = ReplanTransaction.from_dict(empty)
    assert txn.stage is ReplanStage.REJECTED
    assert txn.journal_defects == [f"{name} is required at stage {stage.value!r} but empty"]
    missing = _seed_dict(stage)
    del missing[name]
    txn = ReplanTransaction.from_dict(missing)
    assert txn.stage is ReplanStage.REJECTED
    assert txn.journal_defects == [f"{name} is required at stage {stage.value!r} but missing"]


def test_r2f1_a_rejected_journal_requires_nothing_beyond_its_types():
    """REJECTED is reachable from every stage, so no field is implied by it."""
    data = _seed_dict(ReplanStage.REJECTED)
    data.update(source_branch="", replacement_pr_url="", close_intent_at="")
    txn = ReplanTransaction.from_dict(data)
    assert txn.journal_defects == [] and txn.stage is ReplanStage.REJECTED


@pytest.mark.parametrize("stage", [ReplanStage.COMPENSATING, ReplanStage.SUPERSEDED])
def test_r2f1_the_terminal_stages_load_cleanly_when_complete(stage):
    txn = ReplanTransaction.from_dict(_seed_dict(stage))
    assert txn.journal_defects == [] and txn.stage is stage


def test_r2f1_a_verified_journal_without_a_source_branch_never_closes_on_resume(tmp_state_dir):
    """The reviewer's reproduction: OPEN source, matching HEAD, source_branch=""."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    eng.state.replan_transaction["source_branch"] = ""
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert "source_branch is required at stage 'verified' but empty" in reason
    assert "cannot be determined from local state" in reason
    _assert_source_untouched(eng, gh)
    assert gh.closed_prs == [] and eng.provider.calls == []
    assert eng.state.current_pr_url == PR and eng.state.superseded_prs == []
    assert load_state(eng.paths.state_file).replan_transaction["source_branch"] == ""
    eng.state.phase = Phase.REPLAN_REEXECUTE
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.block_reason == reason and gh.closed_prs == []


def test_r2f1_an_intent_without_a_timestamp_is_not_a_licence_to_close(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT, close_intent_at="")
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "close_intent_at is required at stage 'supersede_intent'" in eng.state.block_reason
    _assert_source_untouched(eng, gh)


def test_r2f1_an_incomplete_superseded_journal_is_not_activated(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.SUPERSEDED, replacement_branch="")
    _closed_by_controller(gh)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "replacement_branch is required at stage 'superseded'" in eng.state.block_reason
    assert eng.state.current_pr_url == PR and eng.state.superseded_prs == []
    assert eng.state.escalation_count == 0 and gh.reopened_prs == []


def test_r2f1_the_source_verifiers_refuse_an_empty_checkpointed_branch():
    """Defence in depth at the point of use: never vacuous, like `_same_sha`."""
    from autoforge.replan_txn import verify_closed_source, verify_source_checkpoint

    txn = _seed_txn(ReplanStage.VERIFIED, source_branch="")
    drift = verify_source_checkpoint(_pr(), txn)
    assert "records no branch for source PR" in drift
    drift = verify_closed_source(_pr(state="CLOSED"), txn)
    assert "records no branch for source PR" in drift
    txn.source_branch = BRANCH
    assert verify_source_checkpoint(_pr(), txn) == ""
    assert verify_closed_source(_pr(state="CLOSED"), txn) == ""
    assert "moved from the checkpointed branch" in (
        verify_closed_source(_pr(state="CLOSED", head_ref="other"), txn)
    )


def test_r2f1_the_target_verifier_requires_the_branch_once_it_is_a_checkpoint():
    from autoforge.github import PRInfo
    from autoforge.replan_txn import verify_target_pr

    txn = _seed_txn(ReplanStage.VERIFIED, replacement_branch="")
    target = PRInfo(
        url=REPLACEMENT_PR,
        number=43,
        title="PR",
        state="OPEN",
        head_sha=SHA_B,
        base_ref="main",
        head_ref=REPLACEMENT_BRANCH,
        repository="owner/repo",
        linked_issue_numbers=[2],
        body=IMPLEMENTATION_MARKER,
    )
    # While binding, the branch is about to *become* the checkpoint.
    assert verify_target_pr(target, txn, "owner/repo", require_checkpoint_head=False) == ""
    drift = verify_target_pr(target, txn, "owner/repo", require_checkpoint_head=True)
    assert "records no branch for replacement PR" in drift


@pytest.mark.parametrize(
    "name,value,needle",
    [
        ("transaction_id", "bad", "32 lowercase hex characters"),
        ("transaction_id", TXN_ID.upper(), "32 lowercase hex characters"),
        ("preexisting_pr_urls", ["not-a-url"], "preexisting_pr_urls entry 'not-a-url'"),
        ("preexisting_pr_urls", [PR, ISSUE], f"preexisting_pr_urls entry '{ISSUE}'"),
    ],
    ids=lambda v: repr(v) if not isinstance(v, str) or len(v) < 20 else "...",
)
def test_r2f2_semantic_values_are_validated_not_normalised(name, value, needle):
    data = _seed_dict(ReplanStage.PREPARED)
    data[name] = value
    txn = ReplanTransaction.from_dict(data)
    assert txn.stage is ReplanStage.REJECTED
    assert len(txn.journal_defects) == 1 and needle in txn.journal_defects[0]


def test_r2f2_a_pending_journal_may_not_carry_a_transaction_id():
    """The id is created with PREPARED; one at PENDING was written by somebody else."""
    data = ReplanTransaction(
        stage=ReplanStage.PENDING,
        issue_url=ISSUE,
        decision_pr_url=PR,
        decision_head_sha=SHA_A,
        decision_branch=BRANCH,
        escalation={"trigger": "hard_review_round_threshold"},
        transaction_id=TXN_ID,
    ).to_dict()
    txn = ReplanTransaction.from_dict(data)
    assert txn.stage is ReplanStage.REJECTED
    assert txn.journal_defects == ["transaction_id is set at stage 'pending', before it is created"]


def test_r2f2_a_bad_id_at_pending_is_refused_not_overwritten(tmp_state_dir):
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    eng.state.replan_transaction["transaction_id"] = "bad"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "transaction_id must be 32 lowercase hex characters" in eng.state.block_reason
    # Not laundered into a fresh id by `_prepare_replan`, and no agent ran.
    assert load_state(eng.paths.state_file).replan_transaction["transaction_id"] == "bad"
    assert [c.phase for c in eng.provider.calls] == ["REVIEW"]
    _assert_source_untouched(eng, gh)


def test_r2f2_an_unusable_preexisting_url_blocks_on_resume_instead_of_being_dropped(
    tmp_state_dir,
):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED)
    eng.state.replan_transaction["preexisting_pr_urls"] = [PR, "garbage"]
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "preexisting_pr_urls entry 'garbage'" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


# =============================================================================
# #35 review round 3: the journal is bound to the run, and PENDING binds the
# branch it decided on
# =============================================================================

FOREIGN_PR = "https://github.com/other/repo/pull/42"


def test_r3f1_a_verified_journal_naming_a_pr_in_another_repository_never_closes_it(
    tmp_state_dir,
):
    """The reviewer's reproduction: every source verifier compares GitHub with
    the journal, so a foreign PR that matches its own checkpoint would pass
    them all. The run's own repository and PR are what the source is bound to."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED, source_pr_url=FOREIGN_PR)
    # The foreign PR is exactly what the substituted checkpoint says it is.
    gh.add_pr(url=FOREIGN_PR, head_sha=SHA_A, branch=BRANCH, linked=[2])
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert f"checkpointed source PR {FOREIGN_PR} is not in owner/repo" in reason
    assert gh.prs[FOREIGN_PR].state == "OPEN" and gh.prs[PR].state == "OPEN"
    assert gh.closed_prs == [] and gh.reopened_prs == [] and eng.provider.calls == []
    assert eng.state.current_pr_url == PR and eng.state.superseded_prs == []
    assert _txn(eng).stage is ReplanStage.REJECTED
    # The evidence stays on disk as it was; the refusal replays on resume.
    assert load_state(eng.paths.state_file).replan_transaction["source_pr_url"] == FOREIGN_PR
    eng.state.phase = Phase.REPLAN_REEXECUTE
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.block_reason == reason and gh.closed_prs == []


@pytest.mark.parametrize(
    "stage",
    [
        ReplanStage.PREPARED,
        ReplanStage.VERIFIED,
        ReplanStage.SUPERSEDE_INTENT,
        ReplanStage.COMPENSATING,
        ReplanStage.SUPERSEDED,
    ],
    ids=lambda v: v.value,
)
def test_r3f1_a_journal_about_another_pr_of_this_repository_is_refused_at_every_stage(
    tmp_state_dir, stage
):
    """Same repository, different PR: the transaction does not belong to this run.

    Nothing may act on it, whichever stage it claims: not the agent (PREPARED),
    not the close (VERIFIED, SUPERSEDE_INTENT), not the reopen (COMPENSATING),
    and not the activation (SUPERSEDED).
    """
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, stage, source_pr_url=EARLIER_PR)
    gh.add_pr(url=EARLIER_PR, head_sha=SHA_A, branch=BRANCH, linked=[2])
    if stage is ReplanStage.SUPERSEDED:
        _closed_by_controller(gh, EARLIER_PR)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert f"checkpointed source PR {EARLIER_PR} is not the run's current PR {PR}" in (
        eng.state.block_reason
    )
    assert gh.closed_prs == [] and gh.reopened_prs == [] and eng.provider.calls == []
    assert gh.prs[PR].state == "OPEN"
    assert eng.state.current_pr_url == PR and eng.state.superseded_prs == []
    assert eng.state.escalation_count == 0
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert load_state(eng.paths.state_file).replan_transaction["source_pr_url"] == EARLIER_PR


def test_r3f1_a_journal_substituted_while_the_agent_ran_never_reaches_the_close(tmp_state_dir):
    """The post-agent path does not re-enter the reducer, so the binding is
    checked again immediately before the destructive write."""
    gh = FakeGitHub()
    holder: dict = {}
    # The agent's claim agrees with the substituted journal, so the claim
    # check passes and only the binding stands between it and the close.
    inner = _replan_agent(gh, payload_over={"previous_pr_url": EARLIER_PR})

    def agent(req):
        if req.phase == "REPLAN_REEXECUTE":
            holder["eng"].state.replan_transaction["source_pr_url"] = EARLIER_PR
        return inner(req)

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    holder["eng"] = eng
    gh.add_pr(url=EARLIER_PR, head_sha=SHA_A, branch=BRANCH, linked=[2])
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "is not the run's current PR" in eng.state.block_reason
    assert gh.closed_prs == [] and gh.prs[PR].state == "OPEN"
    assert eng.state.current_pr_url == PR and eng.state.superseded_prs == []


def test_r3f1_the_run_binding_compares_identity_not_url_strings():
    from autoforge.replan_txn import verify_run_binding

    txn = _seed_txn(ReplanStage.VERIFIED)
    assert verify_run_binding(txn, "owner/repo", PR, ISSUE) == ""
    # GitHub owner and repository names are case-insensitive.
    assert verify_run_binding(txn, "Owner/Repo", PR, ISSUE) == ""
    assert (
        verify_run_binding(txn, "owner/repo", "https://github.com/Owner/REPO/pull/42", ISSUE) == ""
    )
    assert "is not in other/repo" in verify_run_binding(txn, "other/repo", PR, ISSUE)
    assert "is not the run's current PR" in verify_run_binding(txn, "owner/repo", EARLIER_PR, ISSUE)
    assert "current PR URL is unusable" in verify_run_binding(txn, "owner/repo", "", ISSUE)
    assert "current PR URL is unusable" in verify_run_binding(txn, "owner/repo", ISSUE, ISSUE)
    # PENDING names no checkpointed source yet; every later stage must.
    pending = _seed_txn(ReplanStage.PENDING, source_pr_url="")
    assert verify_run_binding(pending, "owner/repo", PR, ISSUE) == ""
    prepared = _seed_txn(ReplanStage.PREPARED, source_pr_url="")
    assert "names no source PR" in verify_run_binding(prepared, "owner/repo", PR, ISSUE)
    assert "is unusable" in verify_run_binding(
        _seed_txn(ReplanStage.PREPARED, source_pr_url=ISSUE), "owner/repo", PR, ISSUE
    )


def test_r4f1_the_run_binding_is_two_sided_and_binds_the_decision_pr_at_every_stage():
    """`current_pr_url` is persisted state too, so the decision REVIEW recorded
    must name its PR and that PR must be the one the run holds -- at PENDING,
    where it is the only source identity there is, and afterwards, where it
    pins the checkpoint to the decision."""
    from autoforge.replan_txn import verify_run_binding

    pending = _seed_txn(ReplanStage.PENDING, source_pr_url="")
    assert verify_run_binding(pending, "owner/repo", PR, ISSUE) == ""
    assert (
        verify_run_binding(pending, "Owner/Repo", "https://github.com/Owner/REPO/pull/42", ISSUE)
        == ""
    )
    # The run now holds another PR: the decision was not made on it.
    assert f"decision PR {PR} is not the run's current PR {EARLIER_PR}" in verify_run_binding(
        pending, "owner/repo", EARLIER_PR, ISSUE
    )
    assert "is not in other/repo" in verify_run_binding(pending, "other/repo", PR, ISSUE)
    for stage in ReplanStage:
        if stage is ReplanStage.REJECTED:
            continue
        # A decision recorded for another PR is refused whatever the checkpoint says.
        assert "is not the run's current PR" in verify_run_binding(
            _seed_txn(stage, decision_pr_url=EARLIER_PR), "owner/repo", PR, ISSUE
        ), stage
        assert "does not record the PR whose review decided it" in verify_run_binding(
            _seed_txn(stage, decision_pr_url=""), "owner/repo", PR, ISSUE
        ), stage
        assert "decision PR URL is unusable" in verify_run_binding(
            _seed_txn(stage, decision_pr_url=ISSUE), "owner/repo", PR, ISSUE
        ), stage
    # A checkpoint that does not bind is reported before the decision is looked at.
    assert "checkpointed source PR" in verify_run_binding(
        _seed_txn(ReplanStage.PREPARED, source_pr_url=EARLIER_PR, decision_pr_url=EARLIER_PR),
        "owner/repo",
        PR,
        ISSUE,
    )


def test_r4f1_the_decision_point_verifier_binds_the_pr_it_reads():
    from autoforge.replan_txn import verify_decision_point

    txn = _seed_txn(ReplanStage.PENDING, source_pr_url="")
    assert verify_decision_point(_pr(), txn) == ""
    assert verify_decision_point(_pr(url="https://github.com/Owner/REPO/pull/42"), txn) == ""
    assert "never reviewed against this decision" in verify_decision_point(_pr(url=EARLIER_PR), txn)
    assert "read back as (none)" in verify_decision_point(_pr(url=""), txn)
    txn.decision_pr_url = ""
    assert "does not record the PR whose review decided it" in verify_decision_point(_pr(), txn)


def test_r4f1_a_pending_decision_for_another_pr_never_prepares_the_run_s_current_pr(
    tmp_state_dir,
):
    """The reviewer's reproduction: REVIEW decides a replan for PR A, then the
    run's `current_pr_url` is substituted with same-repository PR B at the
    same HEAD on the same branch (one branch, two bases). PR B satisfies the
    revision half of the decision, so before the PR half was recorded the
    prepare step would checkpoint B as the source and the close would go to
    a PR no review decided on."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    txn = _txn(eng)
    assert txn.stage is ReplanStage.PENDING and txn.decision_pr_url == PR
    gh.add_pr(url=EARLIER_PR, head_sha=SHA_A, branch=BRANCH, linked=[2], base_ref="develop")
    eng.state.current_pr_url = EARLIER_PR
    eng._save()
    calls_before = len(eng.provider.calls)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert f"decision PR {PR} is not the run's current PR {EARLIER_PR}" in eng.state.block_reason
    assert len(eng.provider.calls) == calls_before
    assert gh.closed_prs == [] and gh.reopened_prs == []
    assert gh.prs[PR].state == "OPEN" and gh.prs[EARLIER_PR].state == "OPEN"
    assert eng.state.superseded_prs == [] and eng.state.escalation_count == 0
    txn = _txn(eng)
    assert txn.stage is ReplanStage.REJECTED
    assert txn.transaction_id == "" and txn.source_pr_url == ""  # nothing was checkpointed
    # The refusal replays: a resume cannot prepare it either.
    eng.state.phase = Phase.REPLAN_REEXECUTE
    assert eng.step().next_phase == "BLOCKED"
    assert len(eng.provider.calls) == calls_before and gh.closed_prs == []


def test_r4f1_a_pending_decision_naming_no_pr_is_corruption_not_a_free_source(tmp_state_dir):
    """A journal written before the PR half of the decision existed is refused
    on load like every other incomplete checkpoint, rather than prepared from
    whatever `current_pr_url` holds."""
    gh = FakeGitHub()
    eng = _pending_at_the_source(tmp_state_dir, gh)
    del eng.state.replan_transaction["decision_pr_url"]
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "decision_pr_url is required at stage 'pending' but missing" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)
    assert "decision_pr_url" not in load_state(eng.paths.state_file).replan_transaction


def test_r4f1_the_prepared_source_is_read_from_github_and_must_be_the_decision_pr(
    tmp_state_dir,
):
    """`verify_run_binding` binds the URL the run holds; the decision point
    verifier binds the PR GitHub actually returned for it."""
    from autoforge.github import PRInfo

    gh = FakeGitHub()
    eng = _pending_at_the_source(tmp_state_dir, gh)
    # GitHub answers the run's URL with a different PR.
    gh.prs[PR] = PRInfo(
        url=EARLIER_PR,
        number=41,
        title="PR",
        state="OPEN",
        head_sha=SHA_A,
        head_ref=BRANCH,
        repository="owner/repo",
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert f"source PR read back as {EARLIER_PR}" in eng.state.block_reason
    assert "never reviewed against this decision" in eng.state.block_reason
    assert _txn(eng).stage is ReplanStage.REJECTED and _txn(eng).transaction_id == ""
    assert eng.provider.calls == [] and gh.closed_prs == []


def test_r3f2_the_decision_point_verifier_refuses_a_missing_branch():
    """Never vacuous at the point of use, like the source and target verifiers."""
    from autoforge.replan_txn import verify_decision_point

    txn = _seed_txn(ReplanStage.PENDING, decision_branch="")
    assert "does not record the reviewed branch" in verify_decision_point(_pr(), txn)
    txn.decision_branch = BRANCH
    assert verify_decision_point(_pr(), txn) == ""
    assert "is on branch" in verify_decision_point(_pr(head_ref="other"), txn)


def test_r3f2_review_does_not_decide_a_replan_it_cannot_bind_to_a_branch(tmp_state_dir):
    """A PENDING journal without the reviewed branch is refused on load, so
    REVIEW refuses to write one: it blocks before anything is recorded, with
    the reason in its own words rather than as journal corruption."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    gh.prs[PR].head_ref = ""
    eng.state.current_branch = ""
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "reviewed branch is not recorded in controller state" in eng.state.block_reason
    assert eng.state.replan_transaction == {}
    assert [c.phase for c in eng.provider.calls] == ["REVIEW"]
    assert eng.state.review_round == 20 and len(eng.state.open_findings) == 1
    _assert_source_untouched(eng, gh)


# =============================================================================
# Issue #35, review round 5 (R5-F1): the completeness table covers the review
# evidence a PREPARED journal carries and the attestation a VERIFIED one
# carries, so an omitted field can never be enforced at its dataclass default
# =============================================================================

EVIDENCE_FIELDS = (
    "source_review_round",
    "evidence_finding_count",
    "rendered_findings",
    "rendered_observations",
    "rendered_verification_failures",
    "preexisting_pr_urls",
)
ATTESTATION_FIELDS = ("attested_findings_considered", "attested_unique_constraints")


def test_r5f1_the_reviewers_reproduction_a_zero_claim_is_never_accepted(tmp_state_dir):
    """A PREPARED journal without ``evidence_finding_count`` used to default the
    acknowledgement requirement to 0, so a replacement attesting it considered
    0 findings passed ``verify_attestation`` and the source holding the real
    findings could be closed. The journal is now refused on load, before any
    candidate is read; with the field present the same claim is refused by the
    attestation rule, so the two layers agree."""
    from autoforge.replan_txn import verify_attestation

    zero_claim = ReplanAttestation(
        transaction_id=TXN_ID,
        execution_attempt=2,
        findings_considered=0,
        unique_constraints=0,
        tests_passed=True,
    )
    complete = _seed_txn(ReplanStage.PREPARED)
    assert "considered 0 of the 3 historical finding(s)" in verify_attestation(zero_claim, complete)

    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    gh.prs[REPLACEMENT_PR].body = render_marker(zero_claim)
    del eng.state.replan_transaction["evidence_finding_count"]
    eng.save()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert "evidence_finding_count is required at stage 'prepared' but missing" in reason
    assert "cannot be determined from local state" in reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)
    assert gh.reopened_prs == [] and gh.prs[REPLACEMENT_PR].state == "OPEN"
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert _txn(eng).replacement_pr_url == ""
    # The evidence on disk is left as found, and a resume replays the refusal.
    assert "evidence_finding_count" not in load_state(eng.paths.state_file).replan_transaction
    eng.state.phase = Phase.REPLAN_REEXECUTE
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.block_reason == reason
    assert eng.provider.calls == [] and gh.closed_prs == []


@pytest.mark.parametrize(
    "stage,name",
    [
        *[(ReplanStage.PREPARED, name) for name in EVIDENCE_FIELDS],
        *[(ReplanStage.VERIFIED, name) for name in (*EVIDENCE_FIELDS, *ATTESTATION_FIELDS)],
        *[(ReplanStage.SUPERSEDE_INTENT, name) for name in (*EVIDENCE_FIELDS, *ATTESTATION_FIELDS)],
    ],
    ids=lambda v: v.value if isinstance(v, ReplanStage) else v,
)
def test_r5f1_a_journal_missing_evidence_or_attestation_never_reaches_the_close(
    tmp_state_dir, stage, name
):
    """Recovery from every stage before the close: BLOCKED, no agent run, no
    close, no reopen, no activation, and the journal on disk left as found."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, stage)
    del eng.state.replan_transaction[name]
    eng.save()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert f"{name} is required at stage {stage.value!r} but missing" in reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)
    assert gh.reopened_prs == [] and gh.prs[REPLACEMENT_PR].state == "OPEN"
    assert eng.state.current_branch == BRANCH and eng.state.reviewed_head_sha != SHA_B
    assert name not in load_state(eng.paths.state_file).replan_transaction
    eng.state.phase = Phase.REPLAN_REEXECUTE
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.block_reason == reason and gh.closed_prs == []


@pytest.mark.parametrize("name", [*EVIDENCE_FIELDS, *ATTESTATION_FIELDS])
def test_r5f1_a_superseded_journal_missing_evidence_or_attestation_is_not_activated(
    tmp_state_dir, name
):
    """After the close the drift is terminal: nothing is reopened, nothing activated."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.SUPERSEDED)
    _closed_by_controller(gh)
    del eng.state.replan_transaction[name]
    eng.save()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert f"{name} is required at stage 'superseded' but missing" in eng.state.block_reason
    assert eng.provider.calls == []
    assert eng.state.current_pr_url == PR and eng.state.superseded_prs == []
    assert eng.state.escalation_count == 0
    assert gh.reopened_prs == [] and gh.closed_prs == []
    assert name not in load_state(eng.paths.state_file).replan_transaction


def test_r5f1_an_honest_zero_unique_constraints_is_present_not_empty():
    """0 is a legitimate ``unique_constraints`` (the prompt's own example uses
    it), so the field is required to be *present* at VERIFIED, never non-zero."""
    from autoforge.replan_txn import present_fields_at, required_fields_at

    assert "attested_unique_constraints" not in required_fields_at(ReplanStage.SUPERSEDED)
    assert "attested_unique_constraints" in present_fields_at(ReplanStage.VERIFIED)
    assert "attested_unique_constraints" in present_fields_at(ReplanStage.SUPERSEDED)
    assert present_fields_at(ReplanStage.PREPARED) == ()
    assert present_fields_at(ReplanStage.REJECTED) == ()

    honest = _seed_dict(ReplanStage.VERIFIED)
    honest["attested_unique_constraints"] = 0
    txn = ReplanTransaction.from_dict(honest)
    assert txn.journal_defects == [] and txn.stage is ReplanStage.VERIFIED
    del honest["attested_unique_constraints"]
    txn = ReplanTransaction.from_dict(honest)
    assert txn.stage is ReplanStage.REJECTED
    assert txn.journal_defects == [
        "attested_unique_constraints is required at stage 'verified' but missing"
    ]
    # Before VERIFIED the attestation does not exist yet, so nothing is implied.
    prepared = _seed_dict(ReplanStage.PREPARED)
    del prepared["attested_unique_constraints"]
    del prepared["attested_findings_considered"]
    assert ReplanTransaction.from_dict(prepared).journal_defects == []


def test_r5f1_a_prepared_journal_missing_rendered_findings_never_renders_the_prompt(
    tmp_state_dir,
):
    """Without the rendering the prompt would carry the pre-execution placeholder
    instead of the persisted cross-round findings; the journal is refused
    before the prompt is built, so the agent never sees that prompt."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PREPARED, marker=False)
    del eng.state.replan_transaction["rendered_findings"]
    eng.save()
    assert eng.step().next_phase == "BLOCKED"
    assert "rendered_findings is required at stage 'prepared'" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)


def test_r5f1_the_prepare_step_refuses_evidence_that_collects_to_nothing(tmp_state_dir):
    """The write side of the same rule: a PREPARED journal must carry a non-zero
    evidence count, so the controller never writes one. A history whose
    needs-fix rounds record no finding was not written by a review (the review
    invariant ties needs_fix to findings > 0) and has nothing a replacement
    could be required to acknowledge."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_issue_url = ISSUE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.review_round = 1
    eng.state.review_history = _history([0])  # needs_fix, finding_count 0, complete
    eng.state.replan_transaction = _seed_txn(ReplanStage.PENDING).to_dict()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "records no actionable finding" in eng.state.block_reason
    assert eng.provider.calls == []
    txn = _txn(eng)
    assert txn.stage is ReplanStage.REJECTED and txn.transaction_id == ""
    _assert_source_untouched(eng, gh)


def test_r5f1_the_prepare_step_refuses_a_listing_that_lost_the_source(tmp_state_dir):
    """The snapshot is checkpointed as non-empty (it holds the source, which was
    just read as OPEN); a listing without it was taken after the source moved,
    and is refused rather than written as a journal a resume would call corrupt."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    gh.list_open_prs = lambda repo, limit=100, *, strict=False: []  # type: ignore[method-assign]
    eng.state.phase = Phase.REPLAN_REEXECUTE
    eng.state.current_issue_url = ISSUE
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = SHA_A
    eng.state.review_round = 20
    eng.state.review_history = _history([3] * 20)
    eng.state.replan_transaction = _seed_txn(ReplanStage.PENDING).to_dict()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "missing from the open pull-request listing" in eng.state.block_reason
    assert eng.provider.calls == []
    txn = _txn(eng)
    assert txn.stage is ReplanStage.REJECTED and txn.transaction_id == ""
    _assert_source_untouched(eng, gh)


def test_r5f1_every_journal_the_controller_writes_loads_complete(tmp_state_dir):
    """Round trip through the real lifecycle: every field the table requires at
    a stage is one that stage's writer fills, so a journal the controller
    wrote is never called corrupt by its own resume. Each persisted journal is
    captured as it is saved and reloaded through ``from_dict``."""
    from autoforge.replan_txn import present_fields_at, required_fields_at

    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    saved: list[dict] = []
    real_save = eng._save_replan_txn

    def capture(txn):
        real_save(txn)
        saved.append(dict(eng.state.replan_transaction))

    eng._save_replan_txn = capture  # type: ignore[method-assign]
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    saved.append(dict(eng.state.replan_transaction))  # PENDING, written by REVIEW
    assert eng.step().next_phase == "REVIEW"
    stages = [ReplanTransaction.from_dict(d).stage for d in saved]
    assert stages[0] is ReplanStage.PENDING
    assert {ReplanStage.PREPARED, ReplanStage.VERIFIED, ReplanStage.SUPERSEDED} <= set(stages)
    for data in saved:
        journal = ReplanTransaction.from_dict(data)
        assert journal.journal_defects == [], data
        for name in present_fields_at(journal.stage):
            assert name in data, (journal.stage, name)
        for name in required_fields_at(journal.stage):
            assert data.get(name), (journal.stage, name)
    prepared = next(d for d in saved if d["stage"] == ReplanStage.PREPARED.value)
    assert prepared["evidence_finding_count"] >= 1 and prepared["source_review_round"] == 20
    assert PR in prepared["preexisting_pr_urls"]
    assert "R20-F1" in prepared["rendered_findings"]
    assert prepared["rendered_verification_failures"] == "(none)"


# =============================================================================
# #35 R6-F1: the journal's issue is bound to the run, not only its PRs
# =============================================================================


def test_r6f1_the_reviewers_reproduction_a_cross_issue_journal_never_closes_the_source(
    tmp_state_dir,
):
    """A complete VERIFIED journal for source PR #42 (issue #2) whose
    `issue_url` was substituted for same-repository issue #3, and a marked,
    post-watermark replacement linked only to #3. Before the issue was bound
    to the run this closed #42, activated the replacement, and left
    `current_issue_url` at #2."""
    from tests.conftest import ISSUE3

    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, ["the replan agent must not run"], github=gh)
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    gh.add_pr(
        url=REPLACEMENT_PR,
        head_sha=SHA_B,
        branch=REPLACEMENT_BRANCH,
        linked=[3],
        body=_ours_marker(),
    )
    _seed(eng, ReplanStage.VERIFIED, issue_url=ISSUE3)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    reason = eng.state.block_reason
    assert f"replan issue {ISSUE3} is not the run's current issue {ISSUE}" in reason
    assert eng.provider.calls == []
    assert gh.closed_prs == [] and gh.reopened_prs == []
    assert eng.state.current_issue_url == ISSUE
    _assert_source_untouched(eng, gh)
    assert _txn(eng).stage is ReplanStage.REJECTED
    # The journal stays on disk as it was; the refusal replays identically.
    assert load_state(eng.paths.state_file).replan_transaction["issue_url"] == ISSUE3
    eng.state.phase = Phase.REPLAN_REEXECUTE
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.block_reason == reason
    assert eng.provider.calls == [] and gh.closed_prs == []
    assert eng.state.current_pr_url == PR and eng.state.current_issue_url == ISSUE


@pytest.mark.parametrize(
    "stage",
    [s for s in ReplanStage if s is not ReplanStage.REJECTED],
    ids=lambda v: v.value,
)
def test_r6f1_a_journal_about_another_issue_is_refused_at_every_stage(tmp_state_dir, stage):
    """Not the agent (PENDING, PREPARED), not the close (VERIFIED,
    SUPERSEDE_INTENT), not the reopen (COMPENSATING), not the activation
    (SUPERSEDED)."""
    from tests.conftest import ISSUE3

    gh = FakeGitHub()
    over = {"issue_url": ISSUE3}
    if stage is ReplanStage.PENDING:
        over["source_pr_url"] = ""
    eng, _ = _seeded_engine(tmp_state_dir, gh, stage, **over)
    gh.prs[REPLACEMENT_PR].linked_issue_numbers = [3]
    if stage is ReplanStage.SUPERSEDED:
        _closed_by_controller(gh)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert f"replan issue {ISSUE3} is not the run's current issue {ISSUE}" in (
        eng.state.block_reason
    )
    assert gh.closed_prs == [] and gh.reopened_prs == [] and eng.provider.calls == []
    assert eng.state.current_pr_url == PR and eng.state.current_issue_url == ISSUE
    assert eng.state.superseded_prs == [] and eng.state.escalation_count == 0
    assert _txn(eng).stage is ReplanStage.REJECTED
    assert load_state(eng.paths.state_file).replan_transaction["issue_url"] == ISSUE3


def test_r6f1_the_run_binding_binds_the_issue_by_identity_at_every_stage():
    from autoforge.replan_txn import verify_run_binding
    from tests.conftest import ISSUE3

    for stage in ReplanStage:
        if stage is ReplanStage.REJECTED:
            continue
        over = {"source_pr_url": ""} if stage is ReplanStage.PENDING else {}
        txn = _seed_txn(stage, **over)
        assert verify_run_binding(txn, "owner/repo", PR, ISSUE) == "", stage
        # GitHub owner and repository names are case-insensitive.
        assert (
            verify_run_binding(txn, "owner/repo", PR, "https://github.com/Owner/REPO/issues/2")
            == ""
        ), stage
        assert f"replan issue {ISSUE3} is not the run's current issue" in verify_run_binding(
            _seed_txn(stage, issue_url=ISSUE3, **over), "owner/repo", PR, ISSUE
        ), stage
        assert f"replan issue {ISSUE} is not the run's current issue {ISSUE3}" in (
            verify_run_binding(txn, "owner/repo", PR, ISSUE3)
        ), stage
        assert "is not in other/repo" in verify_run_binding(txn, "other/repo", PR, ISSUE), stage
        assert "does not record the issue whose review decided it" in verify_run_binding(
            _seed_txn(stage, issue_url="", **over), "owner/repo", PR, ISSUE
        ), stage
        assert "replan issue URL is unusable" in verify_run_binding(
            _seed_txn(stage, issue_url=PR, **over), "owner/repo", PR, ISSUE
        ), stage
        assert "current issue URL is unusable" in verify_run_binding(txn, "owner/repo", PR, ""), (
            stage
        )
        assert "current issue URL is unusable" in verify_run_binding(txn, "owner/repo", PR, PR), (
            stage
        )
    # An issue of another repository is refused even when the run's own
    # issue URL carries the same number.
    foreign = "https://github.com/other/repo/issues/2"
    assert f"replan issue {foreign} is not in owner/repo" in verify_run_binding(
        _seed_txn(ReplanStage.VERIFIED, issue_url=foreign), "owner/repo", PR, ISSUE
    )


def test_r6f1_the_target_verifier_refuses_an_issue_of_another_repository():
    """Linkage is a number within one repository; the same rule at the point of use."""
    from autoforge.github import PRInfo
    from autoforge.replan_txn import verify_target_pr

    txn = _seed_txn(ReplanStage.VERIFIED, issue_url="https://github.com/other/repo/issues/2")
    pr = PRInfo(
        url=REPLACEMENT_PR,
        number=43,
        title="PR",
        state="OPEN",
        head_sha=SHA_B,
        base_ref="main",
        head_ref=REPLACEMENT_BRANCH,
        repository="owner/repo",
        linked_issue_numbers=[2],
        body=IMPLEMENTATION_MARKER,
    )
    drift = verify_target_pr(pr, txn, "owner/repo", require_checkpoint_head=True)
    assert "replan issue https://github.com/other/repo/issues/2 is not in owner/repo" in drift
    txn.issue_url = "https://github.com/Owner/REPO/issues/2"
    assert verify_target_pr(pr, txn, "owner/repo", require_checkpoint_head=True) == ""


def test_r6f1_review_records_the_issue_with_the_decision_and_prepare_keeps_it(tmp_state_dir):
    """The issue is recorded at PENDING and never re-derived from
    `current_issue_url` by the prepare step; a case-variant of the run's
    issue is the same issue throughout."""
    from autoforge.validation import parse_issue_url

    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    eng.state.current_issue_url = "https://github.com/Owner/REPO/issues/2"
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    txn = _txn(eng)
    assert txn.stage is ReplanStage.PENDING
    assert parse_issue_url(txn.issue_url).same_target(parse_issue_url(ISSUE))
    recorded = txn.issue_url
    saved: list[dict] = []
    real_save = eng._save_replan_txn

    def capture(journal):
        real_save(journal)
        saved.append(dict(eng.state.replan_transaction))

    eng._save_replan_txn = capture  # type: ignore[method-assign]
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert {d["stage"] for d in saved} >= {"prepared", "verified", "superseded"}
    assert all(d["issue_url"] == recorded for d in saved)


def test_r6f1_a_pending_decision_for_another_issue_never_prepares_the_run_s_issue(
    tmp_state_dir,
):
    """`current_issue_url` is persisted state too: substituted between the
    review and the prepare step, it must not become the issue the
    replacement is required to be linked to."""
    from tests.conftest import ISSUE3

    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert _txn(eng).issue_url == ISSUE
    eng.state.current_issue_url = ISSUE3
    eng._save()
    calls_before = len(eng.provider.calls)
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert f"replan issue {ISSUE} is not the run's current issue {ISSUE3}" in eng.state.block_reason
    assert len(eng.provider.calls) == calls_before
    assert gh.closed_prs == [] and gh.prs[PR].state == "OPEN"
    txn = _txn(eng)
    assert txn.stage is ReplanStage.REJECTED
    assert txn.transaction_id == "" and txn.source_pr_url == ""  # nothing was checkpointed
    eng.state.phase = Phase.REPLAN_REEXECUTE
    assert eng.step().next_phase == "BLOCKED"
    assert len(eng.provider.calls) == calls_before and gh.closed_prs == []


def test_r6f1_a_pending_decision_naming_no_issue_is_corruption(tmp_state_dir):
    gh = FakeGitHub()
    eng = _pending_at_the_source(tmp_state_dir, gh)
    del eng.state.replan_transaction["issue_url"]
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "issue_url is required at stage 'pending' but missing" in eng.state.block_reason
    assert eng.provider.calls == []
    _assert_source_untouched(eng, gh)
    assert "issue_url" not in load_state(eng.paths.state_file).replan_transaction


def test_r6f1_the_agent_s_issue_claim_is_compared_by_identity(tmp_state_dir):
    """The claim check accepts a case-variant of the replan issue and refuses
    another issue, without depending on URL strings."""
    from tests.conftest import ISSUE3

    gh = FakeGitHub()
    agent = _replan_agent(gh, payload_over={"issue_url": "https://github.com/Owner/REPO/issues/2"})
    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.current_pr_url == REPLACEMENT_PR

    gh = FakeGitHub()
    eng = _park_at_hard_threshold(
        tmp_state_dir, gh, _replan_agent(gh, payload_over={"issue_url": ISSUE3})
    )
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "does not match the replan issue" in eng.state.block_reason
    assert gh.closed_prs == [] and gh.prs[PR].state == "OPEN"


# ---------------------------------------------------------------------------
# #66 R7-F1: a journal written by the protocol-1 controller is a *protocol*
# incompatibility, not corruption. It is decided at the state boundary by the
# version label, never by the journal's shape: a protocol-1 state with no
# replan in flight is a protocol-2 state and loads as one; an in-flight
# protocol-1 journal is refused with its stage and PRs named, is never
# migrated by filling the decision from the run's current PR and issue, and
# is never handed to the journal loader to be called corrupt.
# ---------------------------------------------------------------------------

_PROTOCOL_1_STAGES = (
    ReplanStage.PENDING,
    ReplanStage.PREPARED,
    ReplanStage.VERIFIED,
    ReplanStage.SUPERSEDE_INTENT,
    ReplanStage.COMPENSATING,
    ReplanStage.SUPERSEDED,
)


def _protocol_1_journal(stage: ReplanStage) -> dict:
    """Exactly what the protocol-1 controller's ``to_dict`` wrote at ``stage``.

    The key set is the protocol-1 dataclass: every current field except
    ``decision_pr_url``, which did not exist. ``issue_url`` was filled by the
    prepare step, so a PENDING journal carries it *present and empty*. This is
    a literal transcription of that controller's serialisation, so that a
    change to the current schema cannot quietly rewrite what "old" means.
    """
    prepared = stage is not ReplanStage.PENDING
    after_verified = stage in _PROTOCOL_1_STAGES[2:]
    data = {
        "transaction_id": TXN_ID if prepared else "",
        "stage": stage.value,
        "issue_url": ISSUE if prepared else "",
        "decision_head_sha": SHA_A,
        "decision_branch": BRANCH,
        "source_pr_url": PR if prepared else "",
        "source_branch": BRANCH if prepared else "",
        "source_head_sha": SHA_A if prepared else "",
        "source_review_round": 20 if prepared else 0,
        "base_branch": "main" if prepared else "",
        "evidence_finding_count": 3 if prepared else 0,
        "rendered_findings": "- R20-F1 (round 20, blocked): fix it" if prepared else "",
        "rendered_observations": "(none)" if prepared else "",
        "rendered_verification_failures": "(none)" if prepared else "",
        "pr_number_watermark": 42 if prepared else 0,
        "preexisting_pr_urls": [PR] if prepared else [],
        "expected_execution_attempt": 2 if prepared else 0,
        "replacement_pr_url": REPLACEMENT_PR if after_verified else "",
        "replacement_branch": REPLACEMENT_BRANCH if after_verified else "",
        "replacement_head_sha": SHA_B if after_verified else "",
        "attested_findings_considered": 4 if after_verified else 0,
        "attested_unique_constraints": 2 if after_verified else 0,
        "close_intent_at": "2026-01-01T00:00:00+00:00" if stage in _PROTOCOL_1_STAGES[3:] else "",
        "superseded_at": "2026-01-01T00:00:01+00:00" if stage is ReplanStage.SUPERSEDED else "",
        "compensating_at": "2026-01-01T00:00:01+00:00" if stage is ReplanStage.COMPENSATING else "",
        "compensation_reason": (
            "the replan checkpoint no longer held at the close"
            if stage is ReplanStage.COMPENSATING
            else ""
        ),
        "rejection_reason": "",
        "rejected_pr_url": "",
        "escalation": {"trigger": "hard_review_round_threshold"},
    }
    assert "decision_pr_url" not in data
    return data


def _protocol_1_state_file(eng, journal: dict) -> dict:
    """Rewrite the engine's state file as the protocol-1 controller left it."""
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    data["protocol_version"] = "1"
    data["phase"] = Phase.REPLAN_REEXECUTE.value
    data["current_issue_url"] = ISSUE
    data["current_pr_url"] = PR
    data["current_branch"] = BRANCH
    data["current_head_sha"] = SHA_A
    data["replan_transaction"] = journal
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    return data


@pytest.mark.parametrize("stage", _PROTOCOL_1_STAGES, ids=lambda s: s.value)
def test_r7f1_a_protocol_1_journal_is_refused_by_the_journal_loader_only_as_corruption(stage):
    """What the fix rules out: under protocol 2 the journal loader can only
    call the old shape corrupt, so it must never be reached from a protocol-1
    file. (Reached from a protocol-2 file, the same shape *is* corruption.)"""
    txn = ReplanTransaction.from_dict(_protocol_1_journal(stage))
    assert txn.stage is ReplanStage.REJECTED
    assert "persisted replan transaction is corrupt" in txn.rejection_reason
    missing = f"decision_pr_url is required at stage {stage.value!r} but missing"
    assert missing in txn.journal_defects


@pytest.mark.parametrize("stage", _PROTOCOL_1_STAGES, ids=lambda s: s.value)
def test_r7f1_an_in_flight_protocol_1_journal_is_refused_at_the_state_boundary(
    tmp_state_dir, stage
):
    """Every in-flight stage written by the protocol-1 controller, the close
    window included: the state file does not load, nothing is read from or
    written to GitHub, no agent runs, and the file is left byte-for-byte."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, stage)
    eng.save()
    data = _protocol_1_state_file(eng, _protocol_1_journal(stage))
    before = eng.paths.state_file.read_bytes()
    with pytest.raises(StateError) as info:
        eng.load()
    message = str(info.value)
    assert "protocol_version '1'" in message and "replan in flight" in message
    assert f"stage {stage.value!r}" in message
    assert "did not record the PR and issue the replan decision was made on" in message
    assert "does not reconstruct them from the run's current PR and issue" in message
    assert "Finish or undo the replan with the controller that wrote it" in message
    assert "start a new run" in message
    # Never diagnosed as corruption, and never a claim about a close that the
    # journal cannot prove either way.
    assert "corrupt" not in message
    assert "nothing was closed or merged" not in message
    if stage is ReplanStage.PENDING:
        assert "source PR (none)" in message and "no PR was closed" in message
    else:
        assert f"transaction {TXN_ID}" in message and f"source PR {PR}" in message
    if stage in _PROTOCOL_1_STAGES[2:]:
        assert f"replacement PR {REPLACEMENT_PR}" in message
    if stage is ReplanStage.SUPERSEDE_INTENT:
        assert "may have done so" in message
        assert "<!-- autoforge-replan-close: <transaction id> -->" in message
    if stage is ReplanStage.COMPENSATING:
        assert "the source PR may still be CLOSED" in message
    if stage is ReplanStage.SUPERSEDED:
        assert "confirmed the close" in message
    assert eng.paths.state_file.read_bytes() == before
    assert json.loads(before)["replan_transaction"] == data["replan_transaction"]
    assert gh.closed_prs == [] and gh.reopened_prs == [] and gh.commented_prs == []
    assert gh.prs[PR].state == "OPEN" and eng.provider.calls == []


def test_r7f1_a_protocol_1_state_without_a_replan_in_flight_loads_as_protocol_2(tmp_state_dir):
    """The one difference between the protocols is the journal, so a
    protocol-1 file with an empty or terminal journal is a protocol-2 file
    with an old label; the label is rewritten on the next save."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    eng.save()
    rejected = {
        "stage": "rejected",
        "rejection_reason": "the replacement attests tests_passed=false",
    }
    for journal in ({}, rejected):
        data = _protocol_1_state_file(eng, journal)
        data["phase"] = "REVIEW" if not journal else "BLOCKED"
        eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
        loaded = eng.load()
        assert loaded.protocol_version == "2"
        assert loaded.replan_transaction == journal
        eng.save()
        assert json.loads(eng.paths.state_file.read_text())["protocol_version"] == "2"
    # A protocol-1 file that predates the journal field altogether is the
    # same case: no replan in flight.
    data = json.loads(eng.paths.state_file.read_text())
    data["protocol_version"] = "1"
    del data["replan_transaction"]
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    assert eng.load().replan_transaction == {}


def test_r7f1_a_rejected_protocol_1_journal_replays_its_block_under_protocol_2(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    eng.save()
    journal = _protocol_1_journal(ReplanStage.VERIFIED)
    journal["stage"] = "rejected"
    journal["rejection_reason"] = "the replacement attests tests_passed=false"
    _protocol_1_state_file(eng, journal)
    eng.load()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "the replacement attests tests_passed=false" in eng.state.block_reason
    assert "corrupt" not in eng.state.block_reason
    _assert_source_untouched(eng, gh)
    assert eng.provider.calls == []


def test_r7f1_the_version_label_decides_not_the_journal_shape(tmp_state_dir):
    """A protocol-1 journal that happens to carry the protocol-2 fields is
    still refused (an in-flight protocol-1 transaction is one whatever a hand
    edit added), and a protocol-2 journal missing them is corruption, not a
    legacy journal (the label says this controller wrote it)."""
    gh = FakeGitHub()
    eng, txn = _seeded_engine(tmp_state_dir, gh, ReplanStage.SUPERSEDE_INTENT)
    eng.save()
    _protocol_1_state_file(eng, txn.to_dict())
    with pytest.raises(StateError, match="replan in flight"):
        eng.load()
    data = json.loads(eng.paths.state_file.read_text())
    data["protocol_version"] = "2"
    del data["replan_transaction"]["decision_pr_url"]
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    eng.load()
    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert "persisted replan transaction is corrupt" in eng.state.block_reason
    assert "decision_pr_url is required at stage 'supersede_intent' but missing" in (
        eng.state.block_reason
    )
    assert gh.closed_prs == [] and eng.provider.calls == []


def test_r7f1_an_unreadable_stage_in_a_protocol_1_journal_is_still_a_refusal(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.PENDING)
    eng.save()
    journal = _protocol_1_journal(ReplanStage.PENDING)
    journal["stage"] = 7
    _protocol_1_state_file(eng, journal)
    with pytest.raises(StateError) as info:
        eng.load()
    assert "unreadable stage 7" in str(info.value)
    assert "cannot be determined from local state" in str(info.value)
    assert eng.provider.calls == [] and gh.closed_prs == []


# =============================================================================
# #36 follow-up: trigger semantics (F3), budget idempotence (F4), step
# accounting (T2)
# =============================================================================


def test_f3_the_window_rule_deliberately_counts_rounds_of_entirely_new_findings():
    """F3: the soft-threshold window rule is the long-tail trigger.

    ``_history`` gives every round findings nobody asked for before, so the
    ``workflow.stagnation_*`` rules (which need a recurrence) see progress
    here. The window rule still escalates from ``soft_threshold`` on: it is
    scoped to exactly this trickle, and the threshold -- not a recurrence --
    is what keeps an early productive loop out of it.
    """
    config = ReplanConfig()
    history = _history([5] * (config.soft_threshold - 3) + [2, 1, 2])
    assert stagnation_reason(history, 2, 3) == ""  # all-new findings are progress there
    decision = evaluate_replan_policy(
        has_actionable_findings=True,
        current_review_round=config.soft_threshold,
        review_history=history,
        escalation_count=0,
        config=config,
    )
    assert decision.action == "replan" and decision.reason == "stagnation_after_soft_threshold"
    assert decision.metadata is not None
    assert decision.metadata["recent_finding_counts"] == [2, 1, 2]
    # The same tail, every round within `max_findings_per_round`, one round
    # before the threshold: the count gate would pass it, so the threshold
    # alone is what keeps this loop bounded by the round cap only. (Not
    # ``history[:-1]``, whose tail ``[5, 2, 1]`` already fails the count gate.)
    early_history = _history([5] * (config.soft_threshold - 4) + [2, 1, 2])
    assert [r["finding_count"] for r in early_history[-3:]] == [2, 1, 2]
    assert stagnation_reason(early_history, 2, 3) == ""
    early = evaluate_replan_policy(
        has_actionable_findings=True,
        current_review_round=config.soft_threshold - 1,
        review_history=early_history,
        escalation_count=0,
        config=config,
    )
    assert early.action == "continue_fix" and early.metadata is None


def test_f4_activation_counts_the_replan_budget_once_per_transaction(tmp_state_dir):
    """F4: replaying one durable SUPERSEDED transaction moves no counter twice.

    The run binding already refuses the replay at the reducer's entry (W9);
    this drives the activation itself a second time over the same journal,
    so the idempotence does not depend on that refusal.
    """
    gh = FakeGitHub()
    eng, txn = _seeded_engine(
        tmp_state_dir, gh, ReplanStage.SUPERSEDED, superseded_at="2026-01-01T00:00:00+00:00"
    )
    gh.prs[PR].state = "CLOSED"
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.execution_attempt == txn.expected_execution_attempt == 2
    assert eng.state.escalation_count == 1
    assert [item["transaction_id"] for item in eng.state.superseded_prs] == [TXN_ID]

    eng.state.phase = Phase.REPLAN_REEXECUTE
    assert eng._activate_replacement(txn).next_phase == "REVIEW"
    assert eng.state.execution_attempt == 2 and eng.state.escalation_count == 1
    assert [item["transaction_id"] for item in eng.state.superseded_prs] == [TXN_ID]
    assert eng.state.current_pr_url == REPLACEMENT_PR and eng.state.review_round == 0
    persisted = load_state(eng.paths.state_file)
    assert persisted.execution_attempt == 2 and persisted.escalation_count == 1
    assert len(persisted.superseded_prs) == 1


def test_t2_replan_steps_consume_the_cumulative_step_budget(tmp_state_dir):
    """T2: the REVIEW that decides a replan and the replan step each count."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    eng.state.step_count = 7
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.state.step_count == 8
    assert eng.step().next_phase == "REVIEW"
    assert eng.state.step_count == 9
    assert load_state(eng.paths.state_file).step_count == 9


def test_t2_the_step_budget_blocks_replan_before_the_agent_or_the_journal_moves(tmp_state_dir):
    """T2: `max_total_steps` is checked before a replan step does anything."""
    gh = FakeGitHub()
    eng = _park_at_hard_threshold(tmp_state_dir, gh, _replan_agent(gh))
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert _txn(eng).stage is ReplanStage.PENDING
    steps = eng.state.step_count
    eng.config.workflow.max_total_steps = steps
    calls_before = len(eng.provider.calls)

    out = eng.step()
    assert out.next_phase == "BLOCKED"
    assert f"workflow.max_total_steps={steps}" in eng.state.block_reason
    assert "not reset by 'resume'" in eng.state.block_reason
    assert len(eng.provider.calls) == calls_before  # the replan agent never ran
    # Nothing executed: the step did not count and the journal is exactly as
    # the review left it -- PENDING, with no transaction id created.
    persisted = load_state(eng.paths.state_file)
    assert persisted.step_count == steps and persisted.phase == Phase.BLOCKED
    journal = ReplanTransaction.from_dict(persisted.replan_transaction)
    assert journal.stage is ReplanStage.PENDING and journal.transaction_id == ""
    _assert_source_untouched(eng, gh)
    assert eng.state.review_round == 20 and len(eng.state.review_history) == 20


def _replan_agent_that_fails(gh, stdout):
    """REVIEW at the hard threshold, then a replan invocation whose output is ``stdout``."""

    def agent(req):
        if req.phase == "REVIEW":
            gh.add_comment(PR, 120, review_comment_body(20, SHA_A, True, ["R20-F1"]))
            return block(_trigger_review_payload())
        assert req.phase == "REPLAN_REEXECUTE", req.phase
        return stdout

    return agent


@pytest.mark.parametrize(
    "failure",
    ["agent_failure", "malformed_result", "verification_refusal"],
)
def test_t2_a_failed_replan_leaves_the_review_accounting_untouched(tmp_state_dir, failure):
    """T2: a replan that does not complete consumes a step and nothing else.

    Mirrors the REVIEW rule that a failed invocation consumes no review round:
    `review_round`, `review_history`, `escalation_count` and
    `execution_attempt` are exactly what the deciding review left, and the
    source PR is still open.
    """
    gh = FakeGitHub()
    if failure == "agent_failure":
        payload = {"phase": "REPLAN_REEXECUTE", "status": "failure", "message": "tests fail"}
        agent = _replan_agent_that_fails(gh, block(payload))
    elif failure == "malformed_result":
        agent = _replan_agent_that_fails(gh, "no control block at all")
    else:
        agent = _replan_agent(gh, marker_over={"tests_passed": False})
    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    eng.config.execution.max_correction_attempts = 0
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.state.review_round == 20 and len(eng.state.review_history) == 20
    assert eng.state.execution_attempt == 1 and eng.state.escalation_count == 0
    steps = eng.state.step_count

    if failure == "agent_failure":
        out = eng.step()
        assert out.next_phase == "FAILED" and "tests fail" in eng.state.block_reason
    elif failure == "malformed_result":
        with pytest.raises(ControlResultValidationError):
            eng.step()
        assert eng.state.phase == Phase.REPLAN_REEXECUTE  # resumable, not laundered
    else:
        out = eng.step()
        assert out.next_phase == "BLOCKED" and "tests_passed=false" in eng.state.block_reason
        assert _txn(eng).stage is ReplanStage.REJECTED

    persisted = load_state(eng.paths.state_file)
    assert persisted.step_count == steps + 1  # the attempt itself is a step
    assert persisted.review_round == 20 and len(persisted.review_history) == 20
    assert persisted.execution_attempt == 1 and persisted.escalation_count == 0
    assert persisted.superseded_prs == [] and persisted.current_pr_url == PR
    assert gh.prs[PR].state == "OPEN" and gh.closed_prs == []


# =============================================================================
# PR #72 review, R10-F1: checkpoint and listing identity is GitHub identity
#
# The journal stores canonical URLs, which keep the owner/repository spelling
# their author used, while GitHub reads back its own spelling. Every identity
# comparison the transaction makes -- the source and replacement checkpoints
# before and after the close, the source and the snapshot in both candidate
# listings, and the prepare step's listing membership -- is therefore by
# identity (repository case-insensitively, then the number), never by string
# equality of canonical forms. A case-only difference is not drift: it must
# neither refuse the close nor, worse, compensate a correct one.
# =============================================================================

PR_VARIANT = "https://github.com/Owner/REPO/pull/42"  # GitHub's spelling of PR
REPLACEMENT_VARIANT = "https://github.com/Owner/REPO/pull/43"  # ... and of REPLACEMENT_PR
VARIANT_REPO = "Owner/REPO"


def test_r10f1_the_source_verifiers_compare_identity_not_spelling():
    from autoforge.replan_txn import verify_closed_source, verify_source_checkpoint

    txn = _seed_txn(ReplanStage.VERIFIED)
    assert verify_source_checkpoint(_pr(url=PR_VARIANT), txn) == ""
    assert verify_closed_source(_pr(url=PR_VARIANT, state="CLOSED"), txn) == ""
    # Identity is repository *and* number, and never vacuous.
    assert "source PR identity mismatch" in verify_source_checkpoint(_pr(url=EARLIER_PR), txn)
    assert "identity mismatch after the close" in verify_closed_source(
        _pr(url="https://github.com/other/repo/pull/42", state="CLOSED"), txn
    )
    assert "source PR identity mismatch" in verify_source_checkpoint(_pr(url=""), txn)
    txn.source_pr_url = ""
    # Two unusable sides are not "the same": an empty checkpoint matches nothing.
    assert "source PR identity mismatch" in verify_source_checkpoint(_pr(url=""), txn)
    assert "identity mismatch after the close" in verify_closed_source(
        _pr(url="", state="CLOSED"), txn
    )


def test_r10f1_the_target_verifier_compares_identity_not_spelling():
    from dataclasses import replace

    from autoforge.replan_txn import verify_target_pr

    txn = _seed_txn(ReplanStage.VERIFIED)
    gh = FakeGitHub()
    target = gh.add_pr(
        url=REPLACEMENT_VARIANT,
        head_sha=SHA_B,
        branch=REPLACEMENT_BRANCH,
        linked=[2],
        body=IMPLEMENTATION_MARKER,
    )
    assert target.repository == VARIANT_REPO
    assert verify_target_pr(target, txn, "owner/repo", require_checkpoint_head=True) == ""
    # A case variant of the source *is* the source: never its own replacement.
    as_source = replace(target, url=PR_VARIANT, number=42)
    assert "replacement PR must differ from the superseded PR" in verify_target_pr(
        as_source, txn, "owner/repo", require_checkpoint_head=True
    )
    # Another number is another PR, whatever the spelling.
    other = replace(target, url="https://github.com/Owner/REPO/pull/44", number=44)
    assert "replacement PR identity mismatch" in verify_target_pr(
        other, txn, "owner/repo", require_checkpoint_head=True
    )


def _txn_with_a_snapshot(**over) -> ReplanTransaction:
    """PREPARED with a watermark *below* the snapshot's numbers, so that only
    snapshot membership can prove a listed PR pre-existed the transaction."""
    return _seed_txn(
        ReplanStage.PREPARED,
        pr_number_watermark=41,
        preexisting_pr_urls=[PR, "https://github.com/owner/repo/pull/45"],
        **over,
    )


def test_r10f1_candidate_selection_recognises_the_source_and_the_snapshot_by_identity():
    gh = FakeGitHub()
    txn = _txn_with_a_snapshot()
    # The source, listed under GitHub's spelling and carrying this
    # transaction's marker: read *as the source* and refused for it, never
    # treated as a pre-existing PR, never adopted.
    source = gh.add_pr(
        url=PR_VARIANT, head_sha=SHA_A, branch=BRANCH, linked=[2], body=_ours_marker()
    )
    selection = select_bound_candidate([source], txn)
    assert selection.disposition is Disposition.REJECTED
    assert f"source PR {PR_VARIANT} carries the marker" in selection.reason
    # A snapshot member under GitHub's spelling, above the watermark: still
    # pre-existing, by membership -- a copied marker can never be adopted.
    copied = gh.add_pr(
        url="https://github.com/Owner/REPO/pull/45",
        head_sha=SHA_B,
        branch="other",
        linked=[2],
        body=_ours_marker(),
    )
    selection = select_bound_candidate([copied], txn)
    assert selection.disposition is Disposition.REJECTED
    assert "it was in the snapshot of the issue's open PRs" in selection.reason
    # The genuine replacement, under GitHub's spelling, is adopted.
    replacement = gh.add_pr(
        url=REPLACEMENT_VARIANT,
        head_sha=SHA_B,
        branch=REPLACEMENT_BRANCH,
        linked=[2],
        body=_ours_marker(),
    )
    selection = select_bound_candidate([replacement], txn)
    assert selection.disposition is Disposition.OK
    assert selection.pr is replacement


def test_r10f1_the_non_open_listing_recognises_the_source_and_the_snapshot_by_identity():
    gh = FakeGitHub()
    txn = _txn_with_a_snapshot()
    source = gh.add_pr(
        url=PR_VARIANT, head_sha=SHA_A, branch=BRANCH, state="CLOSED", body=_ours_marker()
    )
    listing = find_non_open_claimant([source], txn)
    assert listing.disposition is Disposition.REJECTED
    assert f"source PR {PR_VARIANT} carries the marker" in listing.reason
    copied = gh.add_pr(
        url="https://github.com/Owner/REPO/pull/45",
        head_sha=SHA_B,
        branch="other",
        state="CLOSED",
        body=_ours_marker(),
    )
    listing = find_non_open_claimant([copied], txn)
    assert listing.disposition is Disposition.REJECTED
    assert "it was in the snapshot of the issue's open PRs" in listing.reason
    claimant = gh.add_pr(
        url=REPLACEMENT_VARIANT,
        head_sha=SHA_B,
        branch=REPLACEMENT_BRANCH,
        state="CLOSED",
        body=_ours_marker(),
    )
    listing = find_non_open_claimant([claimant], txn)
    assert listing.disposition is Disposition.REJECTED
    assert f"PR {REPLACEMENT_VARIANT} carries the marker" in listing.reason
    assert "is CLOSED, expected OPEN" in listing.reason


def _github_spelling_differs_from_the_journal(tmp_state_dir, gh, stage, **over):
    """A journal written in the run's spelling over a GitHub that reads back its own."""
    eng = make_engine(tmp_state_dir, ["the replan agent must not run"], github=gh)
    gh.add_pr(url=PR_VARIANT, head_sha=SHA_A, branch=BRANCH, linked=[2])
    gh.add_pr(
        url=REPLACEMENT_VARIANT,
        head_sha=SHA_B,
        branch=REPLACEMENT_BRANCH,
        linked=[2],
        body=_replacement(_ours_marker()),
    )
    txn = _seed(eng, stage, **over)
    assert txn.source_pr_url == PR and txn.replacement_pr_url == REPLACEMENT_PR
    return eng


def test_r10f1_github_s_own_spelling_of_both_checkpoints_is_not_drift(tmp_state_dir):
    """The write path: pre-close verification, the close, and the post-close
    confirmation all read back `Owner/REPO` for a journal that says `owner/repo`.
    Neither side is refused, and the correct close is not compensated."""
    gh = FakeGitHub()
    eng = _github_spelling_differs_from_the_journal(tmp_state_dir, gh, ReplanStage.VERIFIED)
    assert eng.step().next_phase == "REVIEW"
    assert len(gh.closed_prs) == 1 and gh.reopened_prs == []
    assert gh.prs[PR_VARIANT].state == "CLOSED"
    assert eng.state.replan_transaction == {}  # activated, the journal is retired
    assert eng.state.current_pr_url == REPLACEMENT_PR  # the journal's spelling is kept
    assert eng.state.superseded_prs[0]["pr_url"] == PR


@pytest.mark.parametrize(
    "stage,over",
    [
        (ReplanStage.SUPERSEDE_INTENT, {}),
        (ReplanStage.SUPERSEDED, {"superseded_at": "2026-01-01T00:00:01+00:00"}),
    ],
)
def test_r10f1_github_s_own_spelling_is_not_drift_on_resume_either(tmp_state_dir, stage, over):
    """The resume paths that re-derive the checkpoints -- the confirmation a
    crashed write owes, and activation -- apply the same identity rule."""
    gh = FakeGitHub()
    eng = _github_spelling_differs_from_the_journal(tmp_state_dir, gh, stage, **over)
    _closed_by_controller(gh, PR_VARIANT)
    eng2 = _restart(eng, gh)
    assert eng2.step().next_phase == "REVIEW"
    assert gh.closed_prs == [] and gh.reopened_prs == []
    assert eng2.provider.calls == []
    assert eng2.state.current_pr_url == REPLACEMENT_PR
    assert eng2.state.superseded_prs[0]["transaction_id"] == TXN_ID


def _respell_inside_the_close_window(fake) -> None:
    """GitHub starts reporting its own spelling between the last read and the
    close -- what a case-only rename of the repository looks like from here."""
    for old, new in ((PR, PR_VARIANT), (REPLACEMENT_PR, REPLACEMENT_VARIANT)):
        info = fake.prs.pop(old)
        info.url = new
        info.repository = VARIANT_REPO
        fake.prs[new] = info


def test_r10f1_a_case_only_respelling_inside_the_close_window_is_not_compensated(tmp_state_dir):
    """The post-close comparison is the one place a false identity mismatch is
    worse than a refusal: it would *reopen* a PR the controller closed
    correctly. A respelling inside the window is the same PR, so the close
    stands and the run activates."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)
    gh.close_race = _respell_inside_the_close_window
    assert eng.step().next_phase == "REVIEW"
    assert len(gh.closed_prs) == 1 and gh.reopened_prs == []
    assert gh.prs[PR_VARIANT].state == "CLOSED"
    assert eng.state.replan_transaction == {}
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert eng.state.superseded_prs[0]["pr_url"] == PR


def test_r10f1_a_case_only_respelling_still_refuses_real_drift_beside_it(tmp_state_dir):
    """Identity is not the only check the respelled read must pass: a push
    landing in the same window is still drift, and is still compensated."""
    gh = FakeGitHub()
    eng, _ = _seeded_engine(tmp_state_dir, gh, ReplanStage.VERIFIED)

    def respell_and_push(fake):
        _respell_inside_the_close_window(fake)
        fake.prs[PR_VARIANT].head_sha = SHA_C

    gh.close_race = respell_and_push
    assert eng.step().next_phase == "BLOCKED"
    assert "inside the close window" in eng.state.block_reason
    assert "the close was undone" in eng.state.block_reason
    assert len(gh.closed_prs) == 1 and len(gh.reopened_prs) == 1
    assert gh.prs[PR_VARIANT].state == "OPEN"
    assert _txn(eng).stage is ReplanStage.REJECTED


def test_r10f1_the_prepare_step_finds_the_source_in_a_listing_that_spells_it_differently(
    tmp_state_dir,
):
    """`gh pr view` without a `url` field falls back to the requested spelling
    (the run's), while the repository-wide listing reports GitHub's. The
    source is in that listing by identity; a string membership test would
    refuse to checkpoint a source that was just read as OPEN."""
    from dataclasses import replace

    from autoforge.validation import parse_pr_url

    gh = FakeGitHub()
    inner = _replan_agent(gh)
    prepared = {}

    def agent(req):
        if req.phase == "REPLAN_REEXECUTE":
            # The journal as PREPARED persisted it, before the agent ran.
            prepared.update(load_state(eng.paths.state_file).replan_transaction)
        return inner(req)

    eng = _park_at_hard_threshold(tmp_state_dir, gh, agent)
    info = gh.prs.pop(PR)
    info.url, info.repository = PR_VARIANT, VARIANT_REPO
    gh.prs[PR_VARIANT] = info
    original = gh.get_pr
    gh.get_pr = lambda url: replace(original(url), url=parse_pr_url(url).canonical)
    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "REVIEW"
    assert prepared["stage"] == ReplanStage.PREPARED.value
    assert prepared["source_pr_url"] == PR  # the run's spelling, as read back
    assert PR_VARIANT in prepared["preexisting_pr_urls"]  # the listing's spelling
    assert eng.state.current_pr_url == REPLACEMENT_PR
    assert eng.state.superseded_prs[0]["pr_url"] == PR
