"""Who may act on an issue: the forward and reverse guards of ADR 0008 D17.2 (#180).

The forward guard's predicate (a scheduler namespace owns its issue when it
holds a state or a live worker holds its run lock; fail-closed) and the
reverse helper (the issue a standalone state holds). Both only read. The
CLI's use of the forward guard is tested in ``test_cli.py``, the engine's in
``test_engine.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoforge.errors import ConfigurationError, SchedulerOwnedIssueError
from autoforge.locking import IssueRunLock
from autoforge.ownership import (
    MAX_REASON_CHARS,
    StandaloneHold,
    refuse_scheduler_owned_issue,
    scheduler_ownership,
    standalone_hold,
    standalone_ownership_predicate,
)
from autoforge.run_namespace import EpicRef, RunBinding, RunKey, issue_namespace, write_binding
from autoforge.state import AutoForgeState, save_state
from autoforge.transitions import Phase, WorkflowMode

from .conftest import EPIC, git_repo, sample_contract


def _issue_url(n: int) -> str:
    return f"https://github.com/owner/repo/issues/{n}"


def _state(n: int | None, phase: Phase = Phase.ANALYZE_EXECUTE, **kw) -> AutoForgeState:
    fields = dict(
        run_id=f"af-issue-{n}",
        repository="owner/repo",
        epic_url=EPIC,
        current_issue_url=_issue_url(n) if n is not None else "",
        phase=phase,
        created_at="2026-10-18T00:00:00+00:00",
        updated_at="2026-10-18T00:00:00+00:00",
    )
    fields.update(kw)
    return AutoForgeState(**fields)  # type: ignore[arg-type]


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every entry under ``root`` (links unfollowed), with a file's bytes."""
    out: dict[str, bytes | None] = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            regular = os.path.isfile(path) and not os.path.islink(path)
            out[os.path.relpath(path, root)] = Path(path).read_bytes() if regular else None
    return out


@pytest.fixture
def common(tmp_path) -> Path:
    """The coordination root: a real repository's git common dir, resolved."""
    return Path(os.path.realpath(git_repo(tmp_path / "repo") / ".git"))


def _own_by_state(common: Path, n: int, phase: Phase = Phase.REVIEW) -> None:
    ns = issue_namespace(common, n)
    with ns.state_paths().open_root(create=True) as root:
        save_state(_state(n, phase), ns.state_file, root=root)


# -- the forward guard's predicate ---------------------------------------------------


def test_no_namespace_owns_nothing_and_nothing_is_created(common):
    before = _tree(common)
    assert scheduler_ownership(common, 7) == ""
    assert _tree(common) == before


@pytest.mark.parametrize("phase", [Phase.REVIEW, Phase.BLOCKED, Phase.FAILED, Phase.DONE])
def test_a_namespace_holding_any_state_owns_its_issue(common, phase):
    """Any state, a terminal one included: only `epic archive` releases it."""
    _own_by_state(common, 7, phase)
    reason = scheduler_ownership(common, 7)
    assert f"holds a state (phase {phase.value})" in reason
    assert str(issue_namespace(common, 7).directory) in reason
    assert scheduler_ownership(common, 8) == "", "only its own issue"


def test_a_namespace_holding_a_state_that_cannot_be_read_owns_its_issue(common):
    ns = issue_namespace(common, 7)
    ns.state_dir.mkdir(parents=True)
    ns.state_file.write_text("{ not json")
    before = _tree(common)
    assert "holds a state that cannot be read" in scheduler_ownership(common, 7)
    assert _tree(common) == before, "never quarantined"


def test_a_bound_namespace_without_a_state_or_a_live_owner_does_not_own(common):
    """The claim half of the predicate is #181's; a binding alone is no ownership."""
    ns = issue_namespace(common, 7)
    write_binding(
        ns,
        RunBinding(
            run_key=RunKey(700_001, 7),
            issue_id=9_007,
            repository_owner="owner",
            repository_name="repo",
            epic=EpicRef(number=1, issue_id=8_001),
            worktree_path="/work/autoforge/worktrees/7",
            created_at="2026-10-18T00:00:00+00:00",
        ),
    )
    assert scheduler_ownership(common, 7) == ""


def test_a_live_run_lock_owns_its_issue(common):
    ns = issue_namespace(common, 7)
    with IssueRunLock(ns):
        assert "a live scheduler worker holds its run lock" in scheduler_ownership(common, 7)
    assert scheduler_ownership(common, 7) == ""


@pytest.mark.parametrize("damage", ["binding", "linked namespace", "linked run lock"])
def test_a_namespace_that_cannot_be_read_owns_its_issue(common, tmp_path, damage):
    """Fail-closed: what cannot be read may be a live scheduler's."""
    ns = issue_namespace(common, 7)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if damage == "binding":
        ns.directory.mkdir(parents=True)
        ns.binding_file.write_text("{ not json")
        expected = "cannot be read"
    elif damage == "linked namespace":
        ns.directory.parent.mkdir(parents=True)
        ns.directory.symlink_to(elsewhere, target_is_directory=True)
        expected = "cannot be read"
    else:
        ns.directory.mkdir(parents=True)
        ns.run_lock_file.symlink_to(elsewhere / "run.lock")
        expected = "cannot be probed"
    before = _tree(common), _tree(elsewhere)
    reason = scheduler_ownership(common, 7)
    assert expected in reason and "may own the issue" in reason
    assert (_tree(common), _tree(elsewhere)) == before


