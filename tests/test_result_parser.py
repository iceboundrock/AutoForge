"""CONTROL_RESULT parser boundary cases (§13)."""

import json
import re
import time

import pytest

from autoforge import loop_guard
from autoforge.errors import ControlResultError, ControlResultValidationError
from autoforge.prompts import CONTROL_CHAR_RE, escape_inline
from autoforge.redaction import MAX_GROWTH_FACTOR, redact_dict
from autoforge.result_parser import (
    _CLOSING_REFERENCE_RE,
    ACCEPTANCE_TARGET_KEYS,
    AGENT_MARKER_OPEN_RE,
    BEGIN,
    CONTROLLER_MARKER_OPEN_RE,
    END,
    FINDING_ID_RE,
    MAX_ACCEPTANCE_BODY_CHARS,
    MAX_CONTROL_RESULT_CHARS,
    MAX_FINDING_ID_CHARS,
    MAX_FINDING_LOCATION_CHARS,
    MAX_FINDING_RESOLUTION_CHARS,
    MAX_FINDING_TITLE_CHARS,
    MAX_FINDINGS_PER_REVIEW,
    MAX_FIX_RATIONALE_CHARS,
    MAX_FOLLOW_UP_BODY_CHARS,
    MAX_FOLLOW_UP_TITLE_CHARS,
    MAX_OPERATOR_ACTIONS_PER_FIX,
    MAX_OPERATOR_COMMENT_CHARS,
    MAX_OPERATOR_ISSUE_NUMBER,
    MAX_PROGRESS_CHARS,
    MAX_RESOLUTIONS_PER_FIX,
    MAX_REVIEW_SECTION_CHARS,
    MAX_REVIEW_SUMMARY_CHARS,
    MAX_ROADMAP_SECTION_CHARS,
    MAX_TESTS_REPORTED,
    MAX_URL_CHARS,
    MIN_RATIONALE_CHARS,
    OPERATOR_TARGET_KEYS,
    REVIEW_PROSE_SECTIONS,
    AnalyzeExecuteResult,
    Finding,
    FixResult,
    LocalFixResult,
    LocalReviewResult,
    OperatorAction,
    ReplanReexecuteResult,
    ReviewResult,
    UpdateEpicRequest,
    UpdateEpicResult,
    _mention_problem,
    commit_message_problem,
    operator_comment_link_problem,
    operator_markup_problem,
    parse_control_result,
    published_markdown_problem,
    published_payload_problem,
    published_text_problem,
    validate_for_phase,
    validate_next_issue_url,
    validate_operator_comment_body,
    validate_pr_body,
    validate_pr_title,
    validate_progress_text,
    validate_roadmap_section,
)
from autoforge.transitions import Phase, WorkflowMode
from tests.conftest import BRANCH, ISSUE, PR, REVIEW_PROSE, SHA_A, SHA_B, SHA_C, block, comment_url

GOOD_REVIEW = {
    "phase": "REVIEW",
    "status": "success",
    "round": 1,
    "reviewed_head_sha": SHA_A,
    "needs_fix_round": False,
    "findings": [],
    **REVIEW_PROSE,
}


def test_valid_block():
    stdout = "some logs...\n" + BEGIN + "\n" + json.dumps(GOOD_REVIEW) + "\n" + END + "\n"
    assert parse_control_result(stdout, Phase.REVIEW) == GOOD_REVIEW


def test_multiple_blocks_last_wins():
    old = (
        BEGIN
        + "\n"
        + json.dumps(
            {
                "phase": "REVIEW",
                "status": "success",
                "round": 99,
                "reviewed_head_sha": SHA_B,
                "needs_fix_round": True,
                "findings": [{"id": "R99-F1", "classification": "nit", "required_resolution": "x"}],
                **REVIEW_PROSE,
            }
        )
        + "\n"
        + END
    )
    new = BEGIN + "\n" + json.dumps(GOOD_REVIEW) + "\n" + END
    stdout = old + "\nsome logs\n" + new
    assert parse_control_result(stdout, Phase.REVIEW) == GOOD_REVIEW


def test_malformed_json_fails():
    stdout = BEGIN + "\n{not json,\n" + END
    with pytest.raises(ControlResultError, match="[Jj][Ss][Oo][Nn]"):
        parse_control_result(stdout, Phase.REVIEW)


def test_missing_marker_fails():
    with pytest.raises(ControlResultError, match="no CONTROL_RESULT"):
        parse_control_result("just logs, no block\n", Phase.REVIEW)


def test_incomplete_marker_fails():
    with pytest.raises(ControlResultError, match="[Ii]ncomplete"):
        parse_control_result("logs\n" + BEGIN + '\n{"phase": "REVIEW"}\n', Phase.REVIEW)


def test_phase_mismatch_fails():
    stdout = BEGIN + "\n" + json.dumps(GOOD_REVIEW) + "\n" + END
    with pytest.raises(ControlResultValidationError, match="mismatch"):
        parse_control_result(stdout, Phase.FIX)


def test_missing_required_field_fails():
    bad = {"phase": "REVIEW", "status": "success", "round": 1}  # missing shas/flag
    stdout = BEGIN + "\n" + json.dumps(bad) + "\n" + END
    with pytest.raises(ControlResultValidationError, match="missing required"):
        parse_control_result(stdout, Phase.REVIEW)


def test_missing_phase_status_fails():
    stdout = BEGIN + '\n{"round": 1}\n' + END
    with pytest.raises(ControlResultValidationError, match="required field"):
        parse_control_result(stdout, Phase.REVIEW)


def test_non_object_fails():
    stdout = BEGIN + '\n["a", "b"]\n' + END
    with pytest.raises(ControlResultValidationError, match="JSON object"):
        parse_control_result(stdout, Phase.REVIEW)


def _finding(rnd, n, cls="nit"):
    return {"id": f"R{rnd}-F{n}", "classification": cls, "required_resolution": "fix"}


def test_per_phase_schemas():
    ae = {
        "phase": "ANALYZE_EXECUTE",
        "status": "success",
        "issue_url": ISSUE,
        "head_sha": SHA_A.upper(),
        "pr_title": "Add the transaction filter",
        "pr_body": "Adds `--since`.\n\nTested with `pytest`.",
        "tests": ["pytest: 12 passed"],
    }
    assert parse_control_result(BEGIN + json.dumps(ae) + END, Phase.ANALYZE_EXECUTE) == ae
    parsed = AnalyzeExecuteResult.from_payload(ae)
    assert parsed.head_sha == SHA_A  # normalized to lowercase
    assert parsed.pr_title == ae["pr_title"] and parsed.pr_body == ae["pr_body"]
    assert parsed.tests == ["pytest: 12 passed"]
    for missing in ("head_sha", "pr_title", "pr_body"):
        bad = dict(ae)
        del bad[missing]
        with pytest.raises(ControlResultValidationError, match=missing):
            parse_control_result(BEGIN + json.dumps(bad) + END, Phase.ANALYZE_EXECUTE)
    without_tests = {k: v for k, v in ae.items() if k != "tests"}
    assert AnalyzeExecuteResult.from_payload(without_tests).tests == []
    with pytest.raises(ControlResultValidationError, match="head_sha"):
        parse_control_result(
            BEGIN + json.dumps(dict(ae, head_sha="h")) + END, Phase.ANALYZE_EXECUTE
        )

    # MERGE is controller-executed: an agent "merged" claim is never accepted.
    merge = {"phase": "MERGE", "status": "success", "merged": True, "next_action": "UPDATE_EPIC"}
    with pytest.raises(ControlResultValidationError, match="executed by the controller"):
        parse_control_result(BEGIN + json.dumps(merge) + END, Phase.MERGE)

    epic = {
        "phase": "UPDATE_EPIC",
        "status": "success",
        "next_issue_url": None,
        "progress": "Issue #2 merged.",
    }
    assert parse_control_result(BEGIN + json.dumps(epic) + END, Phase.UPDATE_EPIC) == epic
    epic["roadmap_section"] = "- [x] #1"
    assert parse_control_result(BEGIN + json.dumps(epic) + END, Phase.UPDATE_EPIC) == epic


_ANALYZE = {
    "issue_url": ISSUE,
    "head_sha": SHA_A,
    "pr_title": "Add the transaction filter",
    "pr_body": "Adds `--since`.",
}


@pytest.mark.parametrize(
    ("key", "value", "rule"),
    [
        ("pr_title", "Add\nthe filter", "control character"),
        ("pr_title", "x" * 257, "at most 256"),
        ("pr_body", "x" * 60001, "at most 60000"),
        ("pr_body", 'Done.\n<!-- ai-implementation: {"issue": "x"} -->', "controller marker"),
        ("pr_body", "Done.\n\nCloses #2", "closing keyword"),
        ("pr_title", "Fixes owner/repo#3", "closing keyword"),
        ("pr_body", "Thanks @octocat", "@-mention"),
        ("pr_body", "Set GH_TOKEN=FAKEtoken123456 first", "credential-shaped"),
        ("tests", ["pytest\n12 passed"], "control character"),
        ("tests", ["pytest"] * 51, "at most 50"),
        ("tests", "pytest", "tests"),
    ],
)
def test_analyze_result_text_is_bounded_and_held_to_the_published_policy(key, value, rule):
    """#161: the controller publishes ``pr_title`` and ``pr_body`` as given, so the
    result is refused (and the agent asked again) rather than the text clipped or
    rewritten; ``Closes #n`` and the marker are the controller's to add."""
    with pytest.raises(ControlResultValidationError, match=rule) as info:
        AnalyzeExecuteResult.from_payload(dict(_ANALYZE, **{key: value}))
    assert "octocat" not in str(info.value) and "FAKEtoken" not in str(info.value)


@pytest.mark.parametrize(
    ("validator", "key", "bad"),
    [
        (validate_pr_title, "pr_title", "Notify @octocat"),
        (validate_pr_title, "pr_title", "Fixes #3"),
        (validate_pr_title, "pr_title", "<!-- ai-x -->"),
        (validate_pr_title, "pr_title", "a\x1bb"),
        (validate_pr_title, "pr_title", "x" * 257),
        (validate_pr_title, "pr_title", "  "),
        (validate_pr_body, "pr_body", "Closes #3\nDone."),
        (validate_pr_body, "pr_body", "Thanks @octocat"),
        (validate_pr_body, "pr_body", "Done.\n<!-- autoforge-x -->"),
        (validate_pr_body, "pr_body", "a\x0bb"),
        (validate_pr_body, "pr_body", "x" * 60001),
        (validate_pr_body, "pr_body", "\n\t"),
    ],
)
def test_pr_text_validators_apply_the_parsers_rules_with_its_messages(validator, key, bad):
    """#161: a persisted K2 title or body text is re-validated under the parser's rules."""
    with pytest.raises(ControlResultValidationError) as from_validator:
        validator(bad)
    with pytest.raises(ControlResultValidationError) as from_payload:
        AnalyzeExecuteResult.from_payload(dict(_ANALYZE, **{key: bad}))
    assert str(from_validator.value) == str(from_payload.value)


def test_pr_text_validators_return_accepted_text():
    assert validate_pr_title(_ANALYZE["pr_title"]) == _ANALYZE["pr_title"]
    assert validate_pr_body(_ANALYZE["pr_body"]) == _ANALYZE["pr_body"]
    assert validate_pr_body("x" * 60000) == "x" * 60000


def test_analyze_result_names_no_target():
    """#161: a PR URL or branch an agent still reports is not read; the controller
    chooses the branch and opens the PR itself."""
    parsed = AnalyzeExecuteResult.from_payload(dict(_ANALYZE, pr_url=PR, branch="feature/x"))
    assert not hasattr(parsed, "pr_url") and not hasattr(parsed, "branch")


def _replan(**changes: object) -> dict:
    payload = {
        "phase": "REPLAN_REEXECUTE",
        "status": "success",
        "previous_head_sha": SHA_A,
        "head_sha": SHA_B,
        **_REPLAN_REST,
    }
    payload.update(changes)
    return payload


def test_replan_result_carries_the_commit_and_the_pr_text():
    """#164: the agent reports the commit it made and the replacement PR's text;
    the controller derives the branch, pushes and creates the PR."""
    parsed = ReplanReexecuteResult.from_payload(_replan(head_sha=SHA_B.upper()))
    assert parsed.head_sha == SHA_B
    assert (parsed.pr_title, parsed.pr_body) == ("Reimplement it", "A fresh reimplementation.")
    for missing in ("head_sha", "pr_title", "pr_body"):
        bad = _replan()
        del bad[missing]
        with pytest.raises(ControlResultValidationError, match=missing):
            parse_control_result(block(bad), Phase.REPLAN_REEXECUTE)


def test_replan_result_names_no_replacement_target():
    """#164: a replacement PR, branch or head an agent still reports is not read."""
    parsed = ReplanReexecuteResult.from_payload(
        _replan(
            replacement_pr_url="https://github.com/owner/repo/pull/8",
            replacement_branch=BRANCH + "-r2",
            replacement_head_sha=SHA_C,
        )
    )
    for name in ("replacement_pr_url", "replacement_branch", "replacement_head_sha"):
        assert not hasattr(parsed, name)


@pytest.mark.parametrize(
    ("key", "value", "rule"),
    [
        ("pr_title", "Reimplement\nit", "control character"),
        ("pr_title", "x" * 257, "at most 256"),
        ("pr_body", "x" * 60001, "at most 60000"),
        ("pr_body", 'Done.\n<!-- autoforge-replan-transaction: {"x": 1} -->', "controller marker"),
        ("pr_body", "Done.\n\nCloses #2", "closing keyword"),
        ("pr_title", "Resolves GH-4", "closing keyword"),
        ("pr_body", "Thanks @octocat", "@-mention"),
        ("pr_body", "Set GH_TOKEN=FAKEtoken123456 first", "credential-shaped"),
    ],
)
def test_replan_pr_text_is_held_to_the_published_policy(key, value, rule):
    """#164: the controller publishes the replacement PR's text as given, so the
    result is refused rather than the text rewritten, under the phase's name."""
    with pytest.raises(ControlResultValidationError, match=rule) as info:
        ReplanReexecuteResult.from_payload(_replan(**{key: value}))
    assert str(info.value).startswith("REPLAN_REEXECUTE")
    assert "octocat" not in str(info.value) and "FAKEtoken" not in str(info.value)


def test_failure_status_skips_schema_but_needs_message():
    bad = {"phase": "REVIEW", "status": "failure"}
    with pytest.raises(ControlResultValidationError, match="message"):
        parse_control_result(BEGIN + json.dumps(bad) + END, Phase.REVIEW)
    ok = {"phase": "REVIEW", "status": "blocked", "message": "cannot"}
    assert parse_control_result(BEGIN + json.dumps(ok) + END, Phase.REVIEW)["status"] == "blocked"
    with pytest.raises(ControlResultValidationError, match="status"):
        parse_control_result(BEGIN + json.dumps(dict(ok, status="meh")) + END, Phase.REVIEW)


def test_review_findings_invariant_and_ids():
    base = dict(GOOD_REVIEW)
    # findings present but needs_fix_round False
    p = dict(base, findings=[_finding(1, 1)])
    with pytest.raises(ControlResultValidationError, match="needs_fix_round"):
        ReviewResult.from_payload(p)
    # needs_fix_round True with no findings
    with pytest.raises(ControlResultValidationError, match="needs_fix_round"):
        ReviewResult.from_payload(dict(base, needs_fix_round=True))
    # bad id shape / wrong round in id / duplicate / bad classification
    for bad in (
        {"id": "F1", "classification": "nit", "required_resolution": "x"},
        {"id": "R2-F1", "classification": "nit", "required_resolution": "x"},
        {"id": "R1-F1", "classification": "urgent", "required_resolution": "x"},
        {"id": "R1-F1", "classification": "nit"},
        # whitespace-only is as absent as "" (R2-F2): a blank demand asks for
        # nothing, and normalising it to "" would make two such rounds look
        # like the same required resolution coming back.
        {"id": "R1-F1", "classification": "nit", "required_resolution": "   "},
        {"id": "R1-F1", "classification": "nit", "required_resolution": "\n\t "},
        {"id": "  ", "classification": "nit", "required_resolution": "x"},
    ):
        with pytest.raises(ControlResultValidationError):
            ReviewResult.from_payload(dict(base, needs_fix_round=True, findings=[bad]))
    with pytest.raises(ControlResultValidationError, match="duplicate"):
        ReviewResult.from_payload(
            dict(base, needs_fix_round=True, findings=[_finding(1, 1), _finding(1, 1)])
        )
    good = ReviewResult.from_payload(
        dict(base, needs_fix_round=True, findings=[_finding(1, 1), _finding(1, 2, "blocked")])
    )
    assert [f.id for f in good.findings] == ["R1-F1", "R1-F2"]
    assert isinstance(good.findings[0], Finding) and good.needs_fix_round
    with pytest.raises(ControlResultValidationError, match="round"):
        ReviewResult.from_payload(dict(base, round=0))


