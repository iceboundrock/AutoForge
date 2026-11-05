"""#414: the owner of a bound PR changes at each successive read of it.

A run drives its PR only while the PR's implementation marker names it
(ADR 0004 D9.4a). These tests take a lifecycle (REVIEW through MERGE, a FIX
with two effects, a replan), count the reads that hand the controller the
bound PR's body, and replay it once per read with the marker rewritten to
name another run at that read. Whatever the read, the run launches, posts,
pushes, closes and merges nothing after the change, stops with ``pr-owner``
by its next step at the latest, and a new controller process on the same
state does no better.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from autoforge import engine as engine_module
from autoforge.engine import ControllerEngine
from autoforge.errors import GitHubError
from autoforge.state import AutoForgeState, load_state
from autoforge.stop_classification import StopReasonCode
from autoforge.transitions import Phase
from autoforge.validation import parse_pr_url
from tests.conftest import (
    PR,
    FakeGitHub,
    block,
    connect_origin,
    fixed,
    fixer,
    implementation_pr_body,
    make_engine,
    new_follow_up,
    review_result,
)
from tests.test_engine import SHA_A, _finding, _in_fix, _in_review
from tests.test_replan import _park_at_hard_threshold, _replan_agent

OTHER_RUN = "af-20261031T040500Z-0a0b0c"
_DRIVE = engine_module.drive


@dataclass
class Lifecycle:
    eng: ControllerEngine
    steps: int
    allow_merge: bool = False


def _review_to_merge(tmp_path, gh: FakeGitHub) -> Lifecycle:
    eng = _in_review(tmp_path / "state", gh, [block(review_result(1, SHA_A))])
    eng.config.safety.allow_merge = True
    return Lifecycle(eng, steps=3, allow_merge=True)  # REVIEW, READY_FOR_MERGE, MERGE


def _fix_with_two_effects(tmp_path, gh: FakeGitHub) -> Lifecycle:
    script = fixer(fixed("R1-F1"), new_follow_up("R2-F1"))
    eng = _in_fix(tmp_path / "state", gh, script, findings=[_finding(1), _finding(2)], origin=True)
    return Lifecycle(eng, steps=1)  # a follow-up issue and the push


def _replan(tmp_path, gh: FakeGitHub) -> Lifecycle:
    eng = _park_at_hard_threshold(tmp_path / "state", gh, _replan_agent(gh))
    return Lifecycle(eng, steps=2)  # REVIEW at the threshold, REPLAN_REEXECUTE


LIFECYCLES = {
    "review-to-merge": _review_to_merge,
    "fix": _fix_with_two_effects,
    "replan": _replan,
}


@dataclass
class Effects:
    launches: int
    writes: int
    merges: int
    closes: int
    refs: str

    @classmethod
    def of(cls, eng: ControllerEngine, gh: FakeGitHub) -> Effects:
        return cls(
            launches=len(eng.provider.calls),
            writes=len(gh.effect_writes),
            merges=len(gh.merges),
            closes=len(gh.closed_prs),
            refs=_origin_refs(eng),
        )


def _origin_refs(eng: ControllerEngine) -> str:
    origin = getattr(eng, "origin", None)
    if origin is None:
        return ""
    return subprocess.run(
        ["git", "-C", str(origin.bare), "for-each-ref", "--format=%(refname) %(objectname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@dataclass
class OwnerFlip:
    """Rewrites the bound PR's marker to name OTHER_RUN at its ``at``-th read.

    A read is any fake call that hands the controller the PR's body (a view,
    an exact read, a listing); the read that flips already sees the change.
    The fake's own reads behind a call that returns no body (a PR's
    comments, its HEAD, its state) are not counted: real ``gh`` answers
    those without the body. Nor are the reads an effect makes inside its own
    ``drive`` (a K7 create lists every PR of the repository, the source
    included): they belong to that write, and GitHub has no conditional
    write, so a change landing there is the cooperative-fencing residual of
    ADR 0004 D9.4a, seen at the next check.
    """

    gh: FakeGitHub
    at: int | None
    monkeypatch: pytest.MonkeyPatch
    reads: int = 0
    before: Effects | None = None
    eng: ControllerEngine | None = None
    saves: list[Phase] = field(default_factory=list)
    inside: int = 0

    BODILESS = (
        "get_pr_comments",
        "get_pr_head_sha",
        "get_pr_state",
        "get_pr_branch",
        "get_pr_checks",
        "pr_exists",
    )

    def install(self, eng: ControllerEngine) -> None:
        self.eng = eng
        owned = self.gh._owned
        target = parse_pr_url(PR)

        def reading(pr):
            seen = owned(pr)
            if self.inside or not pr.url or not parse_pr_url(pr.url).same_target(target):
                return seen
            self.reads += 1
            if self.reads == self.at:
                assert self.eng is not None
                self.before = Effects.of(self.eng, self.gh)
                body = f"Taken over.\n\n{implementation_pr_body(owner_run_id=OTHER_RUN)}"
                self.gh.prs[PR].body = body
                self.gh.unbodied.discard(PR)
                seen.body = body
            return seen

        self.gh._owned = reading  # type: ignore[method-assign]
        for name in self.BODILESS:
            setattr(self.gh, name, self._bodiless(getattr(self.gh, name)))
        self.monkeypatch.setattr(engine_module, "drive", self._bodiless(_DRIVE))

        save = eng.save

        def saving() -> None:
            if self.before is not None:
                assert eng.state is not None
                self.saves.append(eng.state.phase)
            save()

        eng.save = eng._save = saving  # type: ignore[method-assign]

    def _bodiless(self, read: Callable) -> Callable:
        def call(*args, **kwargs):
            self.inside += 1
            try:
                return read(*args, **kwargs)
            finally:
                self.inside -= 1

        return call


def _run(lifecycle: Lifecycle, flip: OwnerFlip, *, extra: int = 1):
    """The lifecycle's steps, and ``extra`` more: the next step sees any late change."""
    eng = lifecycle.eng
    eng._save()
    flip.install(eng)
    outcomes = []
    target = parse_pr_url(PR)
    for taken in range(lifecycle.steps + extra):
        if eng.state.phase in (Phase.BLOCKED, Phase.UPDATE_EPIC):
            break
        if taken >= lifecycle.steps and not parse_pr_url(eng.state.current_pr_url).same_target(
            target
        ):
            break  # a replan activated its replacement: the PR that changed is not driven
        outcomes.append(eng.step(allow_merge=lifecycle.allow_merge))
    return outcomes


