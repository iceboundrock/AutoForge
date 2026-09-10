"""Transitions: every legal edge passes, every illegal edge raises."""

import pytest

from autoforge.errors import StateTransitionError
from autoforge.transitions import (
    LEGAL_EDGES,
    TERMINAL_PHASES,
    Phase,
    decide_next_phase,
    is_legal,
    validate_transition,
)

# The topology, written out independently of ``LEGAL_EDGES`` so that a change
# to the implementation cannot silently redefine what "legal" means. Every
# other pair in ``Phase x Phase`` must be illegal, which the matrix below
# asserts exhaustively -- that is what keeps a new escape hatch out of
# REPLAN_REEXECUTE (or any other phase) from being added without a decision.
EXPECTED_LEGAL_EDGES = {
    (Phase.INITIALIZING, Phase.ANALYZE_EXECUTE),
    (Phase.ANALYZE_EXECUTE, Phase.REVIEW),
    (Phase.REVIEW, Phase.FIX),
    (Phase.REVIEW, Phase.REPLAN_REEXECUTE),
    (Phase.REVIEW, Phase.READY_FOR_MERGE),
    (Phase.REVIEW, Phase.REVIEW),  # stale review (HEAD moved) re-reviews
    (Phase.FIX, Phase.REVIEW),
    (Phase.REPLAN_REEXECUTE, Phase.REVIEW),
    (Phase.READY_FOR_MERGE, Phase.MERGE),
    (Phase.READY_FOR_MERGE, Phase.REVIEW),
    (Phase.MERGE, Phase.ANALYZE_EXECUTE),
    (Phase.MERGE, Phase.UPDATE_EPIC),
    (Phase.MERGE, Phase.DONE),
    (Phase.MERGE, Phase.REVIEW),
    (Phase.UPDATE_EPIC, Phase.ANALYZE_EXECUTE),
    (Phase.UPDATE_EPIC, Phase.DONE),
}


@pytest.mark.parametrize("frm", list(Phase), ids=lambda p: p.value)
@pytest.mark.parametrize("to", list(Phase), ids=lambda p: p.value)
def test_every_phase_pair_matches_the_declared_topology(frm, to):
    """The full Phase x Phase matrix: legal iff the spec above says so."""
    expected = (frm, to) in EXPECTED_LEGAL_EDGES
    assert is_legal(frm, to) is expected
    if expected:
        validate_transition(frm, to)  # must not raise
    else:
        with pytest.raises(StateTransitionError, match="illegal transition"):
            validate_transition(frm, to)


def test_replan_reexecute_has_exactly_one_exit_and_one_entry():
    """REPLAN_REEXECUTE is a funnel: REVIEW in, a fresh REVIEW out.

    It is the only phase in which the controller closes an open PR, so any
    additional edge would be a second way in or out of that destructive step.
    """
    outgoing = {to for frm, to in EXPECTED_LEGAL_EDGES if frm is Phase.REPLAN_REEXECUTE}
    incoming = {frm for frm, to in EXPECTED_LEGAL_EDGES if to is Phase.REPLAN_REEXECUTE}
    assert outgoing == {Phase.REVIEW}
    assert incoming == {Phase.REVIEW}
    # BLOCKED/FAILED are reached by the controller's holding-state path, not by
    # a topology edge, so REPLAN_REEXECUTE cannot "transition" into them.
    assert not is_legal(Phase.REPLAN_REEXECUTE, Phase.BLOCKED)
    assert not is_legal(Phase.REPLAN_REEXECUTE, Phase.FAILED)


def test_terminal_phases_have_no_outgoing_edges():
    for phase in TERMINAL_PHASES:
        assert LEGAL_EDGES[phase] == frozenset()


def test_every_phase_appears_in_the_topology():
    """A new phase must be given edges deliberately, not inherit an empty set."""
    assert set(LEGAL_EDGES) == set(Phase)


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