def test_an_issue_number_no_namespace_can_have_is_refused(common):
    assert "no scheduler namespace can be named" in scheduler_ownership(common, 2**63)


def test_a_reason_quoting_a_loader_is_redacted_and_bounded(common):
    ns = issue_namespace(common, 7)
    ns.directory.mkdir(parents=True)
    ns.binding_file.write_text('{"x": "' + "ghp_" + "a" * 36 + '", "y": "' + "z" * 5000 + '"}')
    reason = scheduler_ownership(common, 7)
    assert "ghp_" + "a" * 36 not in reason
    assert len(reason) < MAX_REASON_CHARS + 500


def test_the_standalone_predicate_exempts_only_the_namespace_unblock_acts_on(common):
    _own_by_state(common, 7)
    _own_by_state(common, 8)
    owner = standalone_ownership_predicate(common, exempt_issue=7)
    assert owner(7) == ""
    assert "holds a state" in owner(8)
    assert "holds a state" in standalone_ownership_predicate(common)(7)


def test_refuse_names_the_scheduler_commands_and_writes_nothing(common):
    _own_by_state(common, 7)
    before = _tree(common)
    with pytest.raises(SchedulerOwnedIssueError) as refused:
        refuse_scheduler_owned_issue(common, _issue_url(7), "step")
    message = str(refused.value)
    assert message.startswith(f"'step' refuses issue {_issue_url(7)}: its scheduler namespace")
    assert "'autoforge epic resume'" in message and "'autoforge epic status'" in message
    assert "'autoforge epic archive <epic> 7'" in message
    assert "Nothing was written." in message
    assert isinstance(refused.value, ConfigurationError), "exit 2"
    assert _tree(common) == before
    refuse_scheduler_owned_issue(common, _issue_url(7), "unblock", exempt_issue=7)
    refuse_scheduler_owned_issue(common, _issue_url(8), "step")
    refuse_scheduler_owned_issue(common, "", "step")  # no issue named: nothing to refuse
    refuse_scheduler_owned_issue(common, "not a url", "step")


# -- the reverse helper ----------------------------------------------------------------


def _save_standalone(state_dir: Path, state: AutoForgeState) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    save_state(state, state_dir / "state.json")


@pytest.mark.parametrize(
    "phase",
    [Phase.ANALYZE_EXECUTE, Phase.REVIEW, Phase.MERGE, Phase.BLOCKED, Phase.FAILED],
)
def test_a_standalone_state_before_done_holds_its_issue(tmp_path, phase):
    sd = tmp_path / ".autoforge"
    _save_standalone(sd, _state(7, phase))
    hold = standalone_hold(sd)
    assert hold == StandaloneHold(
        sd / "state.json", issue_number=7, issue_url=_issue_url(7), phase=phase.value
    )
    assert hold.holds


@pytest.mark.parametrize("state", ["absent dir", "absent file", "done", "no issue", "local"])
def test_what_holds_nothing(tmp_path, state):
    sd = tmp_path / ".autoforge"
    if state == "absent file":
        sd.mkdir()
    elif state == "done":
        _save_standalone(sd, _state(7, Phase.DONE))
    elif state == "no issue":
        _save_standalone(sd, _state(None, Phase.UPDATE_EPIC))
    elif state == "local":
        _save_standalone(
            sd,
            AutoForgeState(
                run_id="af-local",
                mode=WorkflowMode.LOCAL,
                phase=Phase.REVIEW,
                feature_spec_path="features/x.md",
                feature_spec_sha256="a" * 64,
                local_run_contract=sample_contract(
                    repository_root=str(tmp_path), state_root=str(sd)
                ),
                workspace_fingerprint="b" * 64,
                base_head_sha="c" * 40,
                base_branch="main",
                created_at="2026-10-18T00:00:00+00:00",
                updated_at="2026-10-18T00:00:00+00:00",
            ),
        )
    hold = standalone_hold(sd)
    assert not hold.holds and hold.problem == ""
    assert not os.path.lexists(sd) or state != "absent dir"


@pytest.mark.parametrize("damage", ["garbage", "symlink"])
def test_an_unreadable_standalone_state_is_reported_and_holds_nothing(tmp_path, damage):
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    if damage == "garbage":
        (sd / "state.json").write_text("{ not json " + "ghp_" + "b" * 36)
    else:
        target = tmp_path / "elsewhere.json"
        save_state(_state(7), target)
        (sd / "state.json").symlink_to(target)
    before = _tree(sd)
    hold = standalone_hold(sd)
    assert not hold.holds and hold.problem
    assert "ghp_" + "b" * 36 not in hold.problem
    assert len(hold.problem) <= MAX_REASON_CHARS
    assert _tree(sd) == before, "never quarantined, created or written"