def _reads_in(make: Callable, tmp_path, monkeypatch) -> int:
    gh = FakeGitHub()
    flip = OwnerFlip(gh, at=None, monkeypatch=monkeypatch)
    lifecycle = make(tmp_path / "count", gh)
    outcomes = _run(lifecycle, flip, extra=0)
    assert all(o.next_phase != "BLOCKED" for o in outcomes), [o.message for o in outcomes]
    assert flip.reads > 0
    return flip.reads


def _restarted(eng: ControllerEngine, gh: FakeGitHub) -> ControllerEngine:
    eng.close()
    fresh = make_engine(eng.paths.state_dir, "never", github=gh, workdir=eng.workdir)
    if getattr(eng, "origin", None) is not None:
        connect_origin(fresh, eng.origin)
        fresh.origin = eng.origin
    fresh.load()
    return fresh


def test_the_state_has_no_field_that_remembers_an_observed_owner():
    """No read of the bound PR writes state: ownership is the PR's marker as
    read, never something the run saved from an earlier read."""
    fields = AutoForgeState.__dataclass_fields__
    assert not [name for name in fields if "owner" in name], sorted(fields)


@pytest.mark.parametrize("name", sorted(LIFECYCLES))
def test_an_owner_change_at_any_read_of_the_bound_pr_stops_the_run(tmp_path, monkeypatch, name):
    make = LIFECYCLES[name]
    total = _reads_in(make, tmp_path, monkeypatch)
    for at in range(1, total + 1):
        gh = FakeGitHub()
        flip = OwnerFlip(gh, at=at, monkeypatch=monkeypatch)
        lifecycle = make(tmp_path / f"flip-{at}", gh)
        eng = lifecycle.eng
        outcomes = _run(lifecycle, flip)
        where = f"{name}: owner changed at read {at} of {total}"
        assert flip.before is not None, where
        assert Effects.of(eng, gh) == flip.before, where
        last = outcomes[-1]
        if flip.before.merges:
            # Read back after the merge: the PR is done and nothing is left to fence.
            assert last.next_phase == "UPDATE_EPIC", f"{where}: {last.message}"
            continue
        if flip.before.closes:
            # Read after the replan closed it: the run drives its replacement now.
            assert eng.state.current_pr_url != PR and last.next_phase == "REVIEW", where
            continue
        assert last.next_phase == "BLOCKED", f"{where}: {last.message}"
        assert last.stop is not None and last.stop.code is StopReasonCode.PR_OWNER, where
        assert OTHER_RUN in eng.state.block_reason, where
        # Nothing after the change is saved but the stop itself, or a phase
        # whose writes were all done before it.
        assert flip.saves and flip.saves[-1] is Phase.BLOCKED, where

        again = _restarted(eng, gh)
        refused = again.unblock("try again")
        assert not refused.unblocked, where
        # A run with a replan journal is never unblocked; any other names the owner.
        journaled = load_state(again.paths.state_file).replan_transaction
        assert journaled or OTHER_RUN in refused.message, where
        assert load_state(again.paths.state_file).phase is Phase.BLOCKED, where
        assert Effects.of(again, gh).writes == flip.before.writes, where
        assert Effects.of(again, gh).merges == flip.before.merges, where
        assert Effects.of(again, gh).refs == flip.before.refs, where
        assert again.provider.calls == [], where


