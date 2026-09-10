"""Loop bounds (pure logic): review-round cap, stagnation rules, step budget."""

import pytest

from autoforge.errors import StateError
from autoforge.loop_guard import (
    MAX_PERSISTED_RESOLUTION_DIGESTS,
    RESULT_CLEAN,
    RESULT_NEEDS_FIX,
    RESULT_STALE,
    findings_fingerprint,
    next_round_cap_reason,
    normalize_resolution,
    resolution_digests,
    review_record,
    round_cap_reason,
    round_evidence_is_complete,
    stagnation_reason,
    step_budget_reason,
    truncated_evidence_rounds,
    validate_review_history,
)
from tests.conftest import SHA_A


def _f(text: str, fid: str = "R1-F1", loc: str = "src/x.py:1") -> dict:
    return {"id": fid, "classification": "nit", "location": loc, "required_resolution": text}


def _hist(*specs: tuple[str, list[dict]]) -> list[dict]:
    return [review_record(i + 1, SHA_A, result, fs) for i, (result, fs) in enumerate(specs)]


# -- fingerprint ----------------------------------------------------------------
def test_normalize_collapses_whitespace_and_case():
    assert normalize_resolution("  Add a\n  regression   TEST ") == "add a regression test"
    assert normalize_resolution(None) == ""


def test_fingerprint_ignores_ids_locations_and_order():
    a = [_f("add a test", "R1-F1", "a.py"), _f("fix typo", "R1-F2", "b.py")]
    b = [_f("Fix  typo", "R2-F1", "c.py"), _f("ADD A TEST", "R2-F2", "d.py")]
    assert findings_fingerprint(a) == findings_fingerprint(b)
    assert findings_fingerprint(a) != findings_fingerprint([_f("add a test")])
    assert findings_fingerprint([]) == findings_fingerprint([])


def test_review_record_shape_and_result_validation():
    rec = review_record(3, SHA_A, RESULT_NEEDS_FIX, [_f("x"), _f("y", "R3-F2")])
    assert rec["round"] == 3
    assert rec["reviewed_head_sha"] == SHA_A
    assert rec["result"] == "needs_fix"
    assert rec["finding_count"] == 2
    assert rec["fingerprint"] == findings_fingerprint([_f("x"), _f("y")])
    assert rec["resolutions"] == resolution_digests([_f("x"), _f("y")])
    assert rec["resolutions_truncated"] is False
    assert rec["findings"] == [
        {"id": "R1-F1", "classification": "nit", "required_resolution": "x"},
        {"id": "R3-F2", "classification": "nit", "required_resolution": "y"},
    ]
    assert len(rec["resolutions"]) == 2 and rec["resolutions"] == sorted(rec["resolutions"])
    # per-finding digests ignore ids/locations/whitespace/case and de-duplicate
    assert resolution_digests([_f(" X ", "R9-F9", "z.py"), _f("x")]) == resolution_digests(
        [_f("x")]
    )
    with pytest.raises(ValueError, match="unknown review result"):
        review_record(1, SHA_A, "merged", [])


def test_review_record_bounds_retained_finding_evidence_and_marks_the_loss():
    findings = [_f("x" * 2500, f"R1-F{i}") for i in range(101)]
    record = review_record(1, SHA_A, RESULT_NEEDS_FIX, findings)
    assert record["finding_count"] == 101
    assert len(record["findings"]) == 100
    assert len(record["findings"][0]["required_resolution"]) == 2000
    # The bound stays, but the loss is never silent: a replan must be able to
    # see that this round's evidence can no longer be reproduced in full.
    assert record["evidence_truncated"] is True
    assert not round_evidence_is_complete(record)
    assert truncated_evidence_rounds([record]) == [1]


@pytest.mark.parametrize(
    "findings,truncated",
    [
        ([_f("x", "R1-F1")], False),
        ([_f("x" * 2000, "R1-F1")], False),  # exactly at the cap: nothing lost
        ([_f("x" * 2001, "R1-F1")], True),  # one clipped resolution is enough
        ([_f("x", f"R1-F{i}") for i in range(100)], False),
        ([_f("x", f"R1-F{i}") for i in range(101)], True),
    ],
)
def test_evidence_truncation_is_detected_per_round(findings, truncated):
    record = review_record(1, SHA_A, RESULT_NEEDS_FIX, findings)
    assert record.get("evidence_truncated", False) is truncated
    assert round_evidence_is_complete(record) is not truncated
    assert bool(truncated_evidence_rounds([record])) is truncated