def test_fix_resolution_rules():
    base = {"phase": "FIX", "status": "success", "previous_head_sha": SHA_A, "head_sha": SHA_B}
    with pytest.raises(ControlResultValidationError, match="resolutions"):
        FixResult.from_payload(base)
    ok = FixResult.from_payload(
        dict(base, resolutions=[{"finding_id": "R1-F1", "resolution": "fixed"}])
    )
    assert ok.resolutions[0].resolution == "fixed" and ok.head_sha == SHA_B
    with pytest.raises(ControlResultValidationError, match="rationale"):
        FixResult.from_payload(
            dict(
                base,
                resolutions=[{"finding_id": "R1-F1", "resolution": "no_change_with_rationale"}],
            )
        )
    with pytest.raises(ControlResultValidationError, match="rationale"):
        FixResult.from_payload(
            dict(
                base,
                resolutions=[
                    {
                        "finding_id": "R1-F1",
                        "resolution": "no_change_with_rationale",
                        "rationale": "short",
                    }
                ],
            )
        )
    with pytest.raises(ControlResultValidationError, match="follow_up_issue_url"):
        FixResult.from_payload(
            dict(base, resolutions=[{"finding_id": "R1-F1", "resolution": "follow_up_created"}])
        )
    with pytest.raises(ControlResultValidationError, match="carries a follow-up issue"):
        FixResult.from_payload(
            dict(
                base,
                resolutions=[
                    {"finding_id": "R1-F1", "resolution": "fixed", "follow_up_issue_url": ISSUE}
                ],
            )
        )
    with pytest.raises(ControlResultValidationError, match="resolution"):
        FixResult.from_payload(
            dict(base, resolutions=[{"finding_id": "R1-F1", "resolution": "wontfix"}])
        )
    with pytest.raises(ControlResultValidationError, match="duplicate"):
        FixResult.from_payload(
            dict(base, resolutions=[{"finding_id": "R1-F1", "resolution": "fixed"}] * 2)
        )


NEW_FOLLOW_UP = {"title": "Cache the parsed config", "body": "The config is parsed twice."}


def test_fix_follow_up_is_a_handed_issue_or_a_new_one_never_both():
    """#163: a deferral names an issue the controller handed over, or asks for a
    new one by title and body; it is never both or neither, and no other
    resolution carries either. Only a fixed finding names a commit."""
    base = {"phase": "FIX", "status": "success", "previous_head_sha": SHA_A, "head_sha": SHA_B}

    def one(**resolution):
        return FixResult.from_payload(
            dict(base, resolutions=[{"finding_id": "R1-F1", **resolution}])
        ).resolutions[0]

    new = one(resolution="follow_up_created", follow_up_issue=NEW_FOLLOW_UP)
    assert new.new_follow_up and new.follow_up_issue_url == ""
    assert (new.follow_up_title, new.follow_up_body) == (
        NEW_FOLLOW_UP["title"],
        NEW_FOLLOW_UP["body"],
    )
    assert "follow_up_title" not in new.to_dict() and "follow_up_body" not in new.to_dict()
    handed = one(resolution="follow_up_created", follow_up_issue_url=ISSUE)
    assert not handed.new_follow_up and handed.follow_up_issue_url == ISSUE
    assert one(resolution="fixed", commit_sha=SHA_B).commit_sha == SHA_B
    with pytest.raises(ControlResultValidationError, match="exactly one of"):
        one(
            resolution="follow_up_created",
            follow_up_issue_url=ISSUE,
            follow_up_issue=NEW_FOLLOW_UP,
        )
    with pytest.raises(ControlResultValidationError, match="carries a follow-up issue"):
        one(
            resolution="no_change_with_rationale",
            rationale="x" * MIN_RATIONALE_CHARS,
            follow_up_issue=NEW_FOLLOW_UP,
        )
    with pytest.raises(ControlResultValidationError, match="only a fixed finding"):
        one(resolution="follow_up_created", follow_up_issue_url=ISSUE, commit_sha=SHA_B)
    with pytest.raises(ControlResultValidationError, match="must be an object"):
        one(resolution="follow_up_created", follow_up_issue="Cache the parsed config")


@pytest.mark.parametrize(
    ("issue", "match"),
    [
        pytest.param({"body": "b"}, "'title' must be a string", id="no-title"),
        pytest.param({"title": "t"}, "'body' must be a string", id="no-body"),
        pytest.param({"title": "  ", "body": "b"}, "missing required field 'title'", id="blank"),
        pytest.param({"title": "a\nb", "body": "b"}, "title", id="two-line-title"),
        pytest.param(
            {"title": "t" * (MAX_FOLLOW_UP_TITLE_CHARS + 1), "body": "b"},
            f"at most {MAX_FOLLOW_UP_TITLE_CHARS}",
            id="long-title",
        ),
        pytest.param(
            {"title": "t", "body": "b" * (MAX_FOLLOW_UP_BODY_CHARS + 1)},
            f"at most {MAX_FOLLOW_UP_BODY_CHARS}",
            id="long-body",
        ),
        pytest.param({"title": "Closes #12", "body": "b"}, "closing", id="closing-title"),
        pytest.param({"title": "t", "body": "Fixes #3 too"}, "closing", id="closing-body"),
        pytest.param(
            {"title": "t", "body": "<!-- ai-follow-up: x -->"}, "marker", id="marker-body"
        ),
        pytest.param({"title": "t", "body": "ask @octocat"}, "mention", id="mention"),
        pytest.param({"title": 3, "body": "b"}, "must be a string", id="not-a-string"),
    ],
)
def test_fix_new_follow_up_text_is_bounded_and_publishable(issue, match):
    """#163: the controller publishes a new follow-up's title and body as given,
    so each is rejected (never clipped or redacted) when it is blank, too long,
    or would act on GitHub: a closing keyword, a controller marker, a mention."""
    payload = {
        "phase": "FIX",
        "status": "success",
        "previous_head_sha": SHA_A,
        "head_sha": SHA_B,
        "resolutions": [
            {"finding_id": "R1-F1", "resolution": "follow_up_created", "follow_up_issue": issue}
        ],
    }
    with pytest.raises(ControlResultValidationError, match=match):
        FixResult.from_payload(payload)


ACCEPTANCE_RATIONALE = "The finding asks for evidence on the issue, not for a code change."


def _acceptance(action, rationale: str = ACCEPTANCE_RATIONALE, **extra) -> dict:
    payload = {
        "phase": "FIX",
        "status": "success",
        "previous_head_sha": SHA_A,
        "head_sha": SHA_B,
        "resolutions": [
            {
                "finding_id": "R1-F1",
                "resolution": "acceptance_action",
                "rationale": rationale,
                "acceptance_action": action,
                **extra,
            }
        ],
    }
    return payload


def test_fix_acceptance_action_carries_a_kind_and_a_body_only():
    """#211: the resolution's object is the closed kind and the agent's text;
    the parsed resolution keeps the text for the controller and its
    persisted dict is the other resolutions' shape."""
    text = "Ran the operator check on the reviewed HEAD.\n\nEvery check passed."
    res = FixResult.from_payload(_acceptance({"kind": "issue_comment", "body": text}))
    [r] = res.resolutions
    assert r.acceptance_action and r.acceptance_body == text
    assert r.rationale == ACCEPTANCE_RATIONALE
    assert r.to_dict() == {
        "finding_id": "R1-F1",
        "resolution": "acceptance_action",
        "rationale": ACCEPTANCE_RATIONALE,
        "follow_up_issue_url": "",
        "commit_sha": "",
    }


@pytest.mark.parametrize("key", ACCEPTANCE_TARGET_KEYS)
def test_fix_acceptance_action_refuses_any_target_by_name(key):
    """No URL, issue, repository, method, path or command: the controller
    derives the issue from its state, so a target is refused, not ignored."""
    action = {"kind": "issue_comment", "body": "evidence", key: ISSUE}
    with pytest.raises(ControlResultValidationError, match=f"carries '{key}'.*names no target"):
        FixResult.from_payload(_acceptance(action))


@pytest.mark.parametrize(
    ("action", "match"),
    [
        pytest.param({"kind": "issue_comment"}, "'body' must be a string", id="no-body"),
        pytest.param({"kind": "issue_comment", "body": "  "}, "missing required", id="blank"),
        pytest.param({"body": "b"}, "kind NoneType is not supported", id="no-kind"),
        pytest.param({"kind": "pr_comment", "body": "b"}, "'pr_comment' is not", id="kind"),
        pytest.param({"kind": "issue_comment", "body": "b", "x": 1}, "unknown field", id="extra"),
        pytest.param("post a comment", "must be an object", id="not-an-object"),
        pytest.param(
            {"kind": "issue_comment", "body": "b" * (MAX_ACCEPTANCE_BODY_CHARS + 1)},
            f"at most {MAX_ACCEPTANCE_BODY_CHARS}",
            id="long",
        ),
        pytest.param({"kind": "issue_comment", "body": "Closes #12"}, "closing", id="closing"),
        pytest.param(
            {"kind": "issue_comment", "body": "<!-- ai-acceptance: x -->"}, "marker", id="marker"
        ),
        pytest.param({"kind": "issue_comment", "body": "ask @octocat"}, "mention", id="mention"),
        pytest.param(
            {"kind": "issue_comment", "body": "ok\x07"}, "control character", id="control"
        ),
    ],
)
def test_fix_acceptance_action_text_is_bounded_and_publishable(action, match):
    """The text is published as given inside the controller's comment, so it
    is rejected (never clipped or redacted) when it is missing, blank, too
    long, or would act on GitHub."""
    with pytest.raises(ControlResultValidationError, match=match):
        FixResult.from_payload(_acceptance(action))


def test_fix_acceptance_action_needs_a_rationale_and_nothing_else():
    """Like a no-change resolution it explains itself; it never names a
    commit or a follow-up, and no other resolution carries the object."""
    action = {"kind": "issue_comment", "body": "evidence"}
    with pytest.raises(ControlResultValidationError, match="rationale is missing"):
        FixResult.from_payload(_acceptance(action, rationale="short"))
    with pytest.raises(ControlResultValidationError, match="must carry an 'acceptance_action'"):
        FixResult.from_payload(_acceptance(None))
    with pytest.raises(ControlResultValidationError, match="only a fixed finding"):
        FixResult.from_payload(_acceptance(action, commit_sha=SHA_B))
    with pytest.raises(ControlResultValidationError, match="carries a follow-up issue"):
        FixResult.from_payload(_acceptance(action, follow_up_issue_url=ISSUE))
    other = _acceptance(action)
    other["resolutions"][0]["resolution"] = "no_change_with_rationale"
    with pytest.raises(ControlResultValidationError, match="carries an acceptance_action"):
        FixResult.from_payload(other)


def test_fix_result_carries_at_most_one_acceptance_action():
    payload = _acceptance({"kind": "issue_comment", "body": "evidence"})
    second = dict(payload["resolutions"][0], finding_id="R1-F2")
    payload["resolutions"].append(second)
    with pytest.raises(ControlResultValidationError, match="at most 1 per FIX result"):
        FixResult.from_payload(payload)


def test_local_fix_refuses_an_acceptance_action():
    """LOCAL runs perform no external writes: the resolution is not offered."""
    text, mode = _fix_with(
        [
            {
                "finding_id": "R1-F1",
                "resolution": "acceptance_action",
                "rationale": ACCEPTANCE_RATIONALE,
                "acceptance_action": {"kind": "issue_comment", "body": "evidence"},
            }
        ],
        "LOCAL",
    )
    with pytest.raises(ControlResultValidationError, match="LOCAL runs perform no external"):
        parse_control_result(text, Phase.FIX, mode)


# -- operator_acceptance (#243): comments the operator posts on other issues ----------------
OPERATOR_RATIONALE = "The finding asks for a note on the issues this change affects, not code."
OPERATOR_NOTE = "This change alters the retry budget.\n\nThe new default is `3`, see the PR."


def _op(issue: object = 16, body: object = OPERATOR_NOTE, **extra) -> dict:
    """One ``related_issue_comment`` action of an ``operator_acceptance`` object."""
    return {"kind": "related_issue_comment", "issue": issue, "body": body, **extra}


def _operator(
    operator: object, rationale: str = OPERATOR_RATIONALE, fid: str = "R1-F1", **extra
) -> dict:
    """A FIX payload whose one resolution is ``operator_acceptance`` carrying ``operator``."""
    return dict(
        _FIX_TOP,
        resolutions=[
            {
                "finding_id": fid,
                "resolution": "operator_acceptance",
                "rationale": rationale,
                "operator_acceptance": operator,
                **extra,
            }
        ],
    )


def test_operator_acceptance_bounds_are_the_documented_ones():
    """1 to 4 comments per FIX result, each at most 16000 characters (#243)."""
    assert MAX_OPERATOR_ACTIONS_PER_FIX == 4
    assert MAX_OPERATOR_COMMENT_CHARS == 16000
    assert OPERATOR_TARGET_KEYS == (
        "url",
        "repository",
        "owner",
        "repo",
        "target",
        "method",
        "path",
        "command",
    )


@pytest.mark.parametrize("count", [1, MAX_OPERATOR_ACTIONS_PER_FIX])
def test_fix_operator_acceptance_carries_numbered_comments_on_other_issues(count):
    """#243: each action is the closed kind, an issue *number* and the agent's
    text; the parsed resolution keeps them in order with the rationale, and
    its persisted dict is the other resolutions' shape."""
    actions = [_op(16 + n, f"{OPERATOR_NOTE}\n\nNote {n}.") for n in range(count)]
    [r] = FixResult.from_payload(_operator({"actions": actions})).resolutions
    assert r.operator_acceptance and not r.acceptance_action and not r.new_follow_up
    assert r.operator_actions == tuple(
        OperatorAction(issue=16 + n, body=f"{OPERATOR_NOTE}\n\nNote {n}.") for n in range(count)
    )
    assert all(isinstance(a.issue, int) for a in r.operator_actions)
    assert r.rationale == OPERATOR_RATIONALE and r.acceptance_body == ""
    assert r.to_dict() == {
        "finding_id": "R1-F1",
        "resolution": "operator_acceptance",
        "rationale": OPERATOR_RATIONALE,
        "follow_up_issue_url": "",
        "commit_sha": "",
    }


def test_fix_operator_acceptance_issue_and_body_bounds_are_inclusive():
    """Issue 1 and the largest number the controller reads are targets; a body
    of exactly the bound is kept, stripped like every published text."""
    exact = "b" * MAX_OPERATOR_COMMENT_CHARS
    payload = _operator(
        {"actions": [_op(1, f"\n  {exact}  \n"), _op(MAX_OPERATOR_ISSUE_NUMBER, "edge")]}
    )
    [r] = FixResult.from_payload(payload).resolutions
    assert r.operator_actions == (
        OperatorAction(issue=1, body=exact),
        OperatorAction(issue=MAX_OPERATOR_ISSUE_NUMBER, body="edge"),
    )


def test_other_resolutions_carry_no_operator_actions():
    [r] = _remote_fix_result().resolutions
    assert not r.operator_acceptance and r.operator_actions == ()


@pytest.mark.parametrize("key", OPERATOR_TARGET_KEYS)
@pytest.mark.parametrize("where", ["action", "object"])
def test_fix_operator_acceptance_refuses_any_target_by_name(key, where):
    """No URL, repository, owner, target, method, path or command: an action
    names an issue number the controller checks against the run's
    repository, so a target-like key is refused by name, never ignored."""
    if where == "action":
        operator = {"actions": [_op(**{key: "https://github.com/o/x/issues/16"})]}
    else:
        operator = {"actions": [_op()], key: "https://github.com/o/x/issues/16"}
    msg = _refusal(lambda: FixResult.from_payload(_operator(operator)))
    what = "operator_acceptance actions[0]" if where == "action" else "operator_acceptance"
    assert f"R1-F1 {what} carries '{key}'; an operator action names no URL" in msg
    assert "names an issue number of the run's repository" in msg
    assert "o/x" not in msg  # the value is never quoted


def test_fix_operator_acceptance_names_every_target_key_it_refuses():
    action = _op(url="u", command="c", owner="o")
    msg = _refusal(lambda: FixResult.from_payload(_operator({"actions": [action]})))
    assert "carries 'command', 'owner', 'url';" in msg


