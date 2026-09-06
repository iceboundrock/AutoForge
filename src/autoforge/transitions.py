"""Controller phase state machine.

Legal transitions::

    INITIALIZING     -> ANALYZE_EXECUTE
    ANALYZE_EXECUTE  -> REVIEW
    REVIEW           -> FIX               (needs_fix_round=true)
    REVIEW           -> READY_FOR_MERGE   (needs_fix_round=false, HEAD unchanged)
    FIX              -> REVIEW
    READY_FOR_MERGE  -> MERGE             (only behind the merge safety gate)
    READY_FOR_MERGE  -> REVIEW            (PR HEAD moved after the clean review)
    MERGE            -> UPDATE_EPIC       (controller merged; GitHub confirms MERGED)
    MERGE            -> REVIEW            (PR HEAD changed after last clean review)
    MERGE            -> ANALYZE_EXECUTE   (reserved: controller-owned batching, #13)
    MERGE            -> DONE              (reserved: controller-owned batching, #13)
    UPDATE_EPIC      -> ANALYZE_EXECUTE   (next_issue_url != null)
    UPDATE_EPIC      -> DONE              (next_issue_url == null)

BLOCKED / FAILED are exceptional holding states reachable from any agent
phase (agent reports blocked/failure, or the controller cannot verify
GitHub state deterministically); they are outside the happy-path topology.

All transition logic lives here — never scattered across CLI handlers.
"""

from __future__ import annotations

from enum import Enum

from .errors import StateTransitionError


class Phase(Enum):
    INITIALIZING = "INITIALIZING"
    ANALYZE_EXECUTE = "ANALYZE_EXECUTE"
    REVIEW = "REVIEW"
    FIX = "FIX"
    READY_FOR_MERGE = "READY_FOR_MERGE"
    MERGE = "MERGE"
    UPDATE_EPIC = "UPDATE_EPIC"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


TERMINAL_PHASES = frozenset({Phase.DONE, Phase.BLOCKED, Phase.FAILED})

# Phases at which ``run``/``resume`` stop looping while the merge gate is
# closed. READY_FOR_MERGE is a safe holding state in this milestone: automatic
# merge is disabled by default. With the gate open (config AND --allow-merge)
# the loop only stops at TERMINAL_PHASES.
STOP_PHASES = TERMINAL_PHASES | frozenset({Phase.READY_FOR_MERGE})

# Phases whose step invokes an agent through a provider. MERGE is NOT one of
# them: the controller runs `gh pr merge` itself (agents never merge).
AGENT_PHASES = frozenset({Phase.ANALYZE_EXECUTE, Phase.REVIEW, Phase.FIX, Phase.UPDATE_EPIC})

# Static topology: every edge that can ever be legal (conditions on the
# CONTROL_RESULT payload are checked in decide_next_phase).
LEGAL_EDGES: dict[Phase, frozenset[Phase]] = {
    Phase.INITIALIZING: frozenset({Phase.ANALYZE_EXECUTE}),
    Phase.ANALYZE_EXECUTE: frozenset({Phase.REVIEW}),
    # REVIEW -> REVIEW: the PR HEAD moved during the review (stale review).
    Phase.REVIEW: frozenset({Phase.FIX, Phase.READY_FOR_MERGE, Phase.REVIEW}),
    Phase.FIX: frozenset({Phase.REVIEW}),
    Phase.READY_FOR_MERGE: frozenset({Phase.MERGE, Phase.REVIEW}),
    Phase.MERGE: frozenset({Phase.ANALYZE_EXECUTE, Phase.UPDATE_EPIC, Phase.DONE, Phase.REVIEW}),
    Phase.UPDATE_EPIC: frozenset({Phase.ANALYZE_EXECUTE, Phase.DONE}),
    Phase.DONE: frozenset(),
    Phase.BLOCKED: frozenset(),
    Phase.FAILED: frozenset(),
}


def is_legal(frm: Phase, to: Phase) -> bool:
    return to in LEGAL_EDGES.get(frm, frozenset())


def validate_transition(frm: Phase, to: Phase) -> None:
    """Raise StateTransitionError if ``frm -> to`` is not a legal edge."""
    if not is_legal(frm, to):
        raise StateTransitionError(f"illegal transition: {frm.value} -> {to.value}")


def decide_next_phase(current: Phase, result: dict) -> Phase:
    """Pure function: current phase + validated CONTROL_RESULT -> next phase.

    ``result`` is the parsed CONTROL_RESULT payload whose ``phase`` field
    must equal ``current.value`` (checked by the result parser beforehand).
    Missing/invalid decision fields raise ControlResultValidationError;
    terminal phases raise StateTransitionError.
    """
    from .errors import ControlResultValidationError

    def need(key: str):  # helper with a meaningful error
        if key not in result:
            raise ControlResultValidationError(
                f"CONTROL_RESULT for phase {current.value} missing decision field {key!r}"
            )
        return result[key]

    if current == Phase.INITIALIZING:
        return Phase.ANALYZE_EXECUTE
    if current == Phase.ANALYZE_EXECUTE:
        return Phase.REVIEW
    if current == Phase.FIX:
        return Phase.REVIEW
    if current == Phase.REVIEW:
        needs_fix = need("needs_fix_round")
        if not isinstance(needs_fix, bool):
            raise ControlResultValidationError("'needs_fix_round' must be a boolean")
        return Phase.FIX if needs_fix else Phase.READY_FOR_MERGE
    if current == Phase.READY_FOR_MERGE:
        if result.get("head_changed_after_review") is True:
            return Phase.REVIEW
        return Phase.MERGE
    if current == Phase.MERGE:
        # MERGE is controller-executed (no agent CONTROL_RESULT). ``result``
        # here is the controller's own observation: HEAD moved -> REVIEW,
        # otherwise the verified merge routes to UPDATE_EPIC.
        if result.get("head_changed_after_review") is True:
            return Phase.REVIEW
        return Phase.UPDATE_EPIC
    if current == Phase.UPDATE_EPIC:
        nxt = result.get("next_issue_url")
        return Phase.ANALYZE_EXECUTE if nxt else Phase.DONE
    raise StateTransitionError(f"phase {current.value} is terminal; no next phase")
