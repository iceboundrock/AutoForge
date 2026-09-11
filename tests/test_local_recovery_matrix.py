"""Crash recovery for LOCAL mode, as a matrix over where the crash lands.

A LOCAL run's external side effect is the operator's working tree, and the
controller cannot roll it back. So the recovery question is never "undo it" --
it is "what independently verified fact authorises the next transition?" The
answer is always a fact re-derived after the crash: the fingerprint of the
tree as it *is*, the git anchor as it *is*, the frozen specification re-hashed
-- never a claim the state file inherited from the invocation that died.

The matrix below crashes at every boundary of one phase step:

    before the agent is launched | after its side effect, before its result
    at validation | after validation, before the transition is persisted
    after the transition is persisted

and then resumes *in a new controller object that loads the state from disk*,
because a crash is a new process. Each row asserts the same two invariants:

  1. The run never advances past work that was not verified after the crash.
  2. A write-capable agent is never re-invoked in a way that demands work its
     dead predecessor already put in the tree -- the invocation checkpoint is
     what makes "crashed before implementing" and "crashed after implementing"
     distinguishable without asking the agent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoforge.config import default_config
from autoforge.errors import StateError, VerificationError
from autoforge.local_workspace import LocalWorkspace
from autoforge.state import load_state
from autoforge.transitions import Phase

from .conftest import make_local_engine
from .test_local import (
    IMPL_FILE,
    finding,
    fix_result,
    impl_result,
    local_repo,
    review_result,
    touch_impl,
)

FEATURE = "features/add-filter.md"


class Crash(BaseException):
    """A power cut, not an exception the controller could ever handle.

    Deriving from ``BaseException`` is deliberate: an ``except Exception``
    anywhere in the step must not be able to turn this into an orderly
    shutdown, because a real crash does not give the controller that chance.
    """


def fresh(root: Path, cfg=None):
    """A new controller object over the same checkout: a restarted process."""
    eng = make_local_engine(root, FEATURE, cfg=cfg, start=False)
    eng.load()
    return eng


# A LOCAL step persists twice: the pre-agent invocation checkpoint, and the
# transition once everything verified. Naming them makes each test say which
# side of the agent it is crashing on.
SAVE_CHECKPOINT = 1
SAVE_TRANSITION = 2


def crash_on_save(eng, nth: int = SAVE_TRANSITION):
    """Make the ``nth`` remaining state persist die instead of landing."""
    original = eng._save
    left = {"n": nth}

    def dying_save(*a, **k):
        left["n"] -= 1
        if left["n"] == 0:
            raise Crash("power cut before the state reached the disk")
        return original(*a, **k)

    eng._save = dying_save  # type: ignore[method-assign]


def anchor(root: Path) -> tuple[str, str]:
    ws = LocalWorkspace(workdir=root)
    return ws.head_sha(), ws.branch()


def fingerprint(root: Path) -> str:
    return LocalWorkspace(workdir=root).snapshot().fingerprint


# -- the boundaries of one write phase ----------------------------------------
def test_a_crash_before_the_agent_ran_leaves_the_phase_to_be_done(tmp_path):
    """Nothing is in the tree, so the resumed attempt must really implement."""
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()  # INITIALIZING -> ANALYZE_EXECUTE
    before = fingerprint(root)

    def dies_first(req):
        raise Crash("killed while the agent was starting")

    eng.provider._handler = dies_first
    with pytest.raises(Crash):
        eng.step()

    # The checkpoint is durable and says the phase was launched once.
    saved = load_state(eng.paths.state_file)
    assert saved.phase == Phase.ANALYZE_EXECUTE
    assert saved.local_pending_phase == "ANALYZE_EXECUTE"
    assert saved.local_pending_fingerprint == before
    assert saved.local_pending_attempts == 1
    assert fingerprint(root) == before, "a dead agent left nothing behind"

    # Resume, in a new process. An honest "I changed nothing" is now a lie
    # about a tree that still has no implementation, and is refused.
    eng2 = fresh(root)
    eng2.provider._handler = lambda req: impl_result(changed=False)
    with pytest.raises(VerificationError, match="no implementation exists"):
        eng2.step()

    eng3 = fresh(root)
    eng3.provider._handler = lambda req: (touch_impl(root, "v1\n"), impl_result())[1]
    assert eng3.step().next_phase == "REVIEW"


def test_a_crash_after_the_agents_side_effect_does_not_demand_the_work_twice(tmp_path):
    """The tree already holds the implementation; the state file does not know.

    This is the case that makes the invocation checkpoint necessary. Without
    it the resumed attempt compares the tree against the tree its own dead
    predecessor wrote, finds them identical, and rejects an entirely correct
    "nothing left to do" as a failure to implement -- a run whose only escape
    is editing state.json by hand.
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()
    before = fingerprint(root)

    def writes_then_dies(req):
        touch_impl(root, "implemented by the process that died\n")
        raise Crash("killed between the side effect and the result")

    eng.provider._handler = writes_then_dies
    with pytest.raises(Crash):
        eng.step()
    assert fingerprint(root) != before, "the side effect survived the crash"
    assert load_state(eng.paths.state_file).local_pending_fingerprint == before

    # The resumed agent sees the work already there and says so. Judged
    # against the pre-crash baseline, that is progress, and it advances.
    eng2 = fresh(root)
    prompts: list[str] = []

    def honest(req):
        prompts.append(req.prompt)
        return impl_result(changed=False, summary="Already implemented by the previous attempt.")

    eng2.provider._handler = honest
    assert eng2.step().next_phase == "REVIEW"
    assert eng2.state.local_pending_phase == "", "the checkpoint is closed once verified"
    # And the agent was warned, so it continues rather than starting over.
    assert "Earlier attempt at this phase" in prompts[0]


