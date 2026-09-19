"""Controller phase state machine.

Legal transitions::

    INITIALIZING     -> ANALYZE_EXECUTE
    ANALYZE_EXECUTE  -> REVIEW
    REVIEW           -> FIX               (needs_fix_round=true)
    REVIEW           -> REPLAN_REEXECUTE  (controller policy; findings remain)
    REPLAN_REEXECUTE -> REVIEW             (replacement PR, fresh round 1)
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

LOCAL runs (``WorkflowMode.LOCAL``) drive a much smaller topology over a
feature Markdown file and the local working tree — no GitHub, no PR, no
merge::

    INITIALIZING     -> ANALYZE_EXECUTE
    ANALYZE_EXECUTE  -> REVIEW
    REVIEW           -> FIX               (needs_fix_round=true, fix budget left)
    FIX              -> REVIEW
    REVIEW           -> DONE              (needs_fix_round=false)

``REVIEW -> READY_FOR_MERGE`` is deliberately *not* legal in LOCAL mode and
``REVIEW -> DONE`` is deliberately *not* legal in REMOTE mode: nothing may
reach DONE locally through the merge machinery, and no remote run may skip
it. A LOCAL review that still has findings once the fix budget is spent
enters BLOCKED, exactly like a remote loop bound.

BLOCKED / FAILED are exceptional holding states reachable from any agent
phase (agent reports blocked/failure, or the controller cannot verify
GitHub state deterministically); they are outside the happy-path topology.

BLOCKED has *operator* edges out of it (``UNBLOCK_TARGETS``), taken only by
the explicit ``unblock`` command after the controller has re-inspected live
GitHub state and chosen the phase to re-enter; no agent result and no
``resume`` ever leaves BLOCKED on its own::

    BLOCKED          -> ANALYZE_EXECUTE   (no PR bound; the entry adopts or launches)
    BLOCKED          -> REVIEW            (open PR; no current review of its revision)
    BLOCKED          -> FIX               (open PR at the reviewed HEAD with open findings)
    BLOCKED          -> READY_FOR_MERGE   (open or merged PR at the clean-reviewed revision)
    BLOCKED          -> UPDATE_EPIC       (PR merged and already counted)

``BLOCKED -> REPLAN_REEXECUTE`` is deliberately absent: REVIEW stays that
transaction's only entry. FAILED and DONE keep no outgoing edge, and the
LOCAL topology has no unblock edge at all.

All transition logic lives here — never scattered across CLI handlers.
"""

from __future__ import annotations

from enum import Enum

from .errors import StateTransitionError


class WorkflowMode(Enum):
    """Which workflow a run executes.

    REMOTE is the original GitHub lifecycle (Issue -> PR -> review -> merge).
    LOCAL drives a feature Markdown file against the local working tree and
    never touches GitHub. The mode is explicit controller state, never
    inferred from whether some field happens to be empty.
    """

    REMOTE = "REMOTE"
    LOCAL = "LOCAL"


class Phase(Enum):
    INITIALIZING = "INITIALIZING"
    ANALYZE_EXECUTE = "ANALYZE_EXECUTE"
    REVIEW = "REVIEW"
    FIX = "FIX"
    REPLAN_REEXECUTE = "REPLAN_REEXECUTE"
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

# LOCAL phases whose agent can change the working tree. REVIEW is excluded: it
# is read-only and the controller *rejects* a reviewer that changed anything.
# A pending-invocation checkpoint (``AutoForgeState.local_pending_phase``) may
# name one of these and nothing else, which is why the tuple lives here rather
# than in the engine: ``state`` validates a loaded checkpoint against it.
LOCAL_WRITE_PHASES = (Phase.ANALYZE_EXECUTE, Phase.FIX)

# Phases whose step invokes an agent through a provider. MERGE is NOT one of
# them: the controller runs `gh pr merge` itself (agents never merge).
AGENT_PHASES = frozenset(
    {Phase.ANALYZE_EXECUTE, Phase.REVIEW, Phase.FIX, Phase.REPLAN_REEXECUTE, Phase.UPDATE_EPIC}
)

# Static topology: every edge that can ever be legal (conditions on the
# CONTROL_RESULT payload are checked in decide_next_phase).
LEGAL_EDGES: dict[Phase, frozenset[Phase]] = {
    Phase.INITIALIZING: frozenset({Phase.ANALYZE_EXECUTE}),
    Phase.ANALYZE_EXECUTE: frozenset({Phase.REVIEW}),
    # REVIEW -> REVIEW: the PR HEAD moved during the review (stale review).
    Phase.REVIEW: frozenset(
        {Phase.FIX, Phase.REPLAN_REEXECUTE, Phase.READY_FOR_MERGE, Phase.REVIEW}
    ),
    Phase.FIX: frozenset({Phase.REVIEW}),
    Phase.REPLAN_REEXECUTE: frozenset({Phase.REVIEW}),
    Phase.READY_FOR_MERGE: frozenset({Phase.MERGE, Phase.REVIEW}),
    Phase.MERGE: frozenset({Phase.ANALYZE_EXECUTE, Phase.UPDATE_EPIC, Phase.DONE, Phase.REVIEW}),
    Phase.UPDATE_EPIC: frozenset({Phase.ANALYZE_EXECUTE, Phase.DONE}),
    Phase.DONE: frozenset(),
    # Operator edges only: see UNBLOCK_TARGETS below and ControllerEngine.unblock.
    Phase.BLOCKED: frozenset(
        {
            Phase.ANALYZE_EXECUTE,
            Phase.REVIEW,
            Phase.FIX,
            Phase.READY_FOR_MERGE,
            Phase.UPDATE_EPIC,
        }
    ),
    Phase.FAILED: frozenset(),
}