def test_only_rounds_with_findings_carry_replan_evidence():
    """Clean/stale rounds are never collected for a replan, so never block one."""
    findings = [_f("x", f"R1-F{i}") for i in range(101)]
    for result in (RESULT_CLEAN, RESULT_STALE):
        record = review_record(1, SHA_A, result, findings)
        assert round_evidence_is_complete(record)
        assert truncated_evidence_rounds([record]) == []


def test_evidence_completeness_is_rechecked_against_the_finding_count():
    """A marker-less record (older history, hand-edited state) still fails closed."""
    record = review_record(4, SHA_A, RESULT_NEEDS_FIX, [_f("x"), _f("y", "R4-F2")])
    record.pop("evidence_truncated", None)
    record["findings"] = record["findings"][:1]
    assert not round_evidence_is_complete(record)
    assert truncated_evidence_rounds([record]) == [4]
    record["finding_count"] = "not-a-number"
    assert not round_evidence_is_complete(record)


# -- review-round cap ---------------------------------------------------------------
def test_round_cap_blocks_findings_at_cap_but_not_clean():
    assert round_cap_reason(5, 6, has_findings=True) == ""
    assert "max_review_rounds=6" in round_cap_reason(6, 6, has_findings=True)
    assert round_cap_reason(6, 6, has_findings=False) == ""
    assert "exceeds" in round_cap_reason(7, 6, has_findings=False)


def test_next_round_cap():
    assert next_round_cap_reason(5, 6) == ""
    assert "review round 7 is not started" in next_round_cap_reason(6, 6)
    assert next_round_cap_reason(0, 1) == ""
    assert next_round_cap_reason(1, 1)


# -- stagnation ---------------------------------------------------------------------------
def test_stagnation_identical_resolutions():
    hist = _hist((RESULT_NEEDS_FIX, [_f("add a test")]), (RESULT_NEEDS_FIX, [_f("Add A Test")]))
    reason = stagnation_reason(hist, identical_rounds=2, unchanged_count_rounds=0)
    assert "rounds 1, 2 requested identical resolutions" in reason
    # one round is not enough for a window of 2
    assert stagnation_reason(hist[:1], 2, 0) == ""
    # different texts: not identical
    other = _hist((RESULT_NEEDS_FIX, [_f("add a test")]), (RESULT_NEEDS_FIX, [_f("fix typo")]))
    assert stagnation_reason(other, 2, 0) == ""


def test_stagnation_unchanged_count_needs_a_recurring_resolution():
    # A/B/A ping-pong: the count never changes and "a" comes back in round 3.
    hist = _hist(
        (RESULT_NEEDS_FIX, [_f("a")]),
        (RESULT_NEEDS_FIX, [_f("b")]),
        (RESULT_NEEDS_FIX, [_f("A")]),
    )
    reason = stagnation_reason(hist, 0, 3)
    assert reason.startswith("review rounds 1, 2, 3 each ended with 1")
    assert "1 required resolution(s) recur" in reason
    assert stagnation_reason(hist, 0, 4) == ""
    # Three genuinely new findings (each earlier one resolved) are progress.
    fresh = _hist(
        (RESULT_NEEDS_FIX, [_f("a")]),
        (RESULT_NEEDS_FIX, [_f("b")]),
        (RESULT_NEEDS_FIX, [_f("c")]),
    )
    assert stagnation_reason(fresh, 0, 3) == ""
    # A recurring demand among otherwise new findings still counts.
    mixed = _hist(
        (RESULT_NEEDS_FIX, [_f("a"), _f("b", "R1-F2")]),
        (RESULT_NEEDS_FIX, [_f("c"), _f("d", "R2-F2")]),
        (RESULT_NEEDS_FIX, [_f("e"), _f("b", "R3-F2")]),
    )
    assert "recur" in stagnation_reason(mixed, 0, 3)
    # A changing count is never stagnation, recurring text or not.
    progress = _hist(
        (RESULT_NEEDS_FIX, [_f("a"), _f("a2", "R1-F2")]),
        (RESULT_NEEDS_FIX, [_f("a")]),
        (RESULT_NEEDS_FIX, [_f("a")]),
    )
    assert stagnation_reason(progress, 0, 3) == ""