def test_a_crash_at_validation_keeps_the_phase_unverified(tmp_path):
    """Validation is controller-owned, so its absence is never an advance."""
    root = local_repo(tmp_path)
    cfg = default_config()
    marker = root / "PASS"
    cfg.local.validation_commands = [["test", "-e", str(marker)]]
    eng = make_local_engine(root, FEATURE, cfg=cfg)
    eng.step()
    before = fingerprint(root)

    eng.provider._handler = lambda req: (touch_impl(root, "v1\n"), impl_result())[1]

    def dying_validation(phase):
        raise Crash("killed while the validation command ran")

    eng._run_validation_commands = dying_validation  # type: ignore[method-assign]
    with pytest.raises(Crash):
        eng.step()

    saved = load_state(eng.paths.state_file)
    assert saved.phase == Phase.ANALYZE_EXECUTE
    assert saved.local_pending_fingerprint == before

    # Resume with validation actually passing.
    marker.write_text("ok\n", encoding="utf-8")
    eng2 = fresh(root, cfg=cfg)
    eng2.provider._handler = lambda req: impl_result(changed=False)
    assert eng2.step().next_phase == "REVIEW"


def test_a_crash_after_validation_but_before_the_transition_is_persisted(tmp_path):
    """The decision died with the process, so it is made again from the tree."""
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()

    eng.provider._handler = lambda req: (touch_impl(root, "v1\n"), impl_result())[1]
    crash_on_save(eng, SAVE_TRANSITION)  # the persist of ANALYZE_EXECUTE -> REVIEW
    with pytest.raises(Crash):
        eng.step()

    saved = load_state(eng.paths.state_file)
    assert saved.phase == Phase.ANALYZE_EXECUTE, "an unpersisted transition never happened"
    # The checkpoint is still open, which is what lets the retry accept the
    # work the dead attempt verified but never got to record.
    assert saved.local_pending_phase == "ANALYZE_EXECUTE"

    eng2 = fresh(root)
    eng2.provider._handler = lambda req: impl_result(changed=False)
    assert eng2.step().next_phase == "REVIEW"
    assert eng2.state.review_round == 0, "no review round was charged by the crash"


def test_a_crash_after_the_transition_is_persisted_does_not_repeat_the_phase(tmp_path):
    """The one boundary where the work is done: it must not be redone."""
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()
    invocations = {"n": 0}

    def counting(req):
        invocations["n"] += 1
        touch_impl(root, "v1\n")
        return impl_result()

    eng.provider._handler = counting
    eng.step()
    assert load_state(eng.paths.state_file).phase == Phase.REVIEW

    eng2 = fresh(root)
    eng2.provider._handler = lambda req: review_result(eng2.state.workspace_fingerprint)
    assert eng2.step().next_phase == "DONE"
    assert invocations["n"] == 1, "the implementation phase ran exactly once"


