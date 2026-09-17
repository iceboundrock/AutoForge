"""CONTROL_RESULT parser boundary cases (§13)."""

import json

import pytest

from autoforge import loop_guard
from autoforge.errors import ControlResultError, ControlResultValidationError
from autoforge.prompts import CONTROL_CHAR_RE, escape_inline
from autoforge.redaction import MAX_GROWTH_FACTOR, redact_dict
from autoforge.result_parser import (
    BEGIN,
    END,
    MAX_CONTROL_RESULT_CHARS,
    MAX_FINDING_ID_CHARS,
    MAX_FINDING_LOCATION_CHARS,
    MAX_FINDING_RESOLUTION_CHARS,
    MAX_FINDING_TITLE_CHARS,
    MAX_FINDINGS_PER_REVIEW,
    MAX_FIX_RATIONALE_CHARS,
    MAX_RESOLUTIONS_PER_FIX,
    MAX_URL_CHARS,
    AnalyzeExecuteResult,
    Finding,
    FixResult,
    LocalFixResult,
    ReviewResult,
    UpdateEpicResult,
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


def test_fix_finding_id_is_bounded_at_parse_time():
    """A FIX finding_id can only ever match a bounded REVIEW id, so it is
    refused at parse rather than echoed back by the engine's coverage check."""
    exact = _id_of_length(MAX_FINDING_ID_CHARS)
    over = _id_of_length(MAX_FINDING_ID_CHARS + 1)
    remote = {
        "phase": "FIX",
        "status": "success",
        "previous_head_sha": SHA_A,
        "new_head_sha": SHA_B,
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
    res = ReviewResult.from_payload(dict(GOOD_REVIEW, needs_fix_round=True, findings=findings))
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
_FIX_TOP = {"phase": "FIX", "status": "success", "previous_head_sha": SHA_A, "new_head_sha": SHA_B}
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


def test_fix_commit_sha_must_be_a_sha_when_present():
    for sha in (SHA_B, SHA_B[:7], SHA_B.upper()):
        assert _remote_fix_result(commit_sha=sha).resolutions[0].commit_sha == sha.lower()
    for absent in ({}, {"commit_sha": None}, {"commit_sha": ""}, {"commit_sha": "  "}):
        assert _remote_fix_result(**absent).resolutions[0].commit_sha == ""
    with pytest.raises(ControlResultValidationError, match="'commit_sha' must be a git SHA"):
        _remote_fix_result(commit_sha="not-a-sha")
    with pytest.raises(ControlResultValidationError, match="'commit_sha' must be a git SHA"):
        _remote_fix_result(commit_sha=SHA_B + "0")  # 41 hex characters is not a SHA


@pytest.mark.parametrize(
    "label,build",
    [
        ("FIX.resolutions[].commit_sha", lambda v: _remote_fix_result(commit_sha=v)),
        (
            "FIX.new_head_sha",
            lambda v: FixResult.from_payload(dict(_FIX_TOP, new_head_sha=v, resolutions=[])),
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
            {"phase": "UPDATE_EPIC", "status": "success", "next_issue_url": url}
        )

    assert nxt(ISSUE).next_issue_url == ISSUE
    assert nxt(None).next_issue_url is None
    assert nxt("").next_issue_url is None
    for bad in ("not a url", PR, ISSUE + "?x=1", "http://github.com/o/r/issues/1"):
        with pytest.raises(
            ControlResultValidationError, match="'next_issue_url' must be a GitHub issue URL"
        ):
            nxt(bad)


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
                {"issue_url": v, "pr_url": PR, "head_sha": SHA_A, "branch": BRANCH}
            ),
        ),
        (
            "REVIEW.review_comment_url",
            lambda v: ReviewResult.from_payload(dict(GOOD_REVIEW, review_comment_url=v)),
        ),
        (
            "UPDATE_EPIC.next_issue_url",
            lambda v: UpdateEpicResult.from_payload(
                {"phase": "UPDATE_EPIC", "status": "success", "next_issue_url": v}
            ),
        ),
    ],
    ids=["optional-issue", "required-issue", "required-comment", "nullable-issue"],
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
    """Fields outside findings keep their existing rules: a REVIEW
    ``summary``/``message`` or a LOCAL ``observations`` entry is quoted as a
    block or not rendered at all, and is out of scope for #78."""
    payload = dict(GOOD_REVIEW, summary="line one\x1bline two", message="a\rb")
    ReviewResult.from_payload(payload)


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
    field bounds -- not the block bound -- are what a reviewer is held to."""
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
    stdout, wf_mode = _review_with(findings, mode)
    assert "\\u6c49" in stdout  # json.dumps escaped the text: the worst case
    raw = stdout.split(BEGIN, 1)[1].split(END, 1)[0].strip()
    assert len(raw) <= MAX_CONTROL_RESULT_CHARS
    payload = parse_control_result(stdout, Phase.REVIEW, wf_mode)
    assert len(payload["findings"]) == MAX_FINDINGS_PER_REVIEW