@pytest.mark.parametrize(
    ("issue", "match"),
    [
        pytest.param("16", "field 'issue' is a string; it is a JSON integer", id="string"),
        pytest.param("#16", "field 'issue' is a string; it is a JSON integer", id="hash"),
        pytest.param(
            "https://github.com/o/r/issues/16",
            r"is a string; it is a JSON integer, the issue number alone \(never a URL",
            id="url",
        ),
        pytest.param("o/r#16", "field 'issue' is a string", id="owner-repo"),
        pytest.param(True, "field 'issue' is bool", id="bool"),
        pytest.param(16.0, "field 'issue' is float", id="float"),
        pytest.param(None, "field 'issue' is NoneType", id="null"),
        pytest.param([16], "field 'issue' is list", id="list"),
        pytest.param(0, f"must be between 1 and {MAX_OPERATOR_ISSUE_NUMBER}", id="zero"),
        pytest.param(-16, f"must be between 1 and {MAX_OPERATOR_ISSUE_NUMBER}", id="negative"),
        pytest.param(
            MAX_OPERATOR_ISSUE_NUMBER + 1,
            f"must be between 1 and {MAX_OPERATOR_ISSUE_NUMBER}",
            id="too-large",
        ),
    ],
)
def test_fix_operator_acceptance_issue_is_a_number_of_the_runs_repository(issue, match):
    """The target is the issue number alone, a JSON integer the controller
    resolves in the run's repository: never a string, '#n', a URL or a bool,
    and within the range the controller's issue references read."""
    msg = _refusal(lambda: FixResult.from_payload(_operator({"actions": [_op(issue)]})))
    assert "R1-F1 operator_acceptance actions[0] field 'issue'" in msg
    assert re.search(match, msg), msg
    assert "github.com/o/r" not in msg


def test_fix_operator_acceptance_issue_is_required():
    action = {"kind": "related_issue_comment", "body": OPERATOR_NOTE}
    msg = _refusal(lambda: FixResult.from_payload(_operator({"actions": [action]})))
    assert "field 'issue' is NoneType" in msg


@pytest.mark.parametrize(
    ("operator", "match"),
    [
        pytest.param("post comments", "must be an object with 'actions'", id="not-an-object"),
        pytest.param([_op()], "must be an object with 'actions'", id="a-bare-list"),
        pytest.param({}, "must carry a non-empty 'actions' array", id="no-actions"),
        pytest.param({"actions": []}, "must carry a non-empty 'actions' array", id="empty"),
        pytest.param({"actions": _op()}, "non-empty 'actions' array", id="actions-not-a-list"),
        pytest.param(
            {"actions": [_op(16 + n) for n in range(MAX_OPERATOR_ACTIONS_PER_FIX + 1)]},
            f"carries {MAX_OPERATOR_ACTIONS_PER_FIX + 1} actions; the controller accepts at "
            f"most {MAX_OPERATOR_ACTIONS_PER_FIX} per FIX result",
            id="too-many",
        ),
        pytest.param(
            {"actions": [_op()], "note": "x"},
            "operator_acceptance has unknown field(s) 'note'; it carries exactly 'actions'",
            id="unknown-object-key",
        ),
        pytest.param(
            {"actions": [_op(extra=1)]},
            "actions[0] has unknown field(s) 'extra'; it carries exactly 'kind', 'issue', 'body'",
            id="unknown-action-key",
        ),
        pytest.param(
            {"actions": ["comment on #16"]},
            "actions[0] must be an object with kind, issue and body",
            id="action-not-an-object",
        ),
        pytest.param(
            {"actions": [dict(_op(), kind="issue_comment")]},
            "kind 'issue_comment' is not supported; the one supported kind is "
            "'related_issue_comment'",
            id="unknown-kind",
        ),
        pytest.param(
            {"actions": [{"issue": 16, "body": OPERATOR_NOTE}]},
            "kind NoneType is not supported",
            id="no-kind",
        ),
        pytest.param(
            {"actions": [_op(16), _op(17), _op(16, "again")]},
            "names issue #16 twice; one finding requests at most one comment per issue",
            id="duplicate-issue",
        ),
    ],
)
def test_fix_operator_acceptance_is_a_closed_schema(operator, match):
    """The object carries ``actions`` only, 1 to 4 of them, each exactly a
    supported kind, an issue and a body, at most one per issue: nothing is
    ignored, because an ignored key would read as one the controller
    honoured."""
    msg = _refusal(lambda: FixResult.from_payload(_operator(operator)))
    assert msg.startswith("FIX: R1-F1 ")
    assert match in msg


@pytest.mark.parametrize(
    ("body", "match"),
    [
        pytest.param(None, "'body' must be a string", id="no-body"),
        pytest.param(7, "'body' must be a string", id="not-a-string"),
        pytest.param("  \n\t ", "missing required field 'body'", id="blank"),
        pytest.param(
            "b" * (MAX_OPERATOR_COMMENT_CHARS + 1),
            f"is {MAX_OPERATOR_COMMENT_CHARS + 1} characters; the controller accepts at most "
            f"{MAX_OPERATOR_COMMENT_CHARS}",
            id="long",
        ),
        pytest.param("ok\x07", "contains a control character", id="control"),
        pytest.param("<!-- ai-operator-comment: x -->", "controller marker", id="marker"),
        pytest.param("note\n<!--AutoForge-x-->", "controller marker", id="autoforge-marker"),
        pytest.param("Closes #12", "closing keyword", id="closing"),
        pytest.param("This fixes o/r#12 too", "closing keyword", id="closing-owner-repo"),
        pytest.param("ask @octocat", "@-mention", id="mention"),
        pytest.param("![shot](https://github.com/o/r/a.png)", "Markdown image", id="image"),
        pytest.param("See ![ref][shot]", "Markdown image at index 4", id="reference-image"),
        pytest.param("<details>x</details>", "raw HTML", id="html-tag"),
        pytest.param("note <!-- hidden -->", "raw HTML", id="html-comment"),
        pytest.param("see <https://github.com/o/r>", "raw HTML", id="autolink"),
    ],
)
def test_fix_operator_acceptance_text_is_bounded_and_publishable(body, match):
    """The operator posts the text under their own identity, often piped
    unedited: it is rejected (never clipped or redacted) when it is missing,
    blank, too long, would act on GitHub, or embeds an image or raw HTML."""
    msg = _refusal(lambda: FixResult.from_payload(_operator({"actions": [_op(16, body)]})))
    assert match in msg
    assert "operator_acceptance of R1-F1 for #16" in msg  # the subject names finding and issue


@pytest.mark.parametrize(
    "cls,text,secret",
    [
        ("github-token", "ghp_FAKE00000000", "FAKE00000000"),
        ("env-assignment", "GH_TOKEN=FAKEtoken123", "FAKEtoken123"),
        ("authorization-header", "Authorization: Bearer FAKEbearer123", "FAKEbearer123"),
        ("url-userinfo", "https://user:FAKEpass123@github.com/o/r", "FAKEpass123"),
    ],
    ids=["github-token", "env-assignment", "authorization-header", "url-userinfo"],
)
def test_fix_operator_acceptance_refuses_a_credential_without_quoting_it(cls, text, secret):
    """Published text is refused, never redacted: the refusal names the
    pattern class, never the matched text (the full table of classes is
    :func:`test_each_credential_class_is_refused_by_name_and_never_quoted`)."""
    body = f"The check ran with {text} and passed."
    msg = _refusal(lambda: FixResult.from_payload(_operator({"actions": [_op(16, body)]})))
    assert "credential-shaped" in msg and cls in msg and "never redacted" in msg
    assert secret not in msg


@pytest.mark.parametrize(
    "body",
    [
        "Use `![badge](x.png)` in the README.",
        "The tag is `<details>`.",
        "```\n<details>\n![shot](x.png)\n</details>\n```",
        "~~~html\n<br>\n~~~",
        "Compare a < b and a <3 b.",
    ],
    ids=["image-in-span", "html-in-span", "fenced", "tilde-fenced", "not-html"],
)
def test_fix_operator_acceptance_allows_an_image_or_html_inside_code(body):
    """GitHub renders neither an image nor raw HTML inside a code span or a
    fenced block, so there they are text and the body is kept as given."""
    [r] = FixResult.from_payload(_operator({"actions": [_op(16, body)]})).resolutions
    assert r.operator_actions == (OperatorAction(issue=16, body=body),)


@pytest.mark.parametrize(
    "body",
    [
        "Mixed: `<details>` then ![shot](x.png)",
        "```\nfenced\n```\n<details>x</details>",
    ],
    ids=["image-after-span", "html-after-fence"],
)
def test_fix_operator_acceptance_judges_markup_outside_code_only(body):
    msg = _refusal(lambda: FixResult.from_payload(_operator({"actions": [_op(16, body)]})))
    assert "outside code" in msg


def test_fix_operator_acceptance_needs_a_rationale_and_nothing_else():
    """Like a no-change resolution it explains itself; it never names a
    commit, a follow-up or an acceptance comment, and no other resolution
    carries the object."""
    operator = {"actions": [_op()]}
    with pytest.raises(ControlResultValidationError, match="R1-F1 uses operator_acceptance but"):
        FixResult.from_payload(_operator(operator, rationale="short"))
    with pytest.raises(ControlResultValidationError, match="rationale is missing"):
        FixResult.from_payload(_operator(operator, rationale=""))
    with pytest.raises(
        ControlResultValidationError, match="must carry an 'operator_acceptance' object"
    ):
        FixResult.from_payload(_operator(None))
    with pytest.raises(ControlResultValidationError, match="only a fixed finding"):
        FixResult.from_payload(_operator(operator, commit_sha=SHA_B))
    with pytest.raises(ControlResultValidationError, match="carries a follow-up issue"):
        FixResult.from_payload(_operator(operator, follow_up_issue_url=ISSUE))
    with pytest.raises(ControlResultValidationError, match="carries a follow-up issue"):
        FixResult.from_payload(_operator(operator, follow_up_issue=NEW_FOLLOW_UP))
    action = {"kind": "issue_comment", "body": "evidence"}
    with pytest.raises(
        ControlResultValidationError,
        match="carries an acceptance_action but resolution is 'operator_acceptance'",
    ):
        FixResult.from_payload(_operator(operator, acceptance_action=action))