# -- the review boundary -------------------------------------------------------
def test_a_crash_before_a_clean_review_is_persisted_re_reviews(tmp_path):
    """A review that never landed cannot finish the run, and is not a round.

    REVIEW is read-only, so repeating it is always safe -- but it must repeat
    against the tree as it is now, never be reconstructed from the payload the
    dead process held.
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()
    eng.provider._handler = lambda req: (touch_impl(root, "v1\n"), impl_result())[1]
    eng.step()

    eng.provider._handler = lambda req: review_result(eng.state.workspace_fingerprint)
    crash_on_save(eng, SAVE_TRANSITION)
    with pytest.raises(Crash):
        eng.step()

    saved = load_state(eng.paths.state_file)
    assert saved.phase == Phase.REVIEW, "DONE was never reached"
    assert saved.review_round == 0, "an unpersisted review is not a completed round"
    assert saved.local_pending_phase == "", "REVIEW writes nothing, so it is not checkpointed"

    eng2 = fresh(root)
    eng2.provider._handler = lambda req: review_result(eng2.state.workspace_fingerprint)
    assert eng2.step().next_phase == "DONE"
    assert eng2.state.review_round == 1, "charged exactly once"


def test_a_crash_before_a_findings_review_is_persisted_does_not_charge_a_round(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()
    eng.provider._handler = lambda req: (touch_impl(root, "v1\n"), impl_result())[1]
    eng.step()

    eng.provider._handler = lambda req: review_result(
        eng.state.workspace_fingerprint, findings=[finding()]
    )
    crash_on_save(eng, SAVE_TRANSITION)
    with pytest.raises(Crash):
        eng.step()
    assert load_state(eng.paths.state_file).review_round == 0
    assert load_state(eng.paths.state_file).open_findings == []

    eng2 = fresh(root)
    eng2.provider._handler = lambda req: review_result(
        eng2.state.workspace_fingerprint, findings=[finding()]
    )
    assert eng2.step().next_phase == "FIX"
    assert eng2.state.review_round == 1
    assert [f["id"] for f in eng2.state.open_findings] == ["R1-F1"]


def test_the_tree_moving_while_the_controller_was_dead_invalidates_the_review(tmp_path):
    """The reviewed bytes are the only thing a clean review is about.

    A crash is a window in which the operator can edit anything, so the
    fingerprint the resumed reviewer is bound to is re-derived, never
    inherited -- and a review quoting the pre-crash fingerprint is stale.
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()
    eng.provider._handler = lambda req: (touch_impl(root, "v1\n"), impl_result())[1]
    eng.step()
    reviewed = eng.state.workspace_fingerprint

    crash_on_save(eng, SAVE_TRANSITION)
    eng.provider._handler = lambda req: review_result(reviewed)
    with pytest.raises(Crash):
        eng.step()

    # The operator edits the tree while nothing is running.
    (root / "src" / "extra.py").write_text("EDITED = 1\n", encoding="utf-8")

    eng2 = fresh(root)
    assert eng2.state.workspace_fingerprint == reviewed, "state still holds the old value"
    eng2.provider._handler = lambda req: review_result(reviewed)
    with pytest.raises(VerificationError, match="fingerprint"):
        eng2.step()
    assert eng2.state.phase == Phase.REVIEW
    # Re-bound: the reviewer is told about the tree as it is now, and a review
    # of *that* is accepted.
    eng3 = fresh(root)
    eng3.provider._handler = lambda req: review_result(eng3.state.workspace_fingerprint)
    assert eng3.state.workspace_fingerprint != reviewed
    assert eng3.step().next_phase == "DONE"