def test_stagnation_unchanged_count_keeps_count_only_rule_for_legacy_history():
    """Entries persisted before per-finding digests existed cannot prove recurrence."""
    hist = _hist(
        (RESULT_NEEDS_FIX, [_f("a")]),
        (RESULT_NEEDS_FIX, [_f("b")]),
        (RESULT_NEEDS_FIX, [_f("c")]),
    )
    del hist[0]["resolutions"]
    reason = stagnation_reason(hist, 0, 3)
    assert "finding count has not changed" in reason and "older controller" in reason


def test_stagnation_only_counts_trailing_needs_fix_rounds():
    hist = _hist(
        (RESULT_NEEDS_FIX, [_f("a")]),
        (RESULT_STALE, [_f("a")]),
        (RESULT_NEEDS_FIX, [_f("a")]),
    )
    # the stale round in between breaks the streak for both rules
    assert stagnation_reason(hist, 2, 2) == ""
    hist = _hist((RESULT_NEEDS_FIX, [_f("a")]), (RESULT_CLEAN, []))
    assert stagnation_reason(hist, 1, 1) == ""


def test_stagnation_rules_can_be_disabled():
    hist = _hist((RESULT_NEEDS_FIX, [_f("a")]), (RESULT_NEEDS_FIX, [_f("a")]))
    assert stagnation_reason(hist, 0, 0) == ""
    assert stagnation_reason([], 2, 3) == ""


# -- step budget ---------------------------------------------------------------------------
def test_step_budget():
    assert step_budget_reason(0, 1) == ""
    assert step_budget_reason(299, 300) == ""
    assert "max_total_steps=300" in step_budget_reason(300, 300)
    assert step_budget_reason(301, 300)


# -- R2-F1: bounded recurrence evidence ------------------------------------------------------
def test_review_record_bounds_the_persisted_digests_and_marks_the_clip():
    """A verbose reviewer must not grow review_history without limit."""
    many = [_f(f"resolution {i}", f"R1-F{i + 1}") for i in range(MAX_PERSISTED_RESOLUTION_DIGESTS)]
    rec = review_record(1, SHA_A, RESULT_NEEDS_FIX, many)
    assert len(rec["resolutions"]) == MAX_PERSISTED_RESOLUTION_DIGESTS
    assert rec["resolutions_truncated"] is False

    over = many + [_f("one more", "R1-F999")]
    rec = review_record(1, SHA_A, RESULT_NEEDS_FIX, over)
    assert rec["finding_count"] == MAX_PERSISTED_RESOLUTION_DIGESTS + 1
    assert len(rec["resolutions"]) == MAX_PERSISTED_RESOLUTION_DIGESTS
    assert rec["resolutions_truncated"] is True
    # the kept digests are still the sorted prefix of the complete set
    assert rec["resolutions"] == resolution_digests(over)[:MAX_PERSISTED_RESOLUTION_DIGESTS]


def test_truncated_evidence_cannot_prove_the_absence_of_a_recurrence():
    """Clipped rounds fall back to the count-only rule instead of "no recurrence"."""
    fresh = [
        (RESULT_NEEDS_FIX, [_f(f"r{n}-{i}", f"R{n}-F{i + 1}") for i in range(60)])
        for n in (1, 2, 3)
    ]
    hist = _hist(*fresh)
    assert all(r["resolutions_truncated"] for r in hist)
    reason = stagnation_reason(hist, 0, 3)
    assert "persisted only the first" in reason and "recurrence cannot be ruled out" in reason
    # A recurrence that *is* visible in the kept digests is reported as such.
    shared = _f("same demand", "R9-F1")
    recurring = _hist(
        (RESULT_NEEDS_FIX, [shared] + [_f(f"a{i}", f"R1-F{i + 2}") for i in range(60)]),
        (RESULT_NEEDS_FIX, [shared] + [_f(f"b{i}", f"R2-F{i + 2}") for i in range(60)]),
    )
    # (whether the shared digest survives the clip depends on its sort position;
    # either way the round must not be reported as recurrence-free)
    assert stagnation_reason(recurring, 0, 2)