@pytest.mark.parametrize(
    ("resolution", "extra"),
    [
        ("fixed", {}),
        ("no_change_with_rationale", {"rationale": OPERATOR_RATIONALE}),
        (
            "acceptance_action",
            {
                "rationale": OPERATOR_RATIONALE,
                "acceptance_action": {"kind": "issue_comment", "body": "evidence"},
            },
        ),
        ("follow_up_created", {"follow_up_issue_url": ISSUE}),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_only_an_operator_acceptance_resolution_carries_the_object(resolution, extra):
    payload = dict(
        _FIX_TOP,
        resolutions=[
            {
                "finding_id": "R1-F1",
                "resolution": resolution,
                "operator_acceptance": {"actions": [_op()]},
                **extra,
            }
        ],
    )
    with pytest.raises(
        ControlResultValidationError,
        match=f"R1-F1 carries an operator_acceptance but resolution is '{resolution}'",
    ):
        FixResult.from_payload(payload)


def _two_operator_findings(first: int, second: int) -> dict:
    payload = _operator({"actions": [_op(16 + n) for n in range(first)]})
    other = _operator({"actions": [_op(16 + n) for n in range(second)]}, fid="R1-F2")
    payload["resolutions"] += other["resolutions"]
    return payload


def test_fix_result_requests_at_most_four_operator_actions_in_total():
    """The bound is per FIX result, not per finding: two findings sharing four
    actions pass, five across them are refused whole."""
    res = FixResult.from_payload(_two_operator_findings(2, 2))
    assert [len(r.operator_actions) for r in res.resolutions] == [2, 2]
    msg = _refusal(lambda: FixResult.from_payload(_two_operator_findings(3, 2)))
    assert (
        f"FIX: the operator_acceptance resolutions request 5 actions; the controller accepts "
        f"at most {MAX_OPERATOR_ACTIONS_PER_FIX} per FIX result"
    ) in msg


def test_two_findings_may_each_request_a_comment_on_the_same_issue():
    """One comment per issue is a per-finding rule: each finding's comment
    carries its own marker, so two findings may both name #16."""
    res = FixResult.from_payload(_two_operator_findings(1, 1))
    assert [a.issue for r in res.resolutions for a in r.operator_actions] == [16, 16]


def test_fix_result_never_mixes_acceptance_action_and_operator_acceptance():
    """One FIX result asks for the controller's acceptance comment or for the
    operator's comments, never both."""
    payload = _acceptance({"kind": "issue_comment", "body": "evidence"})
    payload["resolutions"] += _operator({"actions": [_op()]}, fid="R1-F2")["resolutions"]
    msg = _refusal(lambda: FixResult.from_payload(payload))
    assert (
        "FIX: the result mixes acceptance_action (R1-F1) and operator_acceptance (R1-F2); one "
        "FIX result carries one or the other, never both"
    ) in msg


def test_local_fix_refuses_an_operator_acceptance():
    """LOCAL runs have no GitHub, so no operator action is offered either."""
    text, mode = _fix_with(_operator({"actions": [_op()]})["resolutions"], "LOCAL")
    msg = _refusal(lambda: parse_control_result(text, Phase.FIX, mode))
    assert "LOCAL runs have no GitHub and no operator actions" in msg
    assert "resolve as `unresolved` with a rationale instead" in msg


def test_remote_parse_control_result_accepts_an_operator_acceptance():
    text, mode = _fix_with(_operator({"actions": [_op()]})["resolutions"], "REMOTE")
    payload = parse_control_result(text, Phase.FIX, mode)
    assert payload["resolutions"][0]["resolution"] == "operator_acceptance"


# -- the operator text's link rule: links only into the run's repository (#243) --------
@pytest.mark.parametrize(
    "text",
    [
        "See https://github.com/o/r/issues/3.",
        "See https://github.com/O/R/pull/4#discussion_r1",
        "See https://github.com/o/r",
        "See https://github.com/o/r?tab=readme",
        "See https://github.com/o/r#readme",
        "See [the issue](https://github.com/o/r/issues/3).",
        "See [the issue]( https://github.com/o/r/issues/3 ).",
        "[ref]: https://github.com/o/r/issues/3\n\nSee [ref].",
        "See `https://github.com/o/r/blob/main/README.md`",
        "See #12 and o/r#3 and o/x#4.",
        "Plain text without any link.",
    ],
)
def test_operator_text_may_link_into_the_runs_repository(text):
    """Every link or bare URL is ``https://github.com/<repository>/...``;
    owner and repository compare case-insensitively, as GitHub does. An
    issue reference is not a link."""
    assert operator_comment_link_problem("operator note", text, "o/r") is None
    assert validate_operator_comment_body(text, "operator note", repository="o/r") == text


@pytest.mark.parametrize(
    ("text", "index"),
    [
        pytest.param("See https://evil.example/x.", 4, id="other-host"),
        pytest.param("See https://github.com.evil.example/o/r", 4, id="host-prefix"),
        pytest.param("See https://gitlab.com/o/evil/issues/3", 4, id="gitlab"),
        pytest.param("See https://github.com/o/r-evil/issues/3", 4, id="longer-repository"),
        pytest.param("See https://github.com/o/r.evil", 4, id="repository-suffix"),
        pytest.param("See https://github.com/evil/r/issues/3", 4, id="other-owner"),
        pytest.param("See http://github.com/o/r/evil", 4, id="http"),
        pytest.param("See ftp://github.com/o/r/evil", 4, id="other-scheme"),
        pytest.param("See [it](/evil/r/issues/3).", 9, id="root-relative"),
        pytest.param("See [it](evil/issues/3).", 9, id="relative"),
        pytest.param("See [it](#evil).", 9, id="fragment"),
        pytest.param("See [it](//evil.example).", 9, id="protocol-relative"),
        pytest.param("See [it](<https://evil.example>).", 10, id="angle-destination"),
        pytest.param("See www.evil.example", 4, id="www"),
        pytest.param("Mail evil@example.com", 8, id="e-mail"),
        pytest.param("See https://github.com/o/r/../evil/issues/3", 4, id="dot-dot"),
        pytest.param("See https://github.com/o/r/issues/evil/..", 4, id="trailing-dot-dot"),
        pytest.param("See https://github.com/o/r/./evil", 4, id="dot"),
        pytest.param("See https://github.com/o/r\\evil", 4, id="backslash"),
        pytest.param("[ref]: https://evil.example\n\nSee [ref].", 7, id="definition"),
        pytest.param("[ref]: /evil/r\n\nSee [ref].", 7, id="relative-definition"),
        pytest.param("`https://evil.example`", 1, id="in-code"),
        pytest.param("ok https://github.com/o/r and https://evil.example", 30, id="second"),
    ],
)
def test_operator_text_links_nowhere_but_the_runs_repository(text, index):
    """No relative link, other scheme, host or repository, ``www.`` or e-mail
    autolink, and no path that climbs out with ``..`` or a backslash; code
    is no exemption, since the operator copies the text whole. The refusal
    names the subject, the first index and the rule, never the text."""
    msg = operator_comment_link_problem("operator note", text, "o/r")
    assert msg is not None
    assert msg.startswith("'operator note' contains a link or URL at index ")
    assert f"at index {index} that is not 'https://github.com/o/r/...'" in msg
    assert "evil" not in msg
    # The body validator refuses it too (an angle destination is raw HTML first).
    raised = _refusal(lambda: validate_operator_comment_body(text, "note", repository="o/r"))
    assert raised.startswith("FIX: field 'note body' contains ")
    assert "evil" not in raised


def test_validate_operator_comment_body_applies_the_link_rule_only_with_a_repository():
    """The parser does not know the run's repository, so a FIX result with a
    foreign link parses; the controller applies the rule when it checks the
    result and when it reloads a persisted record."""
    text = "Background: https://evil.example/post"
    assert validate_operator_comment_body(text) == text
    assert validate_operator_comment_body(text, "note", repository="") == text
    with pytest.raises(ControlResultValidationError, match="not 'https://github.com/o/r/...'"):
        validate_operator_comment_body(text, "note", repository="o/r")
    [r] = FixResult.from_payload(_operator({"actions": [_op(16, text)]})).resolutions
    assert r.operator_actions[0].body == text


def test_validate_operator_comment_body_applies_every_other_rule_without_a_repository():
    """Bounds, the published-text rules and the markup rule never depend on
    the repository: a persisted record is re-validated under the same rules."""
    for bad, match in [
        ("", "missing required field 'body'"),
        ("x" * (MAX_OPERATOR_COMMENT_CHARS + 1), f"at most {MAX_OPERATOR_COMMENT_CHARS}"),
        ("Closes #3", "closing keyword"),
        ("![x](y)", "Markdown image"),
        ("<b>x</b>", "raw HTML"),
    ]:
        for repository in ("", "o/r"):
            with pytest.raises(ControlResultValidationError, match=match):
                validate_operator_comment_body(bad, "note", repository=repository)
    stripped = validate_operator_comment_body("  kept\n", "note", repository="o/r")
    assert stripped == "kept"


def test_operator_markup_problem_names_an_index_never_the_text():
    msg = operator_markup_problem("note", "Look: ![secret-shot](x.png)")
    assert msg is not None and "Markdown image at index 6 outside code" in msg
    assert "secret-shot" not in msg
    msg = operator_markup_problem("note", "Look: <img src=secret.png>")
    assert msg is not None and "raw HTML (a tag, comment or autolink) at index 6" in msg
    assert "secret" not in msg
    assert operator_markup_problem("note", "Look: `<img>` and `![x](y)`") is None


@pytest.mark.parametrize(
    "text",
    [
        "> [ref]: //evil.example/x\n\nSee [the note][ref].",
        "- [ref]: /evil/repo\n\nSee [the note][ref].",
        "[a\\]b]: //evil.example\n\nSee [the note][a\\]b].",
        "[a\nb]: //evil.example\n\nSee [the note][a b].",
    ],
    ids=["block-quote", "list-item", "escaped-label", "multi-line-label"],
)
def test_operator_text_refuses_a_link_definition_in_any_container(text):
    assert operator_comment_link_problem("operator note", text, "o/r") is not None


@pytest.mark.parametrize(
    "text",
    [
        "See https://github.com/o/r/%2e%2e/%2e%2e/evil/repo",
        "See https://github.com/o/r/.%2E/.%2E/evil/repo",
        "See [it](https://github.com/o/r/&#46;&#46;/&#46;&#46;/evil/repo).",
    ],
    ids=["percent", "mixed-percent", "entity"],
)
def test_operator_text_refuses_an_encoded_dot_segment(text):
    assert operator_comment_link_problem("operator note", text, "o/r") is not None


def test_fix_result_reports_its_tests_within_bounds():
    """The ``tests`` list is recorded with the result, bounded like ANALYZE_EXECUTE's."""
    base = {
        "phase": "FIX",
        "status": "success",
        "previous_head_sha": SHA_A,
        "head_sha": SHA_B,
        "resolutions": [{"finding_id": "R1-F1", "resolution": "fixed"}],
    }
    assert FixResult.from_payload(dict(base, tests=["make check: passed"])).tests == [
        "make check: passed"
    ]
    with pytest.raises(ControlResultValidationError, match="at most"):
        FixResult.from_payload(dict(base, tests=["t"] * (MAX_TESTS_REPORTED + 1)))


# -- R6-F3: optional free-text fields are validated, never coerced ------------
@pytest.mark.parametrize("bad", [1, 1.5, True, ["x"], {"a": 1}])
def test_finding_optional_text_fields_reject_non_strings(bad):
    """`str(...)` turned a schema violation into content.

    A number, list or object in `title`/`location` used to be stringified and
    then persisted, rendered into the next FIX prompt, and shown to a human as
    if the agent had written it. The field is a string or it is absent.
    """
    base = dict(GOOD_REVIEW, needs_fix_round=True)
    for field_name in ("title", "location"):
        payload = dict(_finding(1, 1))
        payload[field_name] = bad
        with pytest.raises(ControlResultValidationError, match=field_name):
            ReviewResult.from_payload(dict(base, findings=[payload]))


def test_finding_optional_text_fields_accept_absent_null_and_strings():
    base = dict(GOOD_REVIEW, needs_fix_round=True)
    absent = ReviewResult.from_payload(dict(base, findings=[_finding(1, 1)]))
    assert absent.findings[0].title == "" and absent.findings[0].location == ""
    nulled = ReviewResult.from_payload(
        dict(base, findings=[dict(_finding(1, 1), title=None, location=None)])
    )
    assert nulled.findings[0].title == "" and nulled.findings[0].location == ""
    given = ReviewResult.from_payload(
        dict(base, findings=[dict(_finding(1, 1), title=" t ", location=" src/a.py:1 ")])
    )
    assert given.findings[0].title == "t" and given.findings[0].location == "src/a.py:1"


@pytest.mark.parametrize("key", ["rationale", "follow_up_issue_url", "commit_sha"])
@pytest.mark.parametrize("bad", [1, ["x"], {"a": 1}])
def test_remote_fix_optional_text_fields_reject_non_strings(key, bad):
    base = {"phase": "FIX", "status": "success", "previous_head_sha": SHA_A, "head_sha": SHA_B}
    resolution = {"finding_id": "R1-F1", "resolution": "fixed", key: bad}
    with pytest.raises(ControlResultValidationError, match=key):
        FixResult.from_payload(dict(base, resolutions=[resolution]))


@pytest.mark.parametrize("bad", [1, ["x"], {"a": 1}])
def test_local_fix_rationale_rejects_non_strings(bad):
    from autoforge.result_parser import LocalFixResult

    with pytest.raises(ControlResultValidationError, match="rationale"):
        LocalFixResult.from_payload(
            {"resolutions": [{"finding_id": "R1-F1", "resolution": "fixed", "rationale": bad}]}
        )


# -- Every optional string field, every non-string JSON type ------------------
# One table, so a new optional string field has to be added here to be
# covered and a decoder that quietly coerces is caught by the same rows that
# catch every other one. Each row is (label, builder): the builder takes the
# value to put in the field and returns a full stdout to parse for the phase.
def _remote_fix(resolution_extra: dict, **top) -> str:
    return block(
        {
            "phase": "FIX",
            "status": "success",
            "previous_head_sha": SHA_A,
            "head_sha": SHA_B,
            "resolutions": [{"finding_id": "R1-F1", "resolution": "fixed", **resolution_extra}],
            **top,
        }
    )


def _local_fix(resolution_extra: dict, **top) -> str:
    return block(
        {
            "phase": "FIX",
            "status": "success",
            "changed_workspace": True,
            "resolutions": [{"finding_id": "R1-F1", "resolution": "fixed", **resolution_extra}],
            **top,
        }
    )


def _remote_review(finding_extra: dict) -> str:
    return block(
        dict(GOOD_REVIEW, needs_fix_round=True, findings=[dict(_finding(1, 1), **finding_extra)])
    )


def _local_review(finding_extra: dict) -> str:
    return block(
        {
            "phase": "REVIEW",
            "status": "success",
            "round": 1,
            "reviewed_workspace_fingerprint": "a" * 64,
            "needs_fix_round": True,
            "findings": [dict(_finding(1, 1), **finding_extra)],
        }
    )


OPTIONAL_STRING_FIELDS = [
    # (label, phase, mode, builder)
    ("REVIEW.findings[].title", Phase.REVIEW, "REMOTE", lambda v: _remote_review({"title": v})),
    (
        "REVIEW.findings[].location",
        Phase.REVIEW,
        "REMOTE",
        lambda v: _remote_review({"location": v}),
    ),
    (
        "LOCAL REVIEW.findings[].title",
        Phase.REVIEW,
        "LOCAL",
        lambda v: _local_review({"title": v}),
    ),
    (
        "LOCAL REVIEW.findings[].location",
        Phase.REVIEW,
        "LOCAL",
        lambda v: _local_review({"location": v}),
    ),
    ("FIX.resolutions[].rationale", Phase.FIX, "REMOTE", lambda v: _remote_fix({"rationale": v})),
    (
        "FIX.resolutions[].commit_sha",
        Phase.FIX,
        "REMOTE",
        lambda v: _remote_fix({"commit_sha": v}),
    ),
    (
        "FIX.resolutions[].follow_up_issue_url",
        Phase.FIX,
        "REMOTE",
        lambda v: _remote_fix({"follow_up_issue_url": v}),
    ),
    (
        "LOCAL FIX.resolutions[].rationale",
        Phase.FIX,
        "LOCAL",
        lambda v: _local_fix({"rationale": v}),
    ),
    ("LOCAL FIX.blocked_reason", Phase.FIX, "LOCAL", lambda v: _local_fix({}, blocked_reason=v)),
    (
        "UPDATE_EPIC.next_issue_url",
        Phase.UPDATE_EPIC,
        "REMOTE",
        lambda v: block(
            {
                "phase": "UPDATE_EPIC",
                "status": "success",
                "next_issue_url": v,
                "progress": "Issue #2 merged.",
            }
        ),
    ),
    (
        "<any>.message (status blocked)",
        Phase.FIX,
        "REMOTE",
        lambda v: block({"phase": "FIX", "status": "blocked", "message": v}),
    ),
]

NON_STRING_JSON_VALUES = [0, 1, 1.5, True, False, [], ["x"], {}, {"a": 1}]


def _mode(name):
    return WorkflowMode[name]


@pytest.mark.parametrize("bad", NON_STRING_JSON_VALUES, ids=repr)
@pytest.mark.parametrize(
    "label,phase,mode,build", OPTIONAL_STRING_FIELDS, ids=[r[0] for r in OPTIONAL_STRING_FIELDS]
)
def test_every_optional_string_field_rejects_every_non_string_json_type(
    label, phase, mode, build, bad
):
    """A schema violation is a rejection, never content.

    ``str(1)``, ``str(True)``, ``str([])`` all produce text the controller
    would persist, render into the next prompt and show to a human as the
    agent's words. No decoder may coerce; the field is a string, ``null``,
    absent -- or the result is refused and the state machine does not move.
    """
    with pytest.raises(ControlResultValidationError):
        parse_control_result(build(bad), phase, _mode(mode))


@pytest.mark.parametrize(
    "label,phase,mode,build", OPTIONAL_STRING_FIELDS, ids=[r[0] for r in OPTIONAL_STRING_FIELDS]
)
def test_every_optional_string_field_accepts_a_string(label, phase, mode, build):
    text = "some text here ok"
    if "url" in label:
        text = "https://github.com/owner/repo/issues/9"
    elif "sha" in label:
        text = SHA_B  # #77: a present commit_sha must be a SHA, not any string
    if label == "<any>.message (status blocked)":
        parse_control_result(build(text), phase, _mode(mode))
        return
    if label == "LOCAL FIX.blocked_reason":
        # A non-empty blocker beside status "success" is a contradiction (its
        # own rule); the type check is what this table is about.
        with pytest.raises(ControlResultValidationError, match="not both true"):
            parse_control_result(build(text), phase, _mode(mode))
        return
    if label == "FIX.resolutions[].follow_up_issue_url":
        with pytest.raises(ControlResultValidationError, match="resolution is 'fixed'"):
            parse_control_result(build(text), phase, _mode(mode))
        return
    parse_control_result(build(text), phase, _mode(mode))


@pytest.mark.parametrize(
    "label,phase,mode,build",
    [r for r in OPTIONAL_STRING_FIELDS if not r[0].startswith("<any>")],
    ids=[r[0] for r in OPTIONAL_STRING_FIELDS if not r[0].startswith("<any>")],
)
def test_every_optional_string_field_treats_null_as_absent(label, phase, mode, build):
    parse_control_result(build(None), phase, _mode(mode))


# -- REVIEW payload bounds (#34) -------------------------------------------------
# Review output is untrusted, and every accepted finding is persisted whole in
# ``state.open_findings`` and rendered whole into the FIX prompt. The parser
# bounds the payload and *rejects* an oversized one (correctable: the reviewer
# re-emits) rather than clipping it (which would silently drop the work the
# FIX round has to act on).


def _review_with(findings: list[dict], mode: str) -> tuple[str, WorkflowMode]:
    if mode == "REMOTE":
        return block(
            dict(GOOD_REVIEW, needs_fix_round=True, findings=findings)
        ), WorkflowMode.REMOTE
    return _local_review_block(findings), WorkflowMode.LOCAL


def _local_review_block(findings: list[dict]) -> str:
    return block(
        {
            "phase": "REVIEW",
            "status": "success",
            "round": 1,
            "reviewed_workspace_fingerprint": "a" * 64,
            "needs_fix_round": True,
            "findings": findings,
        }
    )


@pytest.mark.parametrize("mode", ["REMOTE", "LOCAL"])
def test_finding_count_is_bounded_and_rejected_whole(mode):
    at_bound = [_finding(1, n) for n in range(1, MAX_FINDINGS_PER_REVIEW + 1)]
    stdout, wf_mode = _review_with(at_bound, mode)
    payload = parse_control_result(stdout, Phase.REVIEW, wf_mode)
    assert len(payload["findings"]) == MAX_FINDINGS_PER_REVIEW

    over = at_bound + [_finding(1, MAX_FINDINGS_PER_REVIEW + 1)]
    stdout, wf_mode = _review_with(over, mode)
    with pytest.raises(ControlResultValidationError) as excinfo:
        parse_control_result(stdout, Phase.REVIEW, wf_mode)
    msg = str(excinfo.value)
    assert f"{MAX_FINDINGS_PER_REVIEW + 1} findings" in msg
    assert f"at most {MAX_FINDINGS_PER_REVIEW}" in msg
    assert "re-emit" in msg


@pytest.mark.parametrize("mode", ["REMOTE", "LOCAL"])
def test_finding_count_is_checked_before_any_element_is_parsed(mode):
    """An oversized list is refused as a list, not element by element."""
    junk: list = list(range(MAX_FINDINGS_PER_REVIEW + 1))  # not even objects
    stdout, wf_mode = _review_with(junk, mode)
    with pytest.raises(ControlResultValidationError, match="findings reported"):
        parse_control_result(stdout, Phase.REVIEW, wf_mode)
    # One fewer element and the per-element validation is what speaks.
    stdout, wf_mode = _review_with(junk[:-1], mode)
    with pytest.raises(ControlResultValidationError, match=r"findings\[0\] must be an object"):
        parse_control_result(stdout, Phase.REVIEW, wf_mode)


@pytest.mark.parametrize(
    "key,limit",
    [
        ("required_resolution", MAX_FINDING_RESOLUTION_CHARS),
        ("title", MAX_FINDING_TITLE_CHARS),
        ("location", MAX_FINDING_LOCATION_CHARS),
    ],
)
@pytest.mark.parametrize("mode", ["REMOTE", "LOCAL"])
def test_finding_text_fields_are_bounded(key, limit, mode):
    exact = "x" * limit
    stdout, wf_mode = _review_with([dict(_finding(1, 1), **{key: exact})], mode)
    payload = parse_control_result(stdout, Phase.REVIEW, wf_mode)
    assert payload["findings"][0][key] == exact
    # The bound applies to the stripped value the controller persists, so
    # surrounding whitespace does not count against it.
    stdout, wf_mode = _review_with([dict(_finding(1, 1), **{key: f"\n  {exact}  \n"})], mode)
    parse_control_result(stdout, Phase.REVIEW, wf_mode)

    over = "y" * (limit + 1)
    stdout, wf_mode = _review_with([dict(_finding(1, 1), **{key: over})], mode)
    with pytest.raises(ControlResultValidationError) as excinfo:
        parse_control_result(stdout, Phase.REVIEW, wf_mode)
    msg = str(excinfo.value)
    assert f"R1-F1 field {key!r} is {limit + 1} characters" in msg
    assert f"at most {limit}" in msg
    # The rejection names the size, never the text: the message is echoed
    # into the correction prompt and the run log.
    assert "yyyy" not in msg


def _id_of_length(length: int, round: int = 1, n: int = 1) -> str:
    """A well-formed ``R<round>-F<n>`` id padded to exactly ``length`` characters."""
    head = f"R{round}-F"
    tail = str(n)
    fid = head + "9" * (length - len(head) - len(tail)) + tail
    assert len(fid) == length
    return fid


@pytest.mark.parametrize("mode", ["REMOTE", "LOCAL"])
def test_finding_id_is_bounded(mode):
    """The id's shape does not bound its length; the parser does (PR #76 review)."""
    exact = _id_of_length(MAX_FINDING_ID_CHARS)
    stdout, wf_mode = _review_with([dict(_finding(1, 1), id=exact)], mode)
    payload = parse_control_result(stdout, Phase.REVIEW, wf_mode)
    assert payload["findings"][0]["id"] == exact

    over = _id_of_length(MAX_FINDING_ID_CHARS + 1)
    stdout, wf_mode = _review_with([dict(_finding(1, 1), id=over)], mode)
    with pytest.raises(ControlResultValidationError) as excinfo:
        parse_control_result(stdout, Phase.REVIEW, wf_mode)
    msg = str(excinfo.value)
    assert f"'id' is {MAX_FINDING_ID_CHARS + 1} characters" in msg
    assert f"at most {MAX_FINDING_ID_CHARS}" in msg
    assert "9999" not in msg


@pytest.mark.parametrize(
    "fid",
    [
        "R1-F" + "9" * 100_000,  # the reviewer's reproduction: well-formed, huge
        "R" + "1" * 5_000 + "-F1",  # a round component past int()'s digit limit
        "Q" * 100_000,  # malformed: the shape error would quote it whole
    ],
    ids=["huge-ordinal", "huge-round", "huge-malformed"],
)
def test_oversized_finding_id_is_refused_before_it_is_quoted_or_converted(fid):
    """Length is checked before the shape and round checks.

    The shape and round messages quote the id, and those messages reach the
    correction prompt and the run log; and ``int()`` of a digit run past the
    interpreter's conversion limit is a ``ValueError``, not a rejection.
    """
    with pytest.raises(ControlResultValidationError) as excinfo:
        Finding.from_payload(dict(_finding(1, 1), id=fid), 1, 0)
    msg = str(excinfo.value)
    assert f"is {len(fid)} characters" in msg
    assert len(msg) < 300


def test_a_finding_id_is_stripped_at_the_result_boundary_but_the_rule_itself_is_exact():
    """A CONTROL_RESULT string field is stripped before its shape is checked,
    so ``"R1-F1\\n"`` in a result *is* ``R1-F1``. The shared rule the
    marker scanner applies to unstripped GitHub data must not accept the
    newline itself: ``$`` would (it matches before a trailing newline), so
    the rule ends in ``\\Z`` and ``autoforge.claims`` refuses the marker."""
    assert Finding.from_payload(dict(_finding(1, 1), id="R1-F1\n"), 1, 0).id == "R1-F1"
    assert FINDING_ID_RE.match("R1-F1") is not None
    for fid in ("R1-F1\n", "R1-F1\r\n", "\nR1-F1", "R1-F1 "):
        assert FINDING_ID_RE.match(fid) is None, repr(fid)


def test_fix_finding_id_is_bounded_at_parse_time():
    """A FIX finding_id can only ever match a bounded REVIEW id, so it is
    refused at parse rather than echoed back by the engine's coverage check."""
    exact = _id_of_length(MAX_FINDING_ID_CHARS)
    over = _id_of_length(MAX_FINDING_ID_CHARS + 1)
    remote = {
        "phase": "FIX",
        "status": "success",
        "previous_head_sha": SHA_A,
        "head_sha": SHA_B,
    }
    ok = FixResult.from_payload(
        dict(remote, resolutions=[{"finding_id": exact, "resolution": "fixed"}])
    )
    assert ok.resolutions[0].finding_id == exact
    with pytest.raises(ControlResultValidationError) as excinfo:
        FixResult.from_payload(
            dict(remote, resolutions=[{"finding_id": over, "resolution": "fixed"}])
        )
    assert f"FIX: field 'finding_id' is {MAX_FINDING_ID_CHARS + 1} characters" in str(excinfo.value)
    assert "9999" not in str(excinfo.value)

    ok_local = LocalFixResult.from_payload(
        {"resolutions": [{"finding_id": exact, "resolution": "fixed"}], "changed_workspace": True}
    )
    assert ok_local.resolutions[0].finding_id == exact
    with pytest.raises(ControlResultValidationError, match="at most"):
        LocalFixResult.from_payload(
            {
                "resolutions": [{"finding_id": over, "resolution": "fixed"}],
                "changed_workspace": True,
            }
        )


def test_oversized_finding_is_rejected_not_clipped():
    over = "z" * (MAX_FINDING_RESOLUTION_CHARS + 1)
    with pytest.raises(ControlResultValidationError):
        ReviewResult.from_payload(
            dict(
                GOOD_REVIEW,
                needs_fix_round=True,
                findings=[dict(_finding(1, 1), required_resolution=over)],
            )
        )
    with pytest.raises(ControlResultValidationError):
        Finding.from_payload(dict(_finding(1, 1), required_resolution=over), 1, 0)


def test_parser_bounds_never_exceed_the_persisted_evidence_bounds():
    """A round the parser accepted is always retained complete in history.

    ``loop_guard`` clips persisted evidence and marks the round truncated,
    which refuses a later replan. With the parser bounds at or below those
    limits, that marker can only come from state persisted before the parser
    bounds existed, never from a result the controller itself accepted.
    """
    assert MAX_FINDINGS_PER_REVIEW <= loop_guard.MAX_PERSISTED_FINDINGS_PER_ROUND
    assert MAX_FINDINGS_PER_REVIEW <= loop_guard.MAX_PERSISTED_RESOLUTION_DIGESTS
    # The engine redacts a finding between the parser and the history, and
    # redaction can lengthen a text, so the persisted bound must absorb the
    # growth of a resolution that is exactly at the parser bound (#33).
    assert (
        MAX_FINDING_RESOLUTION_CHARS * MAX_GROWTH_FACTOR <= loop_guard.MAX_REQUIRED_RESOLUTION_CHARS
    )
    # The worst shape redaction can grow: a one-character secret behind the
    # shortest recognised name, repeated to fill the parser bound exactly.
    unit = "HF_TOKEN=x;"
    resolution = (unit * (MAX_FINDING_RESOLUTION_CHARS // len(unit) + 1))[
        :MAX_FINDING_RESOLUTION_CHARS
    ]
    findings = [
        dict(
            _finding(1, n),
            required_resolution=resolution,
            title="t" * MAX_FINDING_TITLE_CHARS,
            location="l" * MAX_FINDING_LOCATION_CHARS,
        )
        for n in range(1, MAX_FINDINGS_PER_REVIEW + 1)
    ]
    # A REMOTE finding is published in the controller's review comment, so a
    # credential shape is refused at parse (#162); a LOCAL one is published
    # nowhere and still reaches the redaction this bound has to absorb.
    with pytest.raises(ControlResultValidationError, match="credential-shaped"):
        ReviewResult.from_payload(dict(GOOD_REVIEW, needs_fix_round=True, findings=findings))
    res = LocalReviewResult.from_payload(
        {
            "round": 1,
            "reviewed_workspace_fingerprint": "a" * 64,
            "needs_fix_round": True,
            "findings": findings,
        }
    )
    persisted = [redact_dict(f.to_dict()) for f in res.findings]  # the engine's own path
    assert len(persisted[0]["required_resolution"]) > MAX_FINDING_RESOLUTION_CHARS
    assert "HF_TOKEN=x" not in persisted[0]["required_resolution"]
    record = loop_guard.review_record(1, SHA_A, loop_guard.RESULT_NEEDS_FIX, persisted)
    assert record["finding_count"] == len(record["findings"]) == MAX_FINDINGS_PER_REVIEW
    assert record["findings"][0]["required_resolution"] == persisted[0]["required_resolution"]
    assert "evidence_truncated" not in record
    assert record["resolutions_truncated"] is False
    assert loop_guard.truncated_evidence_rounds([record]) == []


# -- FIX payload bounds (#77) --------------------------------------------------------
# A FIX resolution is persisted whole in ``state.last_fix_resolutions`` (after
# redaction) and, in LOCAL mode, an ``unresolved`` rationale is echoed into the
# persisted block reason that ``status`` shows. Same policy as the REVIEW
# bounds: rejected whole, never clipped; the message names the size and the
# limit, never the text; the count is checked before any element is parsed.
_FIX_TOP = {"phase": "FIX", "status": "success", "previous_head_sha": SHA_A, "head_sha": SHA_B}
_LOCAL_FIX_TOP = {"phase": "FIX", "status": "success", "changed_workspace": True}


def _fix_with(resolutions: list, mode: str) -> tuple[str, WorkflowMode]:
    if mode == "REMOTE":
        return block(dict(_FIX_TOP, resolutions=resolutions)), WorkflowMode.REMOTE
    return block(dict(_LOCAL_FIX_TOP, resolutions=resolutions)), WorkflowMode.LOCAL


@pytest.mark.parametrize(
    "mode,resolution",
    [
        ("REMOTE", "fixed"),
        ("REMOTE", "no_change_with_rationale"),
        ("LOCAL", "fixed"),
        ("LOCAL", "no_change_with_rationale"),
        ("LOCAL", "unresolved"),
    ],
)
def test_fix_rationale_is_bounded_whatever_the_resolution(mode, resolution):
    exact = "r" * MAX_FIX_RATIONALE_CHARS
    res = {"finding_id": "R1-F1", "resolution": resolution, "rationale": exact}
    stdout, wf_mode = _fix_with([res], mode)
    assert parse_control_result(stdout, Phase.FIX, wf_mode)["resolutions"][0]["rationale"] == exact
    # The bound applies to the stripped value the controller persists.
    stdout, wf_mode = _fix_with([dict(res, rationale=f"\n  {exact}  \n")], mode)
    parse_control_result(stdout, Phase.FIX, wf_mode)

    over = "r" * (MAX_FIX_RATIONALE_CHARS + 1)
    stdout, wf_mode = _fix_with([dict(res, rationale=over)], mode)
    with pytest.raises(ControlResultValidationError) as excinfo:
        parse_control_result(stdout, Phase.FIX, wf_mode)
    msg = str(excinfo.value)
    assert (
        f"FIX: resolution for R1-F1 field 'rationale' is {MAX_FIX_RATIONALE_CHARS + 1} characters"
        in msg
    )
    assert f"at most {MAX_FIX_RATIONALE_CHARS}" in msg
    assert "rrr" not in msg  # size, never the text


@pytest.mark.parametrize("mode", ["REMOTE", "LOCAL"])
def test_fix_resolution_count_is_bounded_and_checked_before_any_element_is_parsed(mode):
    at_bound = [
        {"finding_id": f"R1-F{n}", "resolution": "fixed"}
        for n in range(1, MAX_RESOLUTIONS_PER_FIX + 1)
    ]
    stdout, wf_mode = _fix_with(at_bound, mode)
    payload = parse_control_result(stdout, Phase.FIX, wf_mode)
    assert len(payload["resolutions"]) == MAX_RESOLUTIONS_PER_FIX

    junk: list = list(range(MAX_RESOLUTIONS_PER_FIX + 1))  # not even objects
    stdout, wf_mode = _fix_with(junk, mode)
    with pytest.raises(ControlResultValidationError) as excinfo:
        parse_control_result(stdout, Phase.FIX, wf_mode)
    msg = str(excinfo.value)
    assert f"FIX: {MAX_RESOLUTIONS_PER_FIX + 1} resolutions reported" in msg
    assert f"at most {MAX_RESOLUTIONS_PER_FIX}" in msg
    # One fewer element and the per-element validation is what speaks.
    stdout, wf_mode = _fix_with(junk[:-1], mode)
    with pytest.raises(ControlResultValidationError, match=r"resolutions\[0\] must be an object"):
        parse_control_result(stdout, Phase.FIX, wf_mode)


def _remote_fix_result(**resolution) -> FixResult:
    return FixResult.from_payload(
        dict(_FIX_TOP, resolutions=[{"finding_id": "R1-F1", "resolution": "fixed", **resolution}])
    )


def test_fix_commit_sha_must_be_a_full_sha_when_present():
    for sha in (SHA_B, SHA_B.upper()):
        assert _remote_fix_result(commit_sha=sha).resolutions[0].commit_sha == sha.lower()
    for absent in ({}, {"commit_sha": None}, {"commit_sha": ""}, {"commit_sha": "  "}):
        assert _remote_fix_result(**absent).resolutions[0].commit_sha == ""
    for bad in ("not-a-sha", SHA_B[:7], SHA_B[:39], SHA_B + "0"):
        with pytest.raises(ControlResultValidationError, match="'commit_sha' must be a full git"):
            _remote_fix_result(commit_sha=bad)


_REPLAN_REST = {
    "issue_url": ISSUE,
    "previous_pr_url": PR,
    "previous_branch": BRANCH,
    "execution_attempt": 2,
    "historical_findings_considered": 1,
    "unique_failure_constraints": 1,
    "previous_pr_disposition": "superseded",
    "fresh_review_round": 1,
    "verification": {"tests_run": ["pytest"], "tests_passed": True},
    "pr_title": "Reimplement it",
    "pr_body": "A fresh reimplementation.",
}

_SHA_FIELDS = [
    pytest.param(
        Phase.ANALYZE_EXECUTE,
        WorkflowMode.REMOTE,
        {"issue_url": ISSUE, "pr_title": "Add it", "pr_body": "Adds it."},
        "head_sha",
        id="ANALYZE_EXECUTE.head_sha",
    ),
    pytest.param(
        Phase.REVIEW,
        WorkflowMode.REMOTE,
        {"round": 1, "needs_fix_round": False, "findings": [], **REVIEW_PROSE},
        "reviewed_head_sha",
        id="REVIEW.reviewed_head_sha",
    ),
    pytest.param(
        Phase.FIX,
        WorkflowMode.REMOTE,
        {"head_sha": SHA_B, "resolutions": []},
        "previous_head_sha",
        id="FIX.previous_head_sha",
    ),
    pytest.param(
        Phase.FIX,
        WorkflowMode.REMOTE,
        {"previous_head_sha": SHA_A, "resolutions": []},
        "head_sha",
        id="FIX.head_sha",
    ),
    pytest.param(
        Phase.REPLAN_REEXECUTE,
        WorkflowMode.REMOTE,
        dict(_REPLAN_REST, head_sha=SHA_B),
        "previous_head_sha",
        id="REPLAN_REEXECUTE.previous_head_sha",
    ),
    pytest.param(
        Phase.REPLAN_REEXECUTE,
        WorkflowMode.REMOTE,
        dict(_REPLAN_REST, previous_head_sha=SHA_B),
        "head_sha",
        id="REPLAN_REEXECUTE.head_sha",
    ),
]


@pytest.mark.parametrize("phase,mode,rest,key", _SHA_FIELDS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(SHA_A[:7], id="abbreviated-7"),
        pytest.param(SHA_A[:12], id="abbreviated-12"),
        pytest.param(SHA_A[:39], id="one-short"),
        pytest.param(SHA_A + "a", id="one-long"),
        pytest.param("g" * 40, id="not-hex"),
    ],
)
def test_every_sha_field_requires_the_full_40_character_sha(phase, mode, rest, key, value):
    """#19: the prompts require the full SHA `git rev-parse HEAD` / `gh pr view
    --json headRefOid` report, and the engine compares every accepted SHA by
    equality with one read from GitHub. An abbreviated SHA must therefore be
    the schema error it is, at parse time, not a later "SHA mismatch" whose
    message blames the comparison rather than the shape."""
    payload = {"phase": phase.value, "status": "success", key: value, **rest}
    with pytest.raises(
        ControlResultValidationError, match=rf"{key!r} must be a full git SHA \(exactly 40"
    ):
        parse_control_result(block(payload), phase, mode)
    # The same payload with the full SHA is the accepted shape.
    parse_control_result(block(dict(payload, **{key: SHA_A})), phase, mode)


def test_sha_rejection_quotes_a_short_value_and_measures_a_long_one():
    with pytest.raises(ControlResultValidationError, match=repr(SHA_A[:7])):
        _remote_fix_result(commit_sha=SHA_A[:7])
    with pytest.raises(ControlResultValidationError, match="41 characters") as info:
        _remote_fix_result(commit_sha=SHA_A + "a")
    assert SHA_A + "a" not in str(info.value)


def test_a_fenced_control_result_block_is_accepted():
    """The prompts show every schema example inside a Markdown code fence and
    say a fence around the block is harmless (#19); the parser reads the
    block by its markers alone, so that promise is pinned here."""
    payload = {
        "phase": "UPDATE_EPIC",
        "status": "success",
        "next_issue_url": None,
        "progress": "Issue #2 merged.",
    }
    stdout = "progress log\n```text\n" + block(payload) + "\n```\n"
    assert parse_control_result(stdout, Phase.UPDATE_EPIC) == payload
    stdout = "```\n" + block(payload) + "```"
    assert parse_control_result(stdout, Phase.UPDATE_EPIC) == payload


@pytest.mark.parametrize(
    "label,build",
    [
        ("FIX.resolutions[].commit_sha", lambda v: _remote_fix_result(commit_sha=v)),
        (
            "FIX.head_sha",
            lambda v: FixResult.from_payload(dict(_FIX_TOP, head_sha=v, resolutions=[])),
        ),
    ],
    ids=["optional", "required"],
)
def test_a_sha_that_is_not_one_is_quoted_only_when_it_is_short(label, build):
    """The rejection quotes a wrong short value (useful) and reports only the
    length of an oversized one (never echoed into the correction prompt)."""
    with pytest.raises(ControlResultValidationError, match="got 'zzzzzzz'"):
        build("zzzzzzz")
    huge = "g" * 5000
    with pytest.raises(ControlResultValidationError) as excinfo:
        build(huge)
    msg = str(excinfo.value)
    assert "got 5000 characters" in msg
    assert "ggg" not in msg


def test_fix_follow_up_issue_url_is_shape_checked_at_parse_time():
    """A malformed follow-up URL is a validation error of the FIX result, not
    a ConfigurationError raised later by the engine (the follow_up half of #15)."""

    def follow_up(url):
        return FixResult.from_payload(
            dict(
                _FIX_TOP,
                resolutions=[
                    {
                        "finding_id": "R1-F1",
                        "resolution": "follow_up_created",
                        "follow_up_issue_url": url,
                    }
                ],
            )
        )

    assert follow_up(ISSUE).resolutions[0].follow_up_issue_url == ISSUE
    for bad in ("garbage", PR, ISSUE + "?x=1", "http://github.com/o/r/issues/1"):
        with pytest.raises(
            ControlResultValidationError, match="'follow_up_issue_url' must be a GitHub issue URL"
        ):
            follow_up(bad)


def test_update_epic_next_issue_url_is_shape_checked_at_parse_time():
    """A malformed next issue URL is a validation error of the UPDATE_EPIC
    result (the next_issue_url half of #15), not a selection for the engine
    to reject; ``null`` and ``""`` still mean "the epic is complete"."""

    def nxt(url):
        return UpdateEpicResult.from_payload(
            {
                "phase": "UPDATE_EPIC",
                "status": "success",
                "next_issue_url": url,
                "progress": "Issue #2 merged.",
            }
        )

    assert nxt(ISSUE).next_issue_url == ISSUE
    assert nxt(None).next_issue_url is None
    assert nxt("").next_issue_url is None
    for bad in ("not a url", PR, ISSUE + "?x=1", "http://github.com/o/r/issues/1"):
        with pytest.raises(
            ControlResultValidationError, match="'next_issue_url' must be a GitHub issue URL"
        ):
            nxt(bad)


def test_update_epic_roadmap_section_is_optional_bounded_text():
    """The managed roadmap section the controller will write into the EPIC
    body (#13): absent, ``null`` or blank is "none returned" (whether one
    was required is the engine's decision); a present one is a bounded
    multi-line string without a controller marker."""

    def res(**fields):
        return UpdateEpicResult.from_payload(
            {
                "phase": "UPDATE_EPIC",
                "status": "success",
                "next_issue_url": None,
                "progress": "Issue #2 merged.",
                **fields,
            }
        )

    assert res().roadmap_section is None
    assert res(roadmap_section=None).roadmap_section is None
    assert res(roadmap_section="  \n").roadmap_section is None
    assert res(roadmap_section="\n## Roadmap\n- [x] #1\n\t- note\n").roadmap_section == (
        "## Roadmap\n- [x] #1\n\t- note"
    )
    assert res(roadmap_section="x" * MAX_ROADMAP_SECTION_CHARS).roadmap_section == (
        "x" * MAX_ROADMAP_SECTION_CHARS
    )
    with pytest.raises(ControlResultValidationError, match="must be a string when present"):
        res(roadmap_section=["- a"])
    over = "x" * (MAX_ROADMAP_SECTION_CHARS + 1)
    with pytest.raises(ControlResultValidationError) as exc:
        res(roadmap_section=over)
    assert f"{MAX_ROADMAP_SECTION_CHARS + 1} characters" in str(exc.value)
    assert "xxxx" not in str(exc.value)  # size reported, text never echoed
    with pytest.raises(ControlResultValidationError, match="control character"):
        res(roadmap_section="a\x00b")
    # The refusal matches the scanner's opening (``autoforge.claims``): any
    # whitespace, none included, between ``<!--`` and ``ai-`` (R2-F1), so a
    # section the controller writes verbatim can never plant a claim.
    for marker in (
        "<!-- ai-controller-roadmap:start -->",
        "<!-- ai-controller-roadmap:end -->",
        '<!-- ai-follow-up: {"pr": "x"} -->',
        '<!--ai-follow-up: {"pr": "x", "finding_id": "R1-F1"} -->',
        '<!--  ai-follow-up: {"pr": "x"} -->',
        '<!--\nai-follow-up: {"pr": "x"} -->',
        "<!--\t \n ai-epic-progress: {} -->",
        "<!--ai-controller-roadmap:start -->",
    ):
        with pytest.raises(ControlResultValidationError, match="must not contain a controller"):
            res(roadmap_section=f"- a\n{marker}\n- b")
    # An ordinary HTML comment is content, not a marker, whatever its spacing.
    assert res(roadmap_section="<!-- note -->\n- a").roadmap_section == "<!-- note -->\n- a"
    assert res(roadmap_section="<!--note-->\n- a").roadmap_section == "<!--note-->\n- a"
    assert res(roadmap_section="<!--\nnote\n-->\n- a").roadmap_section == "<!--\nnote\n-->\n- a"
    assert res(roadmap_section="- ai-follow-up is a phrase").roadmap_section == (
        "- ai-follow-up is a phrase"
    )


@pytest.mark.parametrize(
    "label,build",
    [
        (
            "FIX.resolutions[].follow_up_issue_url",
            lambda v: FixResult.from_payload(
                dict(
                    _FIX_TOP,
                    resolutions=[
                        {
                            "finding_id": "R1-F1",
                            "resolution": "follow_up_created",
                            "follow_up_issue_url": v,
                        }
                    ],
                )
            ),
        ),
        (
            "ANALYZE_EXECUTE.issue_url",
            lambda v: AnalyzeExecuteResult.from_payload(
                {"issue_url": v, "head_sha": SHA_A, "pr_title": "Add it", "pr_body": "Adds it."}
            ),
        ),
        (
            "UPDATE_EPIC.next_issue_url",
            lambda v: UpdateEpicResult.from_payload(
                {
                    "phase": "UPDATE_EPIC",
                    "status": "success",
                    "next_issue_url": v,
                    "progress": "Issue #2 merged.",
                }
            ),
        ),
    ],
    ids=["optional-issue", "required-issue", "nullable-issue"],
)
def test_url_fields_are_bounded_before_the_url_parser_quotes_them(label, build):
    """``validation`` quotes the URL in its error, and that error reaches the
    correction prompt and the run log, so the length is checked first."""
    prefix = "https://github.com/owner/repo/issues/"
    at_bound = prefix + "9" * (MAX_URL_CHARS - len(prefix))
    assert len(at_bound) == MAX_URL_CHARS
    # At the bound the size check is silent: the URL parser is what speaks
    # (an absurd issue number is still an issue URL; it is not a comment URL).
    try:
        build(at_bound)
    except ControlResultValidationError as exc:
        assert f"at most {MAX_URL_CHARS}" not in str(exc)
    over = at_bound + "9"
    with pytest.raises(ControlResultValidationError) as excinfo:
        build(over)
    msg = str(excinfo.value)
    assert f"is {MAX_URL_CHARS + 1} characters" in msg
    assert f"at most {MAX_URL_CHARS}" in msg
    assert "9999" not in msg


def test_fix_bounds_relate_to_the_review_and_persisted_bounds():
    """A FIX can never legitimately carry more resolutions than the controller
    accepted findings, and a persisted (redacted) rationale is never larger
    than a persisted resolution."""
    assert MAX_RESOLUTIONS_PER_FIX == MAX_FINDINGS_PER_REVIEW
    assert MAX_FIX_RATIONALE_CHARS * MAX_GROWTH_FACTOR <= loop_guard.MAX_REQUIRED_RESOLUTION_CHARS
    # The worst shape redaction can grow, filling the rationale bound exactly.
    unit = "HF_TOKEN=x;"
    rationale = (unit * (MAX_FIX_RATIONALE_CHARS // len(unit) + 1))[:MAX_FIX_RATIONALE_CHARS]
    res = _remote_fix_result(rationale=rationale)
    persisted = redact_dict(res.resolutions[0].to_dict())  # the engine's own path
    assert MAX_FIX_RATIONALE_CHARS < len(persisted["rationale"])
    assert len(persisted["rationale"]) <= loop_guard.MAX_REQUIRED_RESOLUTION_CHARS
    assert "HF_TOKEN=x" not in persisted["rationale"]


# -- control characters in finding and resolution text (#78) -----------------------
# A finding's one-line fields are rendered on one line of the FIX prompt
# through ``escape_inline``; the parser refuses at parse time what that
# renderer would otherwise have to escape, so an accepted value is shown as
# it was written. A multi-line field keeps newlines and tabs, nothing else.
_CONTROL_SAMPLES = {
    "nul": "\x00",
    "newline": "\n",
    "carriage_return": "\r",
    "tab": "\t",
    "escape": "\x1b",
    "delete": "\x7f",
    "next_line": "\x85",
    "line_separator": "\u2028",
    "paragraph_separator": "\u2029",
}
_PRINTABLE = "café — «quoted» 日本語 🙂 \\backslash `code` [x](y) #heading"


def _review_finding(**fields) -> Finding:
    finding = {"id": "R1-F1", "classification": "nit", "required_resolution": "fix it", **fields}
    return ReviewResult.from_payload(
        dict(GOOD_REVIEW, needs_fix_round=True, findings=[finding])
    ).findings[0]


@pytest.mark.parametrize("key", ["title", "location"])
@pytest.mark.parametrize("name", sorted(_CONTROL_SAMPLES), ids=str)
def test_a_one_line_finding_field_rejects_every_control_character(key, name):
    ch = _CONTROL_SAMPLES[name]
    with pytest.raises(ControlResultValidationError) as excinfo:
        _review_finding(**{key: f"abc{ch}def"})
    msg = str(excinfo.value)
    assert f"REVIEW: finding R1-F1 field {key!r} contains a control character" in msg
    assert f"(U+{ord(ch):04X} at index 3)" in msg
    assert "one line of printable text" in msg
    assert "abc" not in msg and ch not in msg  # code point and index, never the text


@pytest.mark.parametrize("key", ["title", "location"])
def test_a_one_line_finding_field_accepts_printable_unicode(key):
    assert getattr(_review_finding(**{key: _PRINTABLE}), key) == _PRINTABLE


def test_one_line_acceptance_is_exactly_what_escape_inline_leaves_alone():
    """The parser refuses precisely the characters the renderer would escape:
    one class, defined once in ``prompts``, so the two cannot drift."""
    sampled = [chr(cp) for cp in range(0x0000, 0x0300)]
    sampled += [chr(cp) for cp in range(0x2000, 0x2070)]
    sampled += [chr(cp) for cp in (0xFEFF, 0x3000, 0x1F642, 0xE000, 0xFFFD, 0x10FFFF)]
    for ch in sampled:
        text = f"a{ch}b"
        try:
            _review_finding(title=text)
            accepted = True
        except ControlResultValidationError as exc:
            assert "control character" in str(exc), (hex(ord(ch)), str(exc))
            accepted = False
        assert accepted == (escape_inline(text) == text), hex(ord(ch))
        assert accepted == (CONTROL_CHAR_RE.search(ch) is None), hex(ord(ch))


@pytest.mark.parametrize("name", sorted(_CONTROL_SAMPLES), ids=str)
def test_required_resolution_keeps_newlines_and_tabs_and_nothing_else(name):
    ch = _CONTROL_SAMPLES[name]
    text = f"first{ch}second"
    if ch in ("\n", "\t"):
        assert _review_finding(required_resolution=text).required_resolution == text
        return
    with pytest.raises(ControlResultValidationError) as excinfo:
        _review_finding(required_resolution=text)
    msg = str(excinfo.value)
    assert "REVIEW: finding R1-F1 field 'required_resolution' contains a control character" in msg
    assert f"(U+{ord(ch):04X} at index 5)" in msg
    assert "only newlines and tabs are accepted inside 'required_resolution'" in msg
    assert "first" not in msg and "second" not in msg


@pytest.mark.parametrize("mode", ["REMOTE", "LOCAL"])
@pytest.mark.parametrize("name", sorted(_CONTROL_SAMPLES), ids=str)
def test_fix_rationale_keeps_newlines_and_tabs_and_nothing_else(mode, name):
    ch = _CONTROL_SAMPLES[name]
    text = f"The change is already covered by the existing suite;{ch}nothing to do."
    res = {"finding_id": "R1-F1", "resolution": "no_change_with_rationale", "rationale": text}
    stdout, wf_mode = _fix_with([res], mode)
    if ch in ("\n", "\t"):
        payload = parse_control_result(stdout, Phase.FIX, wf_mode)
        assert payload["resolutions"][0]["rationale"] == text
        return
    with pytest.raises(ControlResultValidationError) as excinfo:
        parse_control_result(stdout, Phase.FIX, wf_mode)
    msg = str(excinfo.value)
    assert "FIX: resolution for R1-F1 field 'rationale' contains a control character" in msg
    assert f"(U+{ord(ch):04X} at index" in msg
    assert "only newlines and tabs are accepted inside 'rationale'" in msg
    assert "existing suite" not in msg


def test_the_length_bound_speaks_before_the_control_character_rule():
    """An oversized value is refused for its size whatever it contains, so
    the index in a control-character message is always into a value of
    accepted size, and the size message never has to inspect the text."""
    over = "\x00" * (MAX_FINDING_TITLE_CHARS + 1)
    with pytest.raises(ControlResultValidationError) as excinfo:
        _review_finding(title=over)
    msg = str(excinfo.value)
    assert f"is {MAX_FINDING_TITLE_CHARS + 1} characters" in msg
    assert "control character" not in msg


def test_control_characters_are_refused_where_the_renderer_would_escape_them_only():
    """Fields outside findings keep their existing rules: a REVIEW ``message``
    or a LOCAL ``observations`` entry is quoted as a block or not rendered at
    all, and is out of scope for #78. A REMOTE prose section is published in
    the controller's review comment (#162), so it is multi-line text."""
    ReviewResult.from_payload(dict(GOOD_REVIEW, message="a\rb"))
    local = LocalReviewResult.from_payload(
        {
            "round": 1,
            "reviewed_workspace_fingerprint": "a" * 64,
            "needs_fix_round": False,
            "findings": [],
            "observations": ["line one\x1bline two"],
        }
    )
    assert local.observations == ["line one\x1bline two"]
    with pytest.raises(ControlResultValidationError) as excinfo:
        ReviewResult.from_payload(dict(GOOD_REVIEW, summary="line one\x1bline two"))
    msg = str(excinfo.value)
    assert "REVIEW: result field 'summary' contains a control character" in msg
    assert "(U+001B at index 8)" in msg
    assert "line one" not in msg


# -- whole-payload bound (#53) ---------------------------------------------------------------
def test_oversized_block_is_rejected_before_it_is_parsed():
    """The accepted payload is persisted whole (control-result.json, the
    events.jsonl line) and acted on by the next phase, so its size is bounded
    at parse time and an oversized block is refused, never clipped."""
    filler = "x" * (MAX_CONTROL_RESULT_CHARS + 1)
    payload = dict(GOOD_REVIEW, summary=filler)
    with pytest.raises(ControlResultValidationError) as excinfo:
        parse_control_result(block(payload), Phase.REVIEW)
    msg = str(excinfo.value)
    assert f"at most {MAX_CONTROL_RESULT_CHARS}" in msg and "characters" in msg
    assert "re-emit" in msg.lower()
    assert "xxxxxxxx" not in msg  # the oversized text is never echoed


def test_block_bound_is_checked_before_json_is_decoded():
    """A block past the bound is refused by size alone: it is not decoded
    (an oversized block full of junk still gets the size message)."""
    stdout = block("{" + "junk" * (MAX_CONTROL_RESULT_CHARS // 4 + 1))
    with pytest.raises(ControlResultValidationError, match="at most"):
        parse_control_result(stdout, Phase.REVIEW)


@pytest.mark.parametrize("mode", ["REMOTE", "LOCAL"])
def test_largest_review_the_field_bounds_accept_fits_the_block_bound(mode):
    """Pins the relation between the bounds: a REVIEW at every field bound,
    in the worst-case JSON encoding (non-ASCII text escaped as \\uXXXX, six
    characters per character), is still under the whole-block bound, so the
    field bounds -- not the block bound -- are what a reviewer is held to.
    A REMOTE review carries its prose sections too (#162), each at its own
    bound in the same encoding."""
    findings = [
        {
            "id": f"R1-F{n}",
            "classification": "blocked",
            "required_resolution": "汉" * MAX_FINDING_RESOLUTION_CHARS,
            "title": "汉" * MAX_FINDING_TITLE_CHARS,
            "location": "汉" * MAX_FINDING_LOCATION_CHARS,
        }
        for n in range(1, MAX_FINDINGS_PER_REVIEW + 1)
    ]
    if mode == "REMOTE":
        prose = {key: "汉" * limit for key, limit in REVIEW_PROSE_SECTIONS}
        stdout = block(dict(GOOD_REVIEW, needs_fix_round=True, findings=findings, **prose))
        wf_mode = WorkflowMode.REMOTE
    else:
        stdout, wf_mode = _review_with(findings, mode)
    assert "\\u6c49" in stdout  # json.dumps escaped the text: the worst case
    raw = stdout.split(BEGIN, 1)[1].split(END, 1)[0].strip()
    assert len(raw) <= MAX_CONTROL_RESULT_CHARS
    payload = parse_control_result(stdout, Phase.REVIEW, wf_mode)
    assert len(payload["findings"]) == MAX_FINDINGS_PER_REVIEW
    if mode == "REMOTE":
        sections = ReviewResult.from_payload(payload).sections
        assert {key: len(text) for key, text in sections.items()} == dict(REVIEW_PROSE_SECTIONS)


# -- published-content policy (ADR 0004 D8.2, D8.3, D8.5) -------------------
# Every value below that looks like a credential is an obviously fake
# placeholder.

_PROGRESS = "Issue #2 merged."
_ROADMAP = "## Roadmap\n- [x] #1 (PR #42)\n- [ ] #3"


def _epic(request=UpdateEpicRequest.FULL, **fields):
    return UpdateEpicResult.from_payload(
        {"phase": "UPDATE_EPIC", "status": "success", **fields}, request
    )


def _full(**fields):
    return _epic(**{"next_issue_url": None, "progress": _PROGRESS, **fields})


def _refusal(build) -> str:
    with pytest.raises(ControlResultValidationError) as excinfo:
        build()
    return str(excinfo.value)


_MARKER_OPENERS = [
    "<!-- ai-follow-up: {} -->",
    "<!--ai-x-->",
    "<!--\n\tai-x -->",
    "<!-- AI-x -->",
    "<!--Ai-epic-progress-->",
    "<!-- autoforge-replan-transaction: {} -->",
    "<!--autoforge-replan-close-->",
    "<!--  AutoForge-x -->",
    "<!--\nAUTOFORGE-x-->",
]


def test_agent_marker_opener_covers_both_prefixes_and_the_scanner_opener_is_unchanged():
    """D8.2: agent text may carry neither the controller's ``ai-`` claims
    nor the replan transaction's ``autoforge-`` markers. The scanner's own
    opening (``autoforge.claims`` builds on it) stays the ``ai-`` one."""
    for opener in _MARKER_OPENERS:
        assert AGENT_MARKER_OPEN_RE.search(opener), opener
    for text in ("<!-- note -->", "<!-- aim -->", "<!-- autoforged -->", "ai-follow-up", "<- ai-"):
        assert not AGENT_MARKER_OPEN_RE.search(text), text
    assert CONTROLLER_MARKER_OPEN_RE.pattern == r"<!--\s*+ai-"
    assert not CONTROLLER_MARKER_OPEN_RE.search("<!-- autoforge-replan-close -->")


@pytest.mark.parametrize("opener", _MARKER_OPENERS)
@pytest.mark.parametrize("key", ["progress", "roadmap_section"])
def test_marker_openers_are_refused_in_progress_and_roadmap_section(key, opener):
    msg = _refusal(lambda: _full(**{key: f"- a\n{opener}\n- b"}))
    assert "controller marker" in msg
    assert key in msg
    assert "follow-up" not in msg and "replan" not in msg  # never quoted


_CREDENTIALS = [
    ("env-assignment", "GH_TOKEN=FAKEtoken123", "FAKEtoken123"),
    ("authorization-header", "Authorization: Bearer FAKEbearer123", "FAKEbearer123"),
    ("url-userinfo", "https://user:FAKEpass123@example.com/x", "FAKEpass123"),
    ("jwt", "eyJhIjowfQ.e30.c2ln", "eyJhIjowfQ"),
    ("github-fine-grained-pat", "github_pat_FAKE0000000000", "FAKE0000000000"),
    ("github-token", "ghp_FAKE00000000", "FAKE00000000"),
    ("openai-key", "sk-proj-FAKE00000000", "FAKE00000000"),
    # ``sk-ant-`` keys are caught by the ``sk-`` pattern first.
    ("openai-key", "sk-ant-FAKE00000000", "FAKE00000000"),
    ("secret-assignment", "password=FAKEpassword00", "FAKEpassword00"),
    ("oauth-field", '"refresh": "FAKErefresh"', "FAKErefresh"),
    ("oauth-assignment", "refresh_token=FAKErefresh", "FAKErefresh"),
    ("oauth-bare-assignment", "access=FAKEaccess0000000000", "FAKEaccess0000000000"),
]


@pytest.mark.parametrize("cls,text,secret", _CREDENTIALS, ids=[c[1] for c in _CREDENTIALS])
def test_each_credential_class_is_refused_by_name_and_never_quoted(cls, text, secret):
    """D8.3: refuse, never redact; the refusal names the field and the
    pattern class, never the matched text."""
    for msg in (
        published_text_problem("progress", f"Done. {text} end"),
        _refusal(lambda: _full(progress=f"Done. {text} end")),
        _refusal(lambda: _full(roadmap_section=f"- [x] #1 {text}")),
    ):
        assert msg is not None
        assert cls in msg
        assert "never redacted" in msg
        assert secret not in msg


@pytest.mark.parametrize(
    "text",
    [
        "Fixes #3",
        "closes: owner/repo#3",
        "RESOLVED https://github.com/o/r/issues/3",
        "close #3",
        "Closed #3",
        "fix: #3",
        "FIXED o/r#3",
        "resolve https://github.com/o/r/pull/3",
        "resolves GH-3",
        "Merged.\nThis fixes #3 too.",
        # Fail closed: a code span or fence does not exempt a closing keyword.
        "`fixes #3`",
        "```\ncloses #3\n```",
    ],
)
def test_closing_keywords_are_refused_in_every_form(text):
    msg = published_text_problem("progress", text)
    assert msg is not None and "closing keyword" in msg
    assert "#3" not in msg and "GH-3" not in msg and "o/r" not in msg
    assert "closing keyword" in _refusal(lambda: _full(progress=text))


@pytest.mark.parametrize(
    "text",
    ["hotfix #3", "fixing #3", "closes the gap", "see #3", "resolved in review", _ROADMAP],
)
def test_text_without_a_closing_reference_passes(text):
    assert published_text_problem("progress", text) is None


@pytest.mark.parametrize(
    "text",
    [
        "@octocat",
        "(@octocat)",
        "@octo-org/team",
        "thanks @octocat",
        "line\n@octocat",
        "x,@octocat",
        # Fail closed where CommonMark could read the backticks otherwise:
        "`a\n@octocat`",  # a multi-line span is never exempt
        "\\`@octocat`",  # an escaped opener
        "| `a|@octocat` |",  # a GFM table splits cells before code spans
        "x `a\nb` @octocat `c`",  # an unpaired backtick on an earlier line
        # An indented fence may sit in a list item or quote. (A field is
        # judged after stripping, as stored, so the fence follows a line.)
        "done\n  ```\n@octocat\n  ```",
        "```\nx\n```\n@octocat",  # after a closed fence
    ],
)
def test_mentions_outside_code_are_refused(text):
    msg = published_text_problem("progress", text)
    assert msg is not None and "@-mention" in msg
    assert "code spans" in msg
    assert "octocat" not in msg
    assert "@-mention" in _refusal(lambda: _full(progress=text))


def test_crlf_line_breaks_open_and_close_fences():
    """UPDATE_EPIC fields refuse CR outright; other published text may carry it."""
    assert published_text_problem("body", "```\r\n@octocat\r\n```") is None
    assert published_text_problem("body", "```\r\nx\r\n```\r\n@octocat") is not None
    assert published_text_problem("body", "```\rx\r```\r@octocat") is not None


@pytest.mark.parametrize(
    "text",
    [
        "a@b.com",
        "mail admin@example.com",
        "`@octocat`",
        "use ``@octocat`` and `@octo-org/team`",
        "```\n@octocat\n```",
        "~~~\n@octocat\n~~~",
        "```python\nx = '@octocat'\n```",
        "````\n```\n@octocat\n````",  # a shorter fence does not close a longer one
        "```\n~~~\n@octocat",  # nor a different character; unclosed runs to the end
        "```\n@octocat",
        "`Vec<T>` and `@octocat`",  # HTML-looking text inside a span is code
        "@",
        "@ octocat",
    ],
)
def test_mentions_in_code_and_non_mentions_pass(text):
    assert published_text_problem("progress", text) is None
    assert _full(progress=text).progress == text


def test_raw_html_outside_code_turns_the_code_exemption_off():
    """GitHub reads backticks and fences inside an HTML block as text."""
    for text in ("<div>\n`@octocat`\n</div>", "<div>\n```\n@octocat\n```\n</div>"):
        msg = published_text_problem("progress", text)
        assert msg is not None and "raw HTML" in msg and "octocat" not in msg
    # Raw HTML without a mention is fine, and so is HTML inside a fence.
    assert published_text_problem("roadmap_section", "<!-- note -->\n- a") is None
    assert published_text_problem("progress", "```html\n<b>@octocat</b>\n```") is None


def test_rules_apply_in_order_marker_credential_closing_mention():
    marker, cred, closing, mention = "<!-- ai-x -->", "GH_TOKEN=FAKEtoken123", "fixes #3", "@o"
    assert "marker" in published_text_problem("f", f"{mention} {closing} {cred} {marker}")
    assert "credential" in published_text_problem("f", f"{mention} {closing} {cred}")
    assert "closing keyword" in published_text_problem("f", f"{mention} {closing}")


def test_payload_is_judged_whole_for_credentials():
    """D8.3: a field ending in ``GITHUB_TOKEN=`` passes alone, but in the
    rendered payload the redactor takes the next rendered word as its value."""
    field = "Implemented the parser. Set GITHUB_TOKEN="
    assert published_text_problem("body", field) is None
    assert published_payload_problem("PR body", field) is None
    msg = published_payload_problem("PR body", field + "\n\nCloses #5")
    assert msg is not None
    assert "PR body" in msg and "env-assignment" in msg
    assert "Closes" not in msg and "parser" not in msg
    # The payload check is the credential check only: the controller renders
    # the run's own closing keyword itself.
    assert published_payload_problem("PR body", "Body.\n\nCloses #5") is None


@pytest.mark.parametrize(
    ("fields", "composed"),
    [
        pytest.param(
            ("A `", "`@octocat`"),
            "- **R1-F1** [blocked] — A `\n  Required resolution: `@octocat`",
            id="backtick-paired-across-fields",
        ),
        pytest.param(
            ("A", "~~~\n@octocat\n~~~"),
            "- **R1-F1** [blocked] — A\n  Required resolution: ~~~\n  @octocat\n  ~~~",
            id="fence-behind-a-prefix",
        ),
        pytest.param(
            ("Rename it:\n~~~", "See\n~~~\n@octocat\n~~~"),
            "Rename it:\n~~~\n\n### R1-F2\n\nSee\n~~~\n@octocat\n~~~",
            id="fence-closed-by-a-later-field",
        ),
        pytest.param(
            ("<pre>", "Use `@octocat`."),
            "<pre>\n\n## Summary\n\nUse `@octocat`.",
            id="raw-html-before-a-later-field",
        ),
    ],
)
def test_markdown_composed_of_fields_is_judged_whole_for_mentions(fields, composed):
    """#162, D8.2: each field passes the mention rule alone, but the Markdown
    they compose shows the mention outside code (or beside raw HTML, where
    the code exemption is not trusted): a backtick in one field pairs with
    one in the next, a prefix pushes a fence off column 0, or one field's
    fence or HTML block runs into the next. Named by index, never quoted."""
    for field in fields:
        assert published_text_problem("field", field) is None
    msg = published_markdown_problem("review comment", composed)
    assert msg is not None and "the rendered 'review comment' contains an @-mention" in msg
    assert "each field may pass alone" in msg and "octocat" not in msg


def test_markdown_with_its_mentions_in_code_passes_whole():
    """Fields that are blocks of their own keep their code: a heading's
    unpaired backtick ends with its line, a fence at column 0 is a fence."""
    composed = (
        "### R1-F1 [blocked] — A `\n\nRequired resolution:\n\n`@octocat`\n\n"
        "### R1-F2 [nit]\n\nRequired resolution:\n\n~~~\n@octocat\n~~~\n\n"
        "## Summary\n\nWrite a@b.c, then ```@pytest.fixture```."
    )
    assert published_markdown_problem("review comment", composed) is None
    assert published_markdown_problem("review comment", "No mention at all.") is None


@pytest.mark.parametrize(
    "message",
    [
        "Fix the parser\n\nFixes #2",
        "closes: owner/repo#2",
        "Resolves Owner/Repo#2",
        "fixes https://github.com/owner/repo/issues/2",
        "Fixes GH-2",
        "See #3 and #4 for context.",
        "fixes #2; refs #3",
    ],
)
def test_commit_message_may_close_the_runs_own_issue(message):
    assert commit_message_problem(message, repository="owner/repo", issue_number=2) is None


@pytest.mark.parametrize(
    "message",
    [
        "Fixes #3",
        "Fixes #2, fixes #3",
        "closes other/repo#2",
        "resolved owner/repo#3",
        "Fixes https://github.com/owner/repo/issues/3",
        "Fixes https://github.com/other/repo/issues/2",
        # Fail closed: the run's own issue is named by its issue URL only.
        "Fixes https://github.com/owner/repo/pull/2",
        "Fixes #02",
    ],
)
def test_commit_message_closing_another_issue_is_refused(message):
    msg = commit_message_problem(message, repository="owner/repo", issue_number=2)
    assert msg is not None and "other than this run's own #2" in msg
    assert "#3" not in msg and "other/repo" not in msg and "#02" not in msg


def test_commit_message_with_a_credential_is_refused():
    msg = commit_message_problem(
        "Add CI\n\nexport GH_TOKEN=FAKEtoken123", repository="owner/repo", issue_number=2
    )
    assert msg is not None and "env-assignment" in msg and "FAKEtoken123" not in msg


def _mib(unit: str) -> str:
    return (unit * ((1 << 20) // len(unit) + 1))[: 1 << 20]


_ADVERSARIAL_SHAPES = {
    "every backtick run length once, unpaired": " ".join("`" * k for k in range(1, 1450)),
    "one long run, then single backticks": "`" * 1000 + " " + _mib("` "),
    "escaped backticks": _mib("\\` "),
    "fence after fence": _mib("```\n`a`\n"),
    "mentions in code spans": _mib("`@a` "),
    "raw HTML with code spans": _mib("<b> `@a` "),
    "carried backtick over spans": "`\n" + _mib("x `a` @b\n"),
    "keyword then whitespace": "fix" + " " * (1 << 20),
    "keyword then a long name": "Fixes " + "a" * (1 << 19) + "/" + "b" * (1 << 19),
    "keyword then URL prefixes": _mib("fixes https://github.com/"),
    "marker openers without a prefix": _mib("<!--" + " " * 100),
}


@pytest.mark.parametrize("text", _ADVERSARIAL_SHAPES.values(), ids=_ADVERSARIAL_SHAPES.keys())
def test_policy_scanners_are_linear_in_the_text(text):
    """Agent text is untrusted: no shape of a 1 MiB input may make a rule
    slow. A quadratic backtick pairing or a backtracking reference pattern
    would take minutes here; each scanner takes well under a second."""
    started = time.perf_counter()
    AGENT_MARKER_OPEN_RE.search(text)
    for _ in _CLOSING_REFERENCE_RE.finditer(text):
        pass
    _mention_problem("progress", text)
    assert time.perf_counter() - started < 3.0


def test_published_text_problem_is_linear_on_a_mixed_mebibyte():
    """The whole rule chain, credential scan included, on one 1 MiB input
    that reaches the last rule and passes it."""
    text = _mib("Done `@a` see #3, fix it ``x`` a@b.c\n```\n@b\n```\n")
    started = time.perf_counter()
    assert published_text_problem("progress", text) is None
    assert commit_message_problem(text, repository="owner/repo", issue_number=2) is None
    assert time.perf_counter() - started < 6.0


# -- REVIEW prose and the published-content policy (#162) --------------------
# The controller renders the round's review comment from a REMOTE result and
# posts it itself: the reviewer's prose sections stand under fixed headings,
# and each finding's title, location and required resolution are shown. All
# of it is published as given, so it is refused, never clipped or rewritten.

_PROSE_KEYS = [key for key, _ in REVIEW_PROSE_SECTIONS]
_FINDING_TEXT_KEYS = ["title", "location", "required_resolution"]


def _review_text(key: str, text: str) -> ReviewResult:
    """A REMOTE review whose prose section or R1-F1 field ``key`` is ``text``."""
    if key in _FINDING_TEXT_KEYS:
        finding = dict(_finding(1, 1), **{key: text})
        return ReviewResult.from_payload(
            dict(GOOD_REVIEW, needs_fix_round=True, findings=[finding])
        )
    return ReviewResult.from_payload(dict(GOOD_REVIEW, **{key: text}))


def _review_text_of(res: ReviewResult, key: str) -> str:
    if key in _FINDING_TEXT_KEYS:
        return getattr(res.findings[0], key)
    return res.sections[key]


def test_remote_review_carries_the_prose_it_publishes_and_names_no_comment():
    """The result carries the six prose sections, stripped and in the order
    the comment renders them; a comment URL a reviewer still reports is not
    read, since the reviewer posts no comment to name."""
    assert REVIEW_PROSE_SECTIONS == (
        ("spec", MAX_REVIEW_SECTION_CHARS),
        ("standards", MAX_REVIEW_SECTION_CHARS),
        ("assessment", MAX_REVIEW_SECTION_CHARS),
        ("observations", MAX_REVIEW_SECTION_CHARS),
        ("verification", MAX_REVIEW_SECTION_CHARS),
        ("summary", MAX_REVIEW_SUMMARY_CHARS),
    )
    res = ReviewResult.from_payload(
        dict(GOOD_REVIEW, spec="  Does what #2 asks.\n", review_comment_url=comment_url(PR, 1))
    )
    assert not hasattr(res, "review_comment_url")
    assert list(res.sections) == _PROSE_KEYS
    assert res.sections == dict(REVIEW_PROSE, spec="Does what #2 asks.")


@pytest.mark.parametrize("key", _PROSE_KEYS)
def test_each_prose_section_is_required_non_blank_text(key):
    absent = {k: v for k, v in GOOD_REVIEW.items() if k != key}
    with pytest.raises(ControlResultValidationError, match=f"missing required field {key!r}"):
        parse_control_result(block(absent), Phase.REVIEW)
    for blank in (None, "", " \n\t "):
        msg = _refusal(lambda blank=blank: _review_text(key, blank))
        assert f"CONTROL_RESULT for REVIEW missing required field {key!r}" in msg
    for bad in NON_STRING_JSON_VALUES:
        msg = _refusal(lambda bad=bad: ReviewResult.from_payload(dict(GOOD_REVIEW, **{key: bad})))
        assert f"REVIEW: field {key!r} must be a string" in msg


@pytest.mark.parametrize("key,limit", REVIEW_PROSE_SECTIONS, ids=_PROSE_KEYS)
def test_each_prose_section_is_bounded_and_rejected_not_clipped(key, limit):
    exact = "x" * limit
    assert _review_text(key, exact).sections[key] == exact
    # The bound applies to the stripped value the controller publishes.
    assert _review_text(key, f"\n  {exact}  \n").sections[key] == exact
    msg = _refusal(lambda: _review_text(key, "y" * (limit + 1)))
    assert f"REVIEW: result field {key!r} is {limit + 1} characters" in msg
    assert f"at most {limit}" in msg
    assert "yyyy" not in msg


@pytest.mark.parametrize("name", sorted(_CONTROL_SAMPLES), ids=str)
def test_each_prose_section_keeps_newlines_and_tabs_and_nothing_else(name):
    ch = _CONTROL_SAMPLES[name]
    text = f"first{ch}second"
    for key in _PROSE_KEYS:
        if ch in ("\n", "\t"):
            assert _review_text(key, text).sections[key] == text
            continue
        msg = _refusal(lambda key=key: _review_text(key, text))
        assert f"REVIEW: result field {key!r} contains a control character" in msg
        assert f"(U+{ord(ch):04X} at index 5)" in msg
        assert "first" not in msg and "second" not in msg


# (text, what the refusal names, what it must never quote)
_PUBLISHED_VIOLATIONS = {
    "marker": ("see <!-- ai-review-result: {} --> here", "controller marker", "review-result"),
    "mention": ("thanks @octocat", "@-mention", "octocat"),
    "closing": ("this Closes #3 too", "closing keyword", "#3"),
    "credential": ("set GH_TOKEN=FAKEtoken123 first", "env-assignment", "FAKEtoken123"),
}


@pytest.mark.parametrize(
    "key,rule",
    [
        (key, rule)
        for key in _PROSE_KEYS + _FINDING_TEXT_KEYS
        for rule in _PUBLISHED_VIOLATIONS
        # A location is shown in a code span, where a mention notifies no one
        # (see the location test below).
        if (key, rule) != ("location", "mention")
    ],
)
def test_review_prose_and_finding_text_are_held_to_the_published_policy(key, rule):
    """A violation is refused (the reviewer is asked again) and the refusal
    names the field and the rule, never the text."""
    text, names, secret = _PUBLISHED_VIOLATIONS[rule]
    msg = _refusal(lambda: _review_text(key, text))
    subject = f"R1-F1.{key}" if key in _FINDING_TEXT_KEYS else key
    assert f"REVIEW: field {subject!r} contains" in msg
    assert names in msg
    assert secret not in msg


@pytest.mark.parametrize("cls,text,secret", _CREDENTIALS, ids=[c[1] for c in _CREDENTIALS])
def test_each_credential_class_is_refused_in_review_text(cls, text, secret):
    for key in ("summary", "title", "location", "required_resolution"):
        msg = _refusal(lambda key=key: _review_text(key, f"see {text} end"))
        assert cls in msg and "never redacted" in msg
        assert secret not in msg


@pytest.mark.parametrize("key", [k for k in _PROSE_KEYS + _FINDING_TEXT_KEYS if k != "location"])
def test_a_mention_in_code_is_accepted_and_a_closing_reference_in_code_is_not(key):
    """The policy's code exemption is the mention's alone: GitHub notifies no
    one for ``@name`` in code, but closes an issue a closing keyword names
    even there (fail closed)."""
    for text in ("ask `@octocat` about it", "use ``@octo-org/team``"):
        assert _review_text_of(_review_text(key, text), key) == text
    msg = _refusal(lambda: _review_text(key, "see `Closes #3`"))
    assert "closing keyword" in msg and "#3" not in msg


def test_a_prose_section_may_quote_a_mention_in_a_fenced_block():
    text = "Ran:\n```\ngit log --author=@octocat\n```"
    assert _review_text("observations", text).sections["observations"] == text
    assert "@-mention" in _refusal(lambda: _review_text("observations", text + "\n@octocat"))


def test_a_location_is_judged_as_the_comment_renders_it_in_a_code_span():
    """The comment shows a location as a code span, so a mention there
    notifies no one and is accepted as written; the rules code does not
    exempt still refuse it."""
    for text in ("@octocat", "src/@scope/pkg/index.ts:3", "a `@b` c"):
        assert _review_text("location", text).findings[0].location == text
    for text, names in (
        ("Closes #3", "closing keyword"),
        ("<!-- ai-x -->", "controller marker"),
        ("GH_TOKEN=FAKEtoken123", "credential-shaped"),
    ):
        msg = _refusal(lambda text=text: _review_text("location", text))
        assert "R1-F1.location" in msg and names in msg
        assert "FAKEtoken123" not in msg and "#3" not in msg


def test_local_review_reports_no_prose_sections():
    """LOCAL mode is unchanged by #162: a local review is published nowhere and
    carries no prose sections, while a REMOTE review without them is refused."""
    payload = parse_control_result(_local_review({}), Phase.REVIEW, WorkflowMode.LOCAL)
    assert not set(payload) & set(_PROSE_KEYS)
    remote = {k: v for k, v in GOOD_REVIEW.items() if k not in _PROSE_KEYS}
    with pytest.raises(ControlResultValidationError, match="missing required field 'spec'"):
        parse_control_result(block(remote), Phase.REVIEW, WorkflowMode.REMOTE)


# -- UPDATE_EPIC request schemas (ADR 0004 D4.7, D8.1) -----------------------


def test_full_update_epic_requires_progress():
    res = _full(roadmap_section=_ROADMAP, next_issue_url=ISSUE)
    assert (res.progress, res.roadmap_section, res.next_issue_url) == (_PROGRESS, _ROADMAP, ISSUE)
    for absent in ({}, {"progress": None}, {"progress": ""}, {"progress": " \n "}):
        msg = _refusal(lambda absent=absent: _epic(next_issue_url=None, **absent))
        assert "missing required field 'progress'" in msg
    assert "must be a string" in _refusal(lambda: _full(progress=["x"]))
    # FULL ignores unknown keys, as every phase schema does.
    assert _full(extra="x").progress == _PROGRESS


def test_progress_is_bounded_multi_line_text():
    assert _full(progress="x" * MAX_PROGRESS_CHARS).progress == "x" * MAX_PROGRESS_CHARS
    msg = _refusal(lambda: _full(progress="x" * (MAX_PROGRESS_CHARS + 1)))
    assert f"{MAX_PROGRESS_CHARS + 1} characters" in msg and "xxxx" not in msg
    assert "control character" in _refusal(lambda: _full(progress="a\x00b"))
    assert _full(progress="a\n\tb").progress == "a\n\tb"
    # The progress comment, with the controller's marker, stays far under
    # GitHub's comment limit.
    assert 4 * MAX_PROGRESS_CHARS <= 65536


@pytest.mark.parametrize(
    "text,index",
    [
        ("Merged in https://github.com/owner/repo/pull/42.", 15),
        ("See http://example.test/x", 8),
        ("HTTPS://GITHUB.COM/owner/repo/pull/42", 5),
        ("Docs at www.example.test", 8),
        ("Docs at WWW.example.test", 8),
        ("Logs: ftp://host/path", 9),
        ("Run `git clone ssh://host/repo`", 18),
        ("```\nhttps://github.com/owner/repo/pull/42\n```", 9),
        ("(www.example.test)", 1),
        ("- _www.example.test_", 3),
    ],
)
def test_progress_with_a_url_is_refused_without_quoting_it(text, index):
    """#160: the progress text carries no URL, code included; the
    controller's marker names the issue and the PR."""
    msg = _refusal(lambda: _full(progress=text))
    assert f"field 'progress' contains a URL at index {index}" in msg
    assert "'#n'" in msg
    assert "example.test" not in msg and "github.com" not in msg
    # The field validator refuses it with the parser's message (D4.6).
    assert _refusal(lambda: validate_progress_text(text)) == msg


@pytest.mark.parametrize(
    "text",
    [
        "Issue #2 done in PR #42; `pytest` passes.",
        "Updated awww.ts and the www_root setting.",
        "Mail a@b.example about owner/repo#42.",
        "A ratio of 1:2, a path //srv/data, a scheme-less github.com/owner/repo.",
    ],
)
def test_progress_without_a_url_passes(text):
    assert _full(progress=text).progress == text


def test_only_progress_is_refused_for_a_url():
    """The roadmap section may link the EPIC's PRs; the URL rule is the
    progress comment's alone, and it runs after the shared rules."""
    linked = "## Roadmap\n- [x] #1 (https://github.com/owner/repo/pull/42)"
    assert _full(roadmap_section=linked).roadmap_section == linked
    msg = _refusal(lambda: _full(progress="Done, @octocat: https://example.test"))
    assert "@-mention" in msg and "URL" not in msg


_RE_REQUESTS = [
    # request, a payload of its own schema, a key outside it
    (UpdateEpicRequest.SELECTION, {"next_issue_url": ISSUE}, "roadmap_section"),
    (
        UpdateEpicRequest.SELECTION_WITH_ROADMAP,
        {"next_issue_url": None, "roadmap_section": _ROADMAP},
        "progress",
    ),
    (UpdateEpicRequest.ROADMAP, {"roadmap_section": _ROADMAP}, "next_issue_url"),
]


@pytest.mark.parametrize("request_, own, outside", _RE_REQUESTS, ids=lambda v: str(v)[:24])
def test_each_re_request_accepts_its_schema_and_refuses_any_other_key(request_, own, outside):
    res = _epic(request_, message="re-selected", **own)
    assert res.progress is None
    assert res.next_issue_url == own.get("next_issue_url")
    assert res.roadmap_section == own.get("roadmap_section")
    for extra in ({outside: None}, {"progress": None}, {"progress": _PROGRESS}):
        msg = _refusal(lambda extra=extra: _epic(request_, **own, **extra))
        for key in extra:
            assert repr(key) in msg
        assert "published progress comment is already done" in msg
        assert "asks only for" in msg
        assert _PROGRESS not in msg


def test_re_request_counts_unknown_keys_without_quoting_them():
    msg = _refusal(
        lambda: _epic(
            UpdateEpicRequest.SELECTION,
            next_issue_url=None,
            progress=None,
            **{"GH_TOKEN=FAKEtoken123": 1, "other": 2},
        )
    )
    assert "'progress'" in msg and "2 other keys" in msg
    assert "FAKEtoken123" not in msg and "'other'" not in msg


def test_re_request_required_fields():
    for request_ in (UpdateEpicRequest.SELECTION, UpdateEpicRequest.SELECTION_WITH_ROADMAP):
        assert "'next_issue_url'" in _refusal(lambda request_=request_: _epic(request_))
        assert _epic(request_, next_issue_url="").next_issue_url is None  # epic complete
        assert _epic(request_, next_issue_url=None).next_issue_url is None
    assert (
        _epic(UpdateEpicRequest.SELECTION_WITH_ROADMAP, next_issue_url=None).roadmap_section is None
    )
    for blank in ({}, {"roadmap_section": None}, {"roadmap_section": " \n"}):
        msg = _refusal(lambda blank=blank: _epic(UpdateEpicRequest.ROADMAP, **blank))
        assert "missing required field 'roadmap_section'" in msg
    # The section's own rules hold in every mode.
    msg = _refusal(lambda: _epic(UpdateEpicRequest.ROADMAP, roadmap_section="- @octocat"))
    assert "@-mention" in msg


def test_update_epic_request_is_threaded_through_the_parser():
    payload = {"phase": "UPDATE_EPIC", "status": "success", "next_issue_url": None}
    with pytest.raises(ControlResultValidationError, match="'progress'"):
        parse_control_result(block(payload), Phase.UPDATE_EPIC)
    sel = UpdateEpicRequest.SELECTION
    assert parse_control_result(block(payload), Phase.UPDATE_EPIC, update_epic_request=sel) == (
        payload
    )
    validate_for_phase(Phase.UPDATE_EPIC, payload, update_epic_request=sel)
    with_progress = dict(payload, progress=_PROGRESS)
    with pytest.raises(ControlResultValidationError, match="already done"):
        parse_control_result(block(with_progress), Phase.UPDATE_EPIC, update_epic_request=sel)
    with pytest.raises(ControlResultValidationError, match="already done"):
        validate_for_phase(Phase.UPDATE_EPIC, with_progress, update_epic_request=sel)
    validate_for_phase(Phase.UPDATE_EPIC, with_progress)  # FULL by default


@pytest.mark.parametrize(
    "validator,key,bad",
    [
        (validate_progress_text, "progress", "@octocat"),
        (validate_progress_text, "progress", "x" * (MAX_PROGRESS_CHARS + 1)),
        (validate_progress_text, "progress", "  "),
        (validate_progress_text, "progress", "See https://example.test"),
        (validate_roadmap_section, "roadmap_section", "<!-- ai-x -->"),
        (validate_roadmap_section, "roadmap_section", "<!-- autoforge-x -->"),
        (validate_roadmap_section, "roadmap_section", "a\x00b"),
        (validate_next_issue_url, "next_issue_url", "not a url"),
        (validate_next_issue_url, "next_issue_url", PR),
    ],
)
def test_field_validators_apply_the_parsers_rules_with_its_messages(validator, key, bad):
    """D4.6: a persisted value is re-validated under the parser's rules."""
    with pytest.raises(ControlResultValidationError) as from_validator:
        validator(bad)
    with pytest.raises(ControlResultValidationError) as from_payload:
        _full(**{key: bad})
    assert str(from_validator.value) == str(from_payload.value)


def test_field_validators_return_accepted_text():
    assert validate_progress_text(_PROGRESS) == _PROGRESS
    assert validate_roadmap_section(_ROADMAP) == _ROADMAP
    assert validate_next_issue_url(ISSUE) == ISSUE
