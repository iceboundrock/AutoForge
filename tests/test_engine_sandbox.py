"""The engine's sandbox routing with scripted agents (ADR 0005, #168).

No launch reaches a backend here: the scripted provider compiles what the
engine asked for and records it, so these tests read the description each
launch class gets, the run log, and the mode-independent git hardening
(binding, candidate addressing, strict import). The real backend runs the
same routes in ``test_sandbox_routing.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autoforge.config import default_config
from autoforge.errors import (
    ControlResultValidationError,
    SandboxLaunchRefusedError,
    VerificationError,
)
from autoforge.sandbox import Access, Bind, Mask, Overlay
from autoforge.state import load_state
from autoforge.transitions import Phase
from tests.conftest import (
    GIT_IDENT,
    analyze_payload,
    block,
    commit_in,
    git_out,
    implement,
    make_engine,
    scripted,
)


def _required():
    cfg = default_config()
    cfg.sandbox.mode = "required"
    return cfg


def _analyzed(eng):
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    return eng.step()


def _executions(eng, phase: str) -> list[dict]:
    """Each launch of ``phase``: its ``request.json`` with ``execution.json`` under "execution"."""
    run_dir = eng.paths.logs_dir / eng.state.run_id
    found = []
    for step in sorted(p for p in run_dir.iterdir() if (p / "request.json").exists()):
        request = json.loads((step / "request.json").read_text(encoding="utf-8"))
        if request["phase"] == phase:
            execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
            found.append({**request, "execution": execution})
    return found


def _at(description, target: Path):
    found = [m for m in description.mounts if m.target == str(target)]
    assert found, f"nothing mounted at {target}"
    return found[0]


def test_disabled_mode_launches_without_a_sandbox(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    assert _analyzed(eng).next_phase == "REVIEW"
    (req,) = eng.provider.calls
    assert req.sandbox is None and eng.provider.descriptions == []
    (record,) = _executions(eng, "ANALYZE_EXECUTE")
    assert record["metadata"]["sandbox_mode"] == "disabled"


def test_a_required_remote_agent_gets_its_worktree_an_overlay_and_a_cache(
    tmp_state_dir, fake_github
):
    eng = make_engine(tmp_state_dir, implement, github=fake_github, cfg=_required(), origin=True)
    out = _analyzed(eng)
    assert out.next_phase == "REVIEW", out.message
    (req,) = eng.provider.calls
    (description,) = eng.provider.descriptions
    worktree = Path(req.cwd)
    common = Path(os.path.realpath(tmp_state_dir.parent)) / ".git"
    sandbox = common / "autoforge" / "sandbox" / "2"
    assert _at(description, worktree).access is Access.WRITABLE
    assert _at(description, worktree / ".git").access is Access.READ_ONLY
    assert _at(description, common / "worktrees" / "2").access is Access.WRITABLE
    assert _at(description, common).access is Access.READ_ONLY
    assert isinstance(_at(description, common / "autoforge"), Mask)
    assert isinstance(_at(description, common / "worktrees"), Mask)
    overlay = _at(description, common / "objects")
    assert isinstance(overlay, Overlay) and overlay.upper == str(sandbox / "objects")
    home = Path(os.path.normpath(os.environ["HOME"]))
    cache = _at(description, home / ".cache")
    assert isinstance(cache, Bind) and cache.source == str(sandbox / "cache")
    # The operator's checkout, the state directory and the run logs are not there.
    for hidden in (tmp_state_dir.parent / "README.md", tmp_state_dir, eng.paths.logs_dir):
        assert description.inside_path(os.path.realpath(hidden)) is None
    (record,) = _executions(eng, "ANALYZE_EXECUTE")
    assert record["metadata"]["sandbox_mode"] == "required"


def test_a_required_review_has_no_persistent_cache(tmp_state_dir, fake_github):
    eng = make_engine(
        tmp_state_dir, scripted(implement), github=fake_github, cfg=_required(), origin=True
    )
    assert _analyzed(eng).next_phase == "REVIEW"
    with pytest.raises(ControlResultValidationError):
        eng.step()  # the review agent answers nothing; its description is recorded
    assert [c.phase for c in eng.provider.calls] == ["ANALYZE_EXECUTE", "REVIEW", "REVIEW"]
    home = Path(os.path.normpath(os.environ["HOME"]))
    assert any(m.target == str(home / ".cache") for m in eng.provider.descriptions[0].mounts)
    for review in eng.provider.descriptions[1:]:
        assert not any(m.target == str(home / ".cache") for m in review.mounts)
        assert isinstance(_at(review, Path(eng.provider.calls[1].cwd)), Bind)


def test_a_refused_launch_is_recorded_and_nothing_runs(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, None, github=fake_github, cfg=_required(), origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    # A dangling link where the issue's sandbox directories go: D8.4.
    sandbox = Path(os.path.realpath(tmp_state_dir.parent)) / ".git" / "autoforge" / "sandbox"
    sandbox.parent.mkdir(parents=True, exist_ok=True)
    sandbox.symlink_to(tmp_state_dir.parent / "elsewhere")
    with pytest.raises(SandboxLaunchRefusedError, match="sandbox cannot be established"):
        eng.step()
    assert eng.provider.calls == []
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE
    (record,) = _executions(eng, "ANALYZE_EXECUTE")
    assert record["metadata"]["sandbox_mode"] == "required"
    assert "SandboxLaunchRefusedError" in record["execution"]["error"]
    assert not (tmp_state_dir.parent / "elsewhere").exists()


def test_an_alternates_file_in_the_private_store_blocks_the_run(tmp_state_dir, fake_github):
    def tampers(req) -> str:
        head = commit_in(req.cwd)
        private = Path(os.path.realpath(req.cwd)).parents[1] / "sandbox" / "2" / "objects"
        (private / "info").mkdir(parents=True)
        (private / "info" / "alternates").write_text("/elsewhere/objects\n")
        return block(analyze_payload(head))

    eng = make_engine(tmp_state_dir, tampers, github=fake_github, cfg=_required(), origin=True)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    assert "info/alternates" in out.message and "tampered" in out.message
    assert fake_github.effect_writes == []
    assert eng.origin.head("autoforge/2") is None


def test_a_private_store_replaced_by_a_link_blocks_the_run(tmp_state_dir, fake_github, tmp_path):
    """In every mode: a link where the issue's private object directory goes
    is never read through, nor taken for an empty store (D6.5)."""
    decoy = tmp_path / "decoy"
    git_out("init", "-q", "-b", "main", str(decoy))

    def links(req) -> str:
        head = commit_in(req.cwd)
        private = Path(os.path.realpath(req.cwd)).parents[1] / "sandbox" / "2" / "objects"
        private.parent.mkdir(parents=True)
        private.symlink_to(decoy / ".git" / "objects")
        return block(analyze_payload(head))

    eng = make_engine(tmp_state_dir, links, github=fake_github, origin=True)
    out = _analyzed(eng)
    assert out.next_phase == "BLOCKED"
    assert "objects is a link" in out.message and "tampered" in out.message
    assert fake_github.effect_writes == []
    assert eng.origin.head("autoforge/2") is None


def test_the_candidate_is_read_through_the_registry_not_the_worktrees_git_file(
    tmp_state_dir, fake_github, tmp_path
):
    """D6.3, D6.4: an agent that points its ``.git`` at another repository
    and reports that repository's HEAD gets a candidate refusal; the decoy's
    commit is never what the controller reads or pushes, and the next launch
    refuses the unbound worktree, naming the repair."""
    decoy = tmp_path / "decoy"
    git_out("init", "-q", "-b", "main", str(decoy))
    git_out("-C", str(decoy), *GIT_IDENT, "commit", "-q", "--allow-empty", "-m", "decoy")
    decoy_head = git_out("-C", str(decoy), "rev-parse", "HEAD")
    heads: list[str] = []

    def redirects(req) -> str:
        heads.append(commit_in(req.cwd))
        Path(req.cwd, ".git").write_text(f"gitdir: {decoy / '.git'}\n")
        return block(analyze_payload(decoy_head))

    eng = make_engine(
        tmp_state_dir, scripted(redirects, redirects), github=fake_github, origin=True
    )
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    with pytest.raises(ControlResultValidationError) as caught:
        eng.step()
    # Both launches: the HEAD read through the common dir is the agent's
    # first commit in the real worktree, never the decoy's.
    assert f"is {decoy_head}, but the worktree's HEAD is {heads[0]}" in str(caught.value)
    assert eng.provider.calls[1].correction
    assert eng.origin.head("autoforge/2") is None and fake_github.effect_writes == []
    with pytest.raises(VerificationError, match="git worktree repair"):
        eng.step()
    assert len(eng.provider.calls) == 2


def test_a_scripted_refusal_from_the_compiler_is_a_launch_failure(tmp_state_dir, fake_github):
    """A declaration the policy refuses is a typed refusal on the spawn path."""
    eng = make_engine(tmp_state_dir, None, github=fake_github, cfg=_required(), origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"

    def refuses(req):
        raise SandboxLaunchRefusedError("the sandbox cannot be established: test refusal")

    eng.provider.execute = refuses  # type: ignore[method-assign]
    with pytest.raises(SandboxLaunchRefusedError, match="test refusal"):
        eng.step()
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE
    assert fake_github.effect_writes == []


def test_the_dry_run_plan_names_the_sandbox_mode(tmp_state_dir, fake_github):
    eng = make_engine(tmp_state_dir, None, github=fake_github)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    (plan,) = [o.plan for o in eng.run(max_steps=1, dry_run=True)]
    assert any(n.startswith("outer sandbox: disabled (sandbox.mode)") for n in plan.notes)
    eng.config.sandbox.mode = "required"
    eng.config.sandbox.validation_network = "isolated"
    (plan,) = [o.plan for o in eng.run(max_steps=1, dry_run=True)]
    (note,) = [n for n in plan.notes if n.startswith("outer sandbox:")]
    assert "required" in note and "never without it" in note
    assert "validation network isolated" in note
