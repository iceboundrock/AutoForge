"""Transitions: every legal edge passes, every illegal edge raises."""

import pytest

from autoforge.errors import ControlResultValidationError, StateTransitionError
from autoforge.transitions import (
    LEGAL_EDGES,
    LOCAL_LEGAL_EDGES,
    TERMINAL_PHASES,
    UNBLOCK_TARGETS,
    Phase,
    WorkflowMode,
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
    # Operator edges: taken only by `unblock` after a live GitHub inspection (#5).
    (Phase.BLOCKED, Phase.ANALYZE_EXECUTE),
    (Phase.BLOCKED, Phase.REVIEW),
    (Phase.BLOCKED, Phase.FIX),
    (Phase.BLOCKED, Phase.READY_FOR_MERGE),
    (Phase.BLOCKED, Phase.UPDATE_EPIC),
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


def test_terminal_phases_have_no_outgoing_edges_except_the_operators_unblock():
    """DONE and FAILED are final. BLOCKED's edges are the operator's, and only
    the operator's: `decide_next_phase` still refuses it, so no agent result
    can route out of it, and the LOCAL topology has no way out at all."""
    assert LEGAL_EDGES[Phase.DONE] == frozenset()
    assert LEGAL_EDGES[Phase.FAILED] == frozenset()
    assert LEGAL_EDGES[Phase.BLOCKED] == UNBLOCK_TARGETS
    assert UNBLOCK_TARGETS == {to for frm, to in EXPECTED_LEGAL_EDGES if frm is Phase.BLOCKED}
    assert Phase.REPLAN_REEXECUTE not in UNBLOCK_TARGETS
    assert not (UNBLOCK_TARGETS & TERMINAL_PHASES)
    for phase in TERMINAL_PHASES:
        assert LOCAL_LEGAL_EDGES[phase] == frozenset()
        assert not is_legal(phase, Phase.REVIEW, WorkflowMode.LOCAL)
    with pytest.raises(StateTransitionError, match="terminal"):
        decide_next_phase(Phase.BLOCKED, {"needs_fix_round": False})


def test_every_phase_appears_in_the_topology():
    """A new phase must be given edges deliberately, not inherit an empty set."""
    assert set(LEGAL_EDGES) == set(Phase)


def test_decide_review_routing():
    assert decide_next_phase(Phase.REVIEW, {"needs_fix_round": True}) == Phase.FIX
    assert decide_next_phase(Phase.REVIEW, {"needs_fix_round": False}) == Phase.READY_FOR_MERGE


@pytest.mark.parametrize("needs_fix", [True, False], ids=["findings", "clean"])
def test_decide_review_stale_round_re_reviews_the_actual_revision(needs_fix):
    """REVIEW -> REVIEW: the reviewed revision moved while the reviewer worked.

    ``head_changed_after_review`` is the controller's observation (HEAD, base
    or merge base), never a reviewer field. The round's verdict describes a
    revision the PR no longer has, so it decides nothing about the next phase
    (a clean stale round does not reach READY_FOR_MERGE) and the actual
    revision is reviewed; the engine consumes the round and carries the
    findings itself.
    """
    result = {"needs_fix_round": needs_fix, "head_changed_after_review": True}
    assert decide_next_phase(Phase.REVIEW, result) == Phase.REVIEW
    assert is_legal(Phase.REVIEW, Phase.REVIEW)
    # An explicit ``False`` routes exactly like an absent one.
    result = {"needs_fix_round": needs_fix, "head_changed_after_review": False}
    expected = Phase.FIX if needs_fix else Phase.READY_FOR_MERGE
    assert decide_next_phase(Phase.REVIEW, result) == expected


def test_decide_review_refuses_a_replan_on_a_stale_round():
    """The replan policy escalates the findings of the reviewed revision; a
    round whose revision moved is not one it may have decided on."""
    result = {"needs_fix_round": True, "head_changed_after_review": True, "replan": True}
    with pytest.raises(ControlResultValidationError, match="stale review round"):
        decide_next_phase(Phase.REVIEW, result)
    result = {"needs_fix_round": True, "head_changed_after_review": "moved"}
    with pytest.raises(ControlResultValidationError, match="must be a boolean"):
        decide_next_phase(Phase.REVIEW, result)


def test_local_review_has_no_stale_edge():
    """A LOCAL review is bound to a workspace fingerprint the reviewer may not
    change (the engine rejects a reviewer that did), so the observation is
    never consulted there -- and never validated either, like ``replan``."""
    assert not is_legal(Phase.REVIEW, Phase.REVIEW, WorkflowMode.LOCAL)
    for observed in (True, "moved"):
        result = {"needs_fix_round": True, "head_changed_after_review": observed}
        assert decide_next_phase(Phase.REVIEW, result, WorkflowMode.LOCAL) == Phase.FIX
        result = {"needs_fix_round": False, "head_changed_after_review": observed}
        assert decide_next_phase(Phase.REVIEW, result, WorkflowMode.LOCAL) == Phase.DONE


def test_decide_review_can_express_the_controllers_replan_decision():
    """REVIEW -> REPLAN_REEXECUTE is a legal edge, so the router must know it.

    The decision is the controller's (replan policy), carried as ``replan``
    beside the reviewer's fields; an explicit ``replan: false`` routes exactly
    like an absent one.
    """
    result = {"needs_fix_round": True, "replan": True}
    assert decide_next_phase(Phase.REVIEW, result) == Phase.REPLAN_REEXECUTE
    assert is_legal(Phase.REVIEW, Phase.REPLAN_REEXECUTE)
    assert decide_next_phase(Phase.REVIEW, {"needs_fix_round": True, "replan": False}) == Phase.FIX
    assert (
        decide_next_phase(Phase.REVIEW, {"needs_fix_round": False, "replan": False})
        == Phase.READY_FOR_MERGE
    )


def test_decide_review_refuses_a_replan_over_a_clean_review():
    """Only a review with findings can escalate; a clean one never replans."""
    with pytest.raises(ControlResultValidationError, match="needs_fix_round == true"):
        decide_next_phase(Phase.REVIEW, {"needs_fix_round": False, "replan": True})
    with pytest.raises(ControlResultValidationError, match="'replan' must be a boolean"):
        decide_next_phase(Phase.REVIEW, {"needs_fix_round": True, "replan": "yes"})


@pytest.mark.parametrize(
    "result",
    [
        {"needs_fix_round": True},
        {"needs_fix_round": True, "replan": True},
        {"needs_fix_round": True, "replan": "yes"},
    ],
    ids=["absent", "true", "not-a-boolean"],
)
def test_local_review_ignores_replan_silently(result):
    """LOCAL mode has no REPLAN_REEXECUTE; the replan key is never consulted there.

    Never consulted means never validated either: a ``replan`` that REMOTE
    would route on or refuse still routes LOCAL's review with findings to FIX.
    """
    assert not is_legal(Phase.REVIEW, Phase.REPLAN_REEXECUTE, WorkflowMode.LOCAL)
    assert decide_next_phase(Phase.REVIEW, result, WorkflowMode.LOCAL) == Phase.FIX


def test_decide_ready_for_merge_is_controller_owned():
    """No agent runs in READY_FOR_MERGE: the PR at the reviewed revision goes
    to MERGE (the gate itself is the engine's), a moved one back to REVIEW."""
    assert decide_next_phase(Phase.READY_FOR_MERGE, {}) == Phase.MERGE
    assert (
        decide_next_phase(Phase.READY_FOR_MERGE, {"head_changed_after_review": False})
        == Phase.MERGE
    )
    assert (
        decide_next_phase(Phase.READY_FOR_MERGE, {"head_changed_after_review": True})
        == Phase.REVIEW
    )
    with pytest.raises(ControlResultValidationError, match="must be a boolean"):
        decide_next_phase(Phase.READY_FOR_MERGE, {"head_changed_after_review": 1})


def test_decide_merge_is_controller_owned():
    # No agent decides anything in MERGE: a verified controller merge routes to
    # UPDATE_EPIC (always: batching roadmap updates is decided inside it, so
    # the reserved MERGE -> ANALYZE_EXECUTE / DONE edges are never chosen); a
    # stale revision after the clean review goes back to REVIEW.
    assert decide_next_phase(Phase.MERGE, {}) == Phase.UPDATE_EPIC
    assert decide_next_phase(Phase.MERGE, {"next_action": "DONE"}) == Phase.UPDATE_EPIC
    assert decide_next_phase(Phase.MERGE, {"head_changed_after_review": False}) == Phase.UPDATE_EPIC
    assert decide_next_phase(Phase.MERGE, {"head_changed_after_review": True}) == Phase.REVIEW
    with pytest.raises(ControlResultValidationError, match="must be a boolean"):
        decide_next_phase(Phase.MERGE, {"head_changed_after_review": "yes"})


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


# Every decision input the engine hands to ``decide_next_phase``, per phase and
# mode, with the edge it must choose. The engine's tests prove it consults
# the function; this table proves the function never chooses an edge the
# topology of that mode does not have.
REMOTE_DECISIONS = [
    (Phase.INITIALIZING, {}, Phase.ANALYZE_EXECUTE),
    (Phase.ANALYZE_EXECUTE, {}, Phase.REVIEW),
    (Phase.REVIEW, {"needs_fix_round": False}, Phase.READY_FOR_MERGE),
    (Phase.REVIEW, {"needs_fix_round": True, "replan": False}, Phase.FIX),
    (Phase.REVIEW, {"needs_fix_round": True, "replan": True}, Phase.REPLAN_REEXECUTE),
    (Phase.REVIEW, {"needs_fix_round": True, "head_changed_after_review": True}, Phase.REVIEW),
    (Phase.REVIEW, {"needs_fix_round": False, "head_changed_after_review": True}, Phase.REVIEW),
    (Phase.FIX, {}, Phase.REVIEW),
    (Phase.REPLAN_REEXECUTE, {}, Phase.REVIEW),
    (Phase.READY_FOR_MERGE, {"head_changed_after_review": False}, Phase.MERGE),
    (Phase.READY_FOR_MERGE, {"head_changed_after_review": True}, Phase.REVIEW),
    (Phase.MERGE, {"head_changed_after_review": False}, Phase.UPDATE_EPIC),
    (Phase.MERGE, {"head_changed_after_review": True}, Phase.REVIEW),
    (Phase.UPDATE_EPIC, {"next_issue_url": None}, Phase.DONE),
    (
        Phase.UPDATE_EPIC,
        {"next_issue_url": "https://github.com/o/r/issues/3"},
        Phase.ANALYZE_EXECUTE,
    ),
]
LOCAL_DECISIONS = [
    (Phase.INITIALIZING, {}, Phase.ANALYZE_EXECUTE),
    (Phase.ANALYZE_EXECUTE, {}, Phase.REVIEW),
    (Phase.REVIEW, {"needs_fix_round": True}, Phase.FIX),
    (Phase.REVIEW, {"needs_fix_round": False}, Phase.DONE),
    (Phase.FIX, {}, Phase.REVIEW),
]


@pytest.mark.parametrize(
    ("mode", "current", "result", "expected"),
    [(WorkflowMode.REMOTE, *row) for row in REMOTE_DECISIONS]
    + [(WorkflowMode.LOCAL, *row) for row in LOCAL_DECISIONS],
    ids=lambda v: v.value if isinstance(v, (Phase, WorkflowMode)) else None,
)
def test_every_decision_is_a_legal_edge_of_its_mode(mode, current, result, expected):
    nxt = decide_next_phase(current, result, mode)
    assert nxt == expected
    validate_transition(current, nxt, mode)  # must not raise


@pytest.mark.parametrize("mode", list(WorkflowMode), ids=lambda m: m.value)
def test_decisions_cover_every_non_operator_edge_of_the_mode(mode):
    """The decision table above reaches every edge the topology declares,
    except the operator's unblock edges (taken by ``unblock`` alone) and the
    reserved post-merge edges (MERGE -> ANALYZE_EXECUTE / DONE, never chosen:
    UPDATE_EPIC runs after every merge). A new edge in the table without a
    decision that chooses it is dead topology; a decision that reaches no
    declared edge is refused by ``validate_transition`` in the test above."""
    table = REMOTE_DECISIONS if mode is WorkflowMode.REMOTE else LOCAL_DECISIONS
    edges = LEGAL_EDGES if mode is WorkflowMode.REMOTE else LOCAL_LEGAL_EDGES
    reserved = {(Phase.MERGE, Phase.ANALYZE_EXECUTE), (Phase.MERGE, Phase.DONE)}
    declared = {(frm, to) for frm, tos in edges.items() for to in tos if frm is not Phase.BLOCKED}
    assert {(frm, expected) for frm, _, expected in table} == declared - reserved