# -- R2-F2: an empty demand is not recurrence evidence ---------------------------------------
def test_blank_resolutions_get_no_digest_and_cannot_recur():
    assert resolution_digests([_f("   "), _f("\n\t", "R1-F2")]) == []
    assert resolution_digests([_f(" "), _f("real", "R1-F2")]) == resolution_digests([_f("real")])
    # blank / new demand / blank must not look like an A/B/A ping-pong
    hist = _hist(
        (RESULT_NEEDS_FIX, [_f("  ")]),
        (RESULT_NEEDS_FIX, [_f("a genuinely new demand")]),
        (RESULT_NEEDS_FIX, [_f("\t\n")]),
    )
    assert stagnation_reason(hist, 0, 3) == ""
    # ... while the identical-resolutions rule still catches two blank rounds
    assert stagnation_reason(hist[:1] + hist[2:], 2, 0)


# -- R1-F2: malformed persisted history fails loudly -----------------------------------------
def test_validate_review_history_accepts_records_and_legacy_entries():
    hist = _hist((RESULT_NEEDS_FIX, [_f("a")]), (RESULT_CLEAN, []))
    validate_review_history(hist)
    legacy = dict(hist[0])
    del legacy["resolutions"]
    del legacy["resolutions_truncated"]
    validate_review_history([legacy])  # a *missing* key is compatibility, not corruption
    assert "findings" not in hist[1]  # a clean round carries no retained evidence
    validate_review_history(
        [dict(hist[0], review_comment_url="https://x/1#c", timestamp="2026-01-01T00:00:00Z")]
    )


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda r: r.update(resolutions="a,b"), "resolutions must be a list"),
        (lambda r: r.update(resolutions={"a": 1}), "resolutions must be a list"),
        (lambda r: r.update(resolutions=None), "resolutions must be a list"),
        (lambda r: r.update(resolutions=[1, 2]), "non-empty digest strings"),
        (lambda r: r.update(resolutions=["ok", ""]), "non-empty digest strings"),
        (lambda r: r.update(resolutions=["ok", "  "]), "non-empty digest strings"),
        (lambda r: r.update(resolutions_truncated="yes"), "resolutions_truncated must be a bool"),
        (lambda r: r.update(round="3"), "round must be an integer"),
        (lambda r: r.update(round=True), "round must be an integer"),
        (lambda r: r.update(finding_count=None), "finding_count must be an integer"),
        (lambda r: r.update(result="needs-fix"), "result must be one of"),
        (lambda r: r.pop("result"), "result must be one of"),
        (lambda r: r.update(fingerprint=123), "fingerprint must be a string"),
        (lambda r: r.update(reviewed_head_sha=None), "reviewed_head_sha must be a string"),
        (lambda r: r.update(findings="R1-F1"), "findings must be a list of objects"),
        (lambda r: r.update(findings=3), "findings must be a list of objects"),
        (lambda r: r.update(findings=[{"id": "R1-F1"}, "R1-F2"]), "findings must be a list"),
        (lambda r: r.update(evidence_truncated="yes"), "evidence_truncated must be a bool"),
        (lambda r: r.update(review_comment_url=7), "review_comment_url must be a string"),
        (lambda r: r.update(timestamp=0), "timestamp must be a string"),
    ],
)
def test_malformed_history_entry_is_corruption_not_legacy_data(mutate, match):
    """A present malformed field must not be reinterpreted as an old entry."""
    hist = _hist((RESULT_NEEDS_FIX, [_f("a")]), (RESULT_NEEDS_FIX, [_f("b")]))
    mutate(hist[-1])
    with pytest.raises(StateError, match=match):
        validate_review_history(hist)
    # the loop guards refuse to decide on it as well
    with pytest.raises(StateError, match=match):
        stagnation_reason(hist, 2, 2)


def test_validate_review_history_rejects_non_entries():
    with pytest.raises(StateError, match="'review_history' must be a list"):
        validate_review_history({"round": 1})
    with pytest.raises(StateError, match=r"review_history\[0\]. must be an object"):
        validate_review_history(["round 1"])
