"""CONTROL_RESULT parser boundary cases (§13)."""

import json

import pytest

from autoforge.errors import ControlResultError, ControlResultValidationError
from autoforge.result_parser import (
    BEGIN,
    END,
    AnalyzeExecuteResult,
    Finding,
    FixResult,
    ReviewResult,
    parse_control_result,
)
from autoforge.transitions import Phase, WorkflowMode
from tests.conftest import BRANCH, ISSUE, PR, SHA_A, SHA_B, block, comment_url

GOOD_REVIEW = {
    "phase": "REVIEW",
    "status": "success",
    "round": 1,
    "reviewed_head_sha": SHA_A,
    "review_comment_url": comment_url(PR, 1),
    "needs_fix_round": False,
    "findings": [],
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
                "review_comment_url": comment_url(PR, 2),
                "needs_fix_round": True,
                "findings": [{"id": "R99-F1", "classification": "nit", "required_resolution": "x"}],
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
        "pr_url": PR,
        "head_sha": SHA_A.upper(),
        "branch": BRANCH,
    }
    assert parse_control_result(BEGIN + json.dumps(ae) + END, Phase.ANALYZE_EXECUTE) == ae
    parsed = AnalyzeExecuteResult.from_payload(ae)
    assert parsed.head_sha == SHA_A  # normalized to lowercase
    for missing in ("head_sha", "branch", "pr_url"):
        bad = dict(ae)
        del bad[missing]
        with pytest.raises(ControlResultValidationError, match=missing):
            parse_control_result(BEGIN + json.dumps(bad) + END, Phase.ANALYZE_EXECUTE)
    with pytest.raises(ControlResultValidationError, match="head_sha"):
        parse_control_result(
            BEGIN + json.dumps(dict(ae, head_sha="h")) + END, Phase.ANALYZE_EXECUTE
        )
    with pytest.raises(ControlResultValidationError, match="pr_url"):
        parse_control_result(BEGIN + json.dumps(dict(ae, pr_url="p")) + END, Phase.ANALYZE_EXECUTE)

    # MERGE is controller-executed: an agent "merged" claim is never accepted.
    merge = {"phase": "MERGE", "status": "success", "merged": True, "next_action": "UPDATE_EPIC"}
    with pytest.raises(ControlResultValidationError, match="executed by the controller"):
        parse_control_result(BEGIN + json.dumps(merge) + END, Phase.MERGE)

    epic = {"phase": "UPDATE_EPIC", "status": "success", "next_issue_url": None}
    assert parse_control_result(BEGIN + json.dumps(epic) + END, Phase.UPDATE_EPIC) == epic


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
    with pytest.raises(ControlResultValidationError, match="review_comment_url"):
        ReviewResult.from_payload(dict(base, review_comment_url=PR))
    with pytest.raises(ControlResultValidationError, match="round"):
        ReviewResult.from_payload(dict(base, round=0))


def test_fix_resolution_rules():
    base = {"phase": "FIX", "status": "success", "previous_head_sha": SHA_A, "new_head_sha": SHA_B}
    with pytest.raises(ControlResultValidationError, match="resolutions"):
        FixResult.from_payload(base)
    ok = FixResult.from_payload(
        dict(base, resolutions=[{"finding_id": "R1-F1", "resolution": "fixed"}])
    )
    assert ok.resolutions[0].resolution == "fixed" and ok.new_head_sha == SHA_B
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
    with pytest.raises(ControlResultValidationError, match="follow_up_issue_url"):
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
    base = {"phase": "FIX", "status": "success", "previous_head_sha": SHA_A, "new_head_sha": SHA_B}
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
            "new_head_sha": SHA_B,
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
        lambda v: block({"phase": "UPDATE_EPIC", "status": "success", "next_issue_url": v}),
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
    text = "https://github.com/owner/repo/issues/9" if "url" in label else "some text here ok"
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
