"""Loop bounds (pure logic): review-round cap, stagnation rules, step budget."""

import pytest

from autoforge.loop_guard import (
    RESULT_CLEAN,
    RESULT_NEEDS_FIX,
    RESULT_STALE,
    findings_fingerprint,
    next_round_cap_reason,
    normalize_resolution,
    resolution_digests,
    review_record,
    round_cap_reason,
    stagnation_reason,
    step_budget_reason,
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
    assert rec == {
        "round": 3,
        "reviewed_head_sha": SHA_A,
        "result": "needs_fix",
        "finding_count": 2,
        "fingerprint": findings_fingerprint([_f("x"), _f("y")]),
        "resolutions": resolution_digests([_f("x"), _f("y")]),
    }
    assert len(rec["resolutions"]) == 2 and rec["resolutions"] == sorted(rec["resolutions"])
    # per-finding digests ignore ids/locations/whitespace/case and de-duplicate
    assert resolution_digests([_f(" X ", "R9-F9", "z.py"), _f("x")]) == resolution_digests(
        [_f("x")]
    )
    with pytest.raises(ValueError, match="unknown review result"):
        review_record(1, SHA_A, "merged", [])


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