@pytest.mark.parametrize("removed", ["before-the-read", "after-the-read"])
def test_a_process_killed_right_after_a_read_of_its_pr_blocks_on_restart_once_unowned(
    tmp_path, removed
):
    """PR #459's P1: there, a kill between a read of the bound PR and the save
    that recorded its owner lost the observation, and once the owner was
    removed the restarted run reviewed the PR. Here a read records nothing,
    so nothing can be lost: the restarted run reads the marker again and stops."""
    gh = FakeGitHub()
    eng = _in_review(tmp_path / "state", gh, "never")
    eng._save()

    def remove_owner() -> None:
        gh.prs[PR].body = f"Hand-edited.\n\n{implementation_pr_body()}"  # names no run
        gh.unbodied.discard(PR)

    if removed == "before-the-read":
        remove_owner()
    raw = eng.paths.state_file.read_bytes()
    get_pr = gh.get_pr

    def killed_after(url):
        get_pr(url)
        raise KeyboardInterrupt

    gh.get_pr = killed_after  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    assert eng.paths.state_file.read_bytes() == raw  # the read wrote nothing
    gh.get_pr = get_pr  # type: ignore[method-assign]
    if removed == "after-the-read":
        remove_owner()
    again = _restarted(eng, gh)
    out = again.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.PR_OWNER
    assert again.provider.calls == [] and gh.effect_writes == []


def test_a_launch_whose_owner_check_cannot_read_the_pr_is_refused(tmp_path):
    """No read of the phase follows the check right before a launch, so a read
    that fails conclusively there refuses the launch rather than assume the
    PR is still this run's."""
    gh = FakeGitHub()
    eng = _in_review(tmp_path / "state", gh, [block(review_result(1, SHA_A))])
    eng._save()
    written = eng._write_read_context
    get_pr = gh.get_pr
    failing: list[bool] = []

    def context_then_fail(phase, attempt):
        out = written(phase, attempt)
        failing.append(True)
        return out

    def read(url):
        if failing:
            raise GitHubError("HTTP 403: forbidden")
        return get_pr(url)

    eng._write_read_context = context_then_fail  # type: ignore[method-assign]
    gh.get_pr = read  # type: ignore[method-assign]
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop is not None and out.stop.code is StopReasonCode.PR_OWNER
    assert "could not be read right before the launch" in out.message
    assert eng.provider.calls == [] and gh.effect_writes == []