# The phases an explicit `unblock` may re-enter from BLOCKED (REMOTE only).
# `validate_transition(Phase.BLOCKED, target)` is the check the unblock path
# goes through, exactly like every other transition; this name exists so the
# engine, the docs and the tests refer to one decision rather than to a
# literal set repeated in each. REPLAN_REEXECUTE is never a target.
UNBLOCK_TARGETS: frozenset[Phase] = LEGAL_EDGES[Phase.BLOCKED]


# LOCAL topology: no PR, no merge, no replan, no EPIC. A clean review is the
# end of the run; findings the controller can no longer afford to fix enter
# BLOCKED (an exceptional state, not an edge).
LOCAL_LEGAL_EDGES: dict[Phase, frozenset[Phase]] = {
    Phase.INITIALIZING: frozenset({Phase.ANALYZE_EXECUTE}),
    Phase.ANALYZE_EXECUTE: frozenset({Phase.REVIEW}),
    Phase.REVIEW: frozenset({Phase.FIX, Phase.DONE}),
    Phase.FIX: frozenset({Phase.REVIEW}),
    Phase.DONE: frozenset(),
    Phase.BLOCKED: frozenset(),
    Phase.FAILED: frozenset(),
}

# Phases a LOCAL run may ever execute. Everything else (REPLAN_REEXECUTE,
# READY_FOR_MERGE, MERGE, UPDATE_EPIC) belongs to the GitHub lifecycle.
LOCAL_PHASES = frozenset(
    {
        Phase.INITIALIZING,
        Phase.ANALYZE_EXECUTE,
        Phase.REVIEW,
        Phase.FIX,
        Phase.DONE,
        Phase.BLOCKED,
        Phase.FAILED,
    }
)


def edges_for(mode: WorkflowMode = WorkflowMode.REMOTE) -> dict[Phase, frozenset[Phase]]:
    """The static topology of ``mode`` (never mutate the returned mapping)."""
    return LOCAL_LEGAL_EDGES if mode == WorkflowMode.LOCAL else LEGAL_EDGES


def stop_phases_for(mode: WorkflowMode = WorkflowMode.REMOTE) -> frozenset[Phase]:
    """Phases at which ``run``/``resume`` stop looping.

    A LOCAL run has no READY_FOR_MERGE holding state: it stops only on a
    terminal phase.
    """
    return TERMINAL_PHASES if mode == WorkflowMode.LOCAL else STOP_PHASES


def is_legal(frm: Phase, to: Phase, mode: WorkflowMode = WorkflowMode.REMOTE) -> bool:
    return to in edges_for(mode).get(frm, frozenset())


def validate_transition(frm: Phase, to: Phase, mode: WorkflowMode = WorkflowMode.REMOTE) -> None:
    """Raise StateTransitionError if ``frm -> to`` is not a legal edge."""
    if not is_legal(frm, to, mode):
        raise StateTransitionError(
            f"illegal transition in {mode.value} mode: {frm.value} -> {to.value}"
        )


def decide_next_phase(
    current: Phase, result: dict, mode: WorkflowMode = WorkflowMode.REMOTE
) -> Phase:
    """Pure function: current phase + validated CONTROL_RESULT -> next phase.

    ``result`` is the parsed CONTROL_RESULT payload whose ``phase`` field
    must equal ``current.value`` (checked by the result parser beforehand),
    possibly extended with the controller's own observations and decisions
    (``head_changed_after_review`` in READY_FOR_MERGE/MERGE, ``replan`` in
    REVIEW). Missing/invalid decision fields raise
    ControlResultValidationError; terminal phases raise StateTransitionError.
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
    if mode == WorkflowMode.LOCAL:
        if current == Phase.REVIEW:
            needs_fix = need("needs_fix_round")
            if not isinstance(needs_fix, bool):
                raise ControlResultValidationError("'needs_fix_round' must be a boolean")
            # Whether the fix budget still allows a FIX is a controller policy
            # decision (see engine._local_review_stop_reason); the topology only
            # says both edges exist.
            return Phase.FIX if needs_fix else Phase.DONE
        raise StateTransitionError(f"phase {current.value} is not part of the LOCAL workflow")
    if current == Phase.REPLAN_REEXECUTE:
        return Phase.REVIEW
    if current == Phase.REVIEW:
        needs_fix = need("needs_fix_round")
        if not isinstance(needs_fix, bool):
            raise ControlResultValidationError("'needs_fix_round' must be a boolean")
        # ``replan`` is the controller's own decision (engine replan policy),
        # never a reviewer field: the agent's CONTROL_RESULT has no say in
        # whether its PR is replaced. It is only meaningful over findings --
        # REVIEW -> REPLAN_REEXECUTE without a fix round to escalate is not a
        # route this topology has.
        replan = result.get("replan", False)
        if not isinstance(replan, bool):
            raise ControlResultValidationError("'replan' must be a boolean")
        if replan:
            if not needs_fix:
                raise ControlResultValidationError(
                    "a replan decision requires needs_fix_round == true: "
                    "only a review with findings can escalate to REPLAN_REEXECUTE"
                )
            return Phase.REPLAN_REEXECUTE
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
