"""Transitions: every legal edge passes, every illegal edge raises."""

import pytest

from autoforge.errors import StateTransitionError
from autoforge.transitions import Phase, decide_next_phase, is_legal, validate_transition


def test_all_spec_legal_edges():
    assert is_legal(Phase.INITIALIZING, Phase.ANALYZE_EXECUTE)
    assert is_legal(Phase.ANALYZE_EXECUTE, Phase.REVIEW)
    assert is_legal(Phase.REVIEW, Phase.FIX)
    assert is_legal(Phase.REVIEW, Phase.REPLAN_REEXECUTE)
    assert is_legal(Phase.REVIEW, Phase.READY_FOR_MERGE)
    assert is_legal(Phase.REVIEW, Phase.REVIEW)  # stale review (HEAD moved) re-reviews
    assert is_legal(Phase.READY_FOR_MERGE, Phase.MERGE)
    assert is_legal(Phase.READY_FOR_MERGE, Phase.REVIEW)
    assert not is_legal(Phase.REVIEW, Phase.MERGE)  # no direct path: READY_FOR_MERGE gate
    assert is_legal(Phase.FIX, Phase.REVIEW)
    assert is_legal(Phase.REPLAN_REEXECUTE, Phase.REVIEW)
    assert is_legal(Phase.MERGE, Phase.ANALYZE_EXECUTE)
    assert is_legal(Phase.MERGE, Phase.UPDATE_EPIC)
    assert is_legal(Phase.MERGE, Phase.DONE)
    assert is_legal(Phase.MERGE, Phase.REVIEW)
    assert is_legal(Phase.UPDATE_EPIC, Phase.ANALYZE_EXECUTE)
    assert is_legal(Phase.UPDATE_EPIC, Phase.DONE)


@pytest.mark.parametrize(
    "frm,to",
    [
        (Phase.INITIALIZING, Phase.REVIEW),
        (Phase.INITIALIZING, Phase.DONE),
        (Phase.ANALYZE_EXECUTE, Phase.FIX),
        (Phase.ANALYZE_EXECUTE, Phase.MERGE),
        (Phase.ANALYZE_EXECUTE, Phase.DONE),
        (Phase.REVIEW, Phase.DONE),
        (Phase.REVIEW, Phase.MERGE),
        (Phase.READY_FOR_MERGE, Phase.DONE),
        (Phase.READY_FOR_MERGE, Phase.FIX),
        (Phase.ANALYZE_EXECUTE, Phase.READY_FOR_MERGE),
        (Phase.REVIEW, Phase.ANALYZE_EXECUTE),
        (Phase.REVIEW, Phase.UPDATE_EPIC),
        (Phase.FIX, Phase.MERGE),
        (Phase.FIX, Phase.DONE),
        (Phase.MERGE, Phase.FIX),
        (Phase.MERGE, Phase.BLOCKED),
        (Phase.UPDATE_EPIC, Phase.MERGE),
        (Phase.UPDATE_EPIC, Phase.FIX),
        (Phase.DONE, Phase.ANALYZE_EXECUTE),
        (Phase.BLOCKED, Phase.REVIEW),
        (Phase.FAILED, Phase.INITIALIZING),
    ],
)
def test_illegal_edges_raise(frm, to):
    assert not is_legal(frm, to)
    with pytest.raises(StateTransitionError, match="illegal transition"):
        validate_transition(frm, to)


def test_decide_review_routing():
    assert decide_next_phase(Phase.REVIEW, {"needs_fix_round": True}) == Phase.FIX
    assert decide_next_phase(Phase.REVIEW, {"needs_fix_round": False}) == Phase.READY_FOR_MERGE


def test_decide_merge_is_controller_owned():
    # No agent decides anything in MERGE: a verified controller merge routes to
    # UPDATE_EPIC; a stale HEAD after the clean review goes back to REVIEW.
    assert decide_next_phase(Phase.MERGE, {}) == Phase.UPDATE_EPIC
    assert decide_next_phase(Phase.MERGE, {"next_action": "DONE"}) == Phase.UPDATE_EPIC
    assert decide_next_phase(Phase.MERGE, {"head_changed_after_review": True}) == Phase.REVIEW


def test_decide_update_epic():
    assert (
        decide_next_phase(Phase.UPDATE_EPIC, {"next_issue_url": "https://github.com/o/r/issues/3"})
        == Phase.ANALYZE_EXECUTE
    )
    assert decide_next_phase(Phase.UPDATE_EPIC, {"next_issue_url": None}) == Phase.DONE
    assert decide_next_phase(Phase.UPDATE_EPIC, {}) == Phase.DONE


def test_decide_terminal_raises():
    with pytest.raises(StateTransitionError, match="terminal"):
        decide_next_phase(Phase.DONE, {})