def test_a_fix_round_is_not_charged_twice_by_a_crash(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()
    eng.provider._handler = lambda req: (touch_impl(root, "v1\n"), impl_result())[1]
    eng.step()
    eng.provider._handler = lambda req: review_result(
        eng.state.workspace_fingerprint, findings=[finding()]
    )
    eng.step()
    assert eng.state.phase == Phase.FIX

    def fixes_then_dies(req):
        touch_impl(root, "fixed\n")
        raise Crash("killed after the fix landed")

    eng.provider._handler = fixes_then_dies
    with pytest.raises(Crash):
        eng.step()
    assert load_state(eng.paths.state_file).local_fix_rounds == 0

    eng2 = fresh(root)
    eng2.provider._handler = lambda req: fix_result(["R1-F1"], changed=False)
    assert eng2.step().next_phase == "REVIEW"
    assert eng2.state.local_fix_rounds == 1, "one fix round, not two"


# -- the boundaries around the run itself --------------------------------------
def test_a_crash_before_the_run_was_ever_persisted_leaves_no_run(tmp_path):
    """`new_local_run` writes nothing; a crash before the first save is a no-op.

    `resume` must then say there is no run rather than invent one, because a
    silently created run would start implementing against a specification no
    operator confirmed.
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    crash_on_save(eng, SAVE_CHECKPOINT)
    with pytest.raises(Crash):
        eng.step()

    eng2 = make_local_engine(root, FEATURE, start=False)
    with pytest.raises(StateError, match="no state file"):
        eng2.load()


def test_a_crash_window_used_to_commit_blocks_rather_than_resuming(tmp_path):
    """The anchor is re-read on resume, not carried over from the dead run.

    A commit during the crash window changes what the accumulated findings and
    the frozen specification are about, and the controller cannot tell the
    operator's commit from an agent's. It refuses, and says nothing was undone.
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()
    head_before = anchor(root)

    def writes_then_dies(req):
        touch_impl(root, "v1\n")
        raise Crash("killed")

    eng.provider._handler = writes_then_dies
    with pytest.raises(Crash):
        eng.step()

    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=t@e",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "operator commit during the outage",
        ],
        check=True,
    )
    assert anchor(root) != head_before

    eng2 = fresh(root)
    invoked = {"n": 0}

    def must_not_run(req):
        invoked["n"] += 1
        return impl_result()

    eng2.provider._handler = must_not_run
    outcome = eng2.step()
    assert outcome.next_phase == "BLOCKED"
    assert invoked["n"] == 0, "the agent must not be launched into a moved anchor"
    assert "git anchor moved" in eng2.state.block_reason
    assert "Nothing was rolled back" in eng2.state.block_reason


def test_every_crash_point_leaves_state_that_loads(tmp_path):
    """No crash may produce a state file a later controller cannot read.

    State is written by atomic replace, so the file on disk is always one
    whole version -- either the previous one or the new one, never a torn
    mixture. That is what makes "resume reads it and re-derives the rest"
    a complete recovery story rather than a hopeful one.
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()

    def dies_before(req):
        raise Crash("killed before the agent wrote anything")

    def dies_after(req):
        touch_impl(root, "partial\n")
        raise Crash("killed after the agent wrote something")

    seen = []
    for handler in (dies_before, dies_after):
        eng2 = fresh(root)
        eng2.provider._handler = handler
        with pytest.raises(Crash):
            eng2.step()
        reloaded = load_state(eng2.paths.state_file)
        seen.append(reloaded.local_pending_attempts)
        assert reloaded.phase == Phase.ANALYZE_EXECUTE
    assert seen == [1, 2], "each launch is counted exactly once, across processes"


def test_nothing_outside_the_checkout_is_touched_by_any_of_this(tmp_path):
    """The sentinel the whole LOCAL promise is about, asserted end to end."""
    outside = tmp_path / "sentinel.txt"
    outside.write_text("mine\n", encoding="utf-8")
    before = outside.stat()
    root = local_repo(tmp_path / "checkout")
    eng = make_local_engine(root, FEATURE)
    eng.step()
    eng.provider._handler = lambda req: (touch_impl(root, "v1\n"), impl_result())[1]
    eng.step()
    eng.provider._handler = lambda req: review_result(eng.state.workspace_fingerprint)
    eng.step()
    assert eng.state.phase == Phase.DONE
    assert outside.read_text(encoding="utf-8") == "mine\n"
    assert outside.stat().st_ino == before.st_ino
    assert outside.stat().st_mtime_ns == before.st_mtime_ns
    assert sorted(p.name for p in tmp_path.iterdir()) == ["checkout", "sentinel.txt"]


def test_the_implementation_file_the_dead_agent_wrote_is_never_reverted(tmp_path):
    """The controller does not own the tree; recovery never discards work."""
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    eng.step()

    def writes_then_dies(req):
        touch_impl(root, "half-finished work\n")
        raise Crash("killed")

    eng.provider._handler = writes_then_dies
    with pytest.raises(Crash):
        eng.step()
    eng2 = fresh(root)
    eng2.provider._handler = lambda req: impl_result(changed=False)
    eng2.step()
    assert (root / IMPL_FILE).read_text(encoding="utf-8") == "half-finished work\n"
