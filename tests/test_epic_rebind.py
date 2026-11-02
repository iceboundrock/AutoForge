"""``rebind-epic``: an existing standalone run moved to its issue's native parent (#435).

The pure decision (the request, what the state allows, what GitHub says),
then the command on real states: a run driven to READY_FOR_MERGE, runs
BLOCKED with a K8 or a K10 record, a run in UPDATE_EPIC, a scheduler's
issue. A dry run changes no byte; an applied rebind changes ``epic_url``
(and the save's ``updated_at``) alone, after its run-log record; a repeat is
``already bound``; every refusal leaves the state as it was. The crash
windows are interrupted at each persistence boundary, and a resumed run
after a rebind writes its progress comment and roadmap on the new EPIC
only.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from autoforge import cli, epic_rebind
from autoforge.config import default_config
from autoforge.effects import operator_comment_body
from autoforge.engine import ControllerEngine
from autoforge.epic_rebind import (
    MAX_REBIND_REASON_CHARS,
    REBIND_PHASES,
    REBIND_READS,
    ParentObservation,
    RebindRequest,
    observe_parent,
    parent_problems,
    parse_rebind_request,
    state_problems,
    status_lines,
)
from autoforge.errors import (
    ConfigurationError,
    EpicRebindError,
    ExecutionError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
)
from autoforge.locking import ControllerLock, repository_lock_path
from autoforge.ownership import standalone_hold
from autoforge.providers import ProviderRegistry, ScriptedProvider
from autoforge.run_namespace import issue_namespace
from autoforge.state import AutoForgeState, load_state, save_state
from autoforge.transitions import Phase
from autoforge.validation import parse_issue_url
from tests.conftest import (
    EPIC,
    ISSUE,
    PR,
    FakeGitHub,
    Origin,
    block,
    comment_url,
    connect_origin,
    fixer,
    git_repo,
    github_origin,
    implement,
    make_engine,
    progress_comment_body,
    publish_pr_head,
    read_context_of,
    review_result,
    state_tree,
)

REPO = "https://github.com/owner/repo"
NEW_EPIC = f"{REPO}/issues/255"
OTHER = f"{REPO}/issues/300"
REASON = "Correct run binding for issue #2"
OTHER_PR = f"{REPO}/pull/41"


def _request(**over) -> RebindRequest:
    words = {
        "issue_url": ISSUE,
        "from_url": EPIC,
        "to_url": NEW_EPIC,
        "reason": REASON,
        "run_id": "",
    }
    words.update(over)
    return parse_rebind_request(**words)


def _state(phase: Phase = Phase.READY_FOR_MERGE, **fields) -> AutoForgeState:
    base = AutoForgeState(
        run_id="af-20261101T000000Z-abcdef",
        repository="owner/repo",
        epic_url=EPIC,
        current_issue_url=ISSUE,
        current_pr_url=PR,
        current_branch="autoforge/2",
        phase=phase,
        created_at="2026-11-01T00:00:00+00:00",
        updated_at="2026-11-01T00:00:00+00:00",
    )
    return replace(base, **fields)


# -- the request ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over, words",
    [
        ({"to_url": "https://github.com/other/repo/issues/9"}, "not the issue's repository"),
        ({"from_url": "https://github.com/other/repo/issues/1"}, "not the issue's repository"),
        ({"to_url": ISSUE}, "is the issue itself"),
        ({"from_url": ISSUE}, "is the issue itself"),
        ({"to_url": EPIC}, "both name"),
        ({"reason": "   "}, "non-empty --reason"),
        ({"reason": "x" * (MAX_REBIND_REASON_CHARS + 1)}, "at most 1000 characters"),
        ({"run_id": "not a run id"}, "is not a run id"),
        ({"to_url": f"{REPO}/pull/255"}, ""),
    ],
)
def test_a_request_is_refused_before_anything_is_read(over, words):
    with pytest.raises(ConfigurationError, match=words or None):
        _request(**over)


def test_a_request_keeps_the_reason_stripped_and_the_urls_typed():
    request = _request(reason=f"  {REASON}\n", run_id="af-20261101T000000Z-abcdef")
    assert request.reason == REASON and request.run_id == "af-20261101T000000Z-abcdef"
    assert request.new.number == 255 and request.old.number == 1 and request.issue.number == 2


# -- what the state allows --------------------------------------------------------------------


def test_ready_for_merge_and_a_quiescent_blocked_run_are_the_only_phases():
    assert REBIND_PHASES == {Phase.READY_FOR_MERGE, Phase.BLOCKED}
    for phase in REBIND_PHASES:
        assert state_problems(_state(phase), _request()) == []


@pytest.mark.parametrize("phase", sorted(set(Phase) - REBIND_PHASES, key=lambda p: p.value))
def test_every_other_phase_is_refused_with_what_to_do(phase):
    [problem] = state_problems(_state(phase), _request())
    assert f"the run is in phase {phase.value}" in problem
    assert "a rebind is made only in BLOCKED or READY_FOR_MERGE" in problem
    if phase in (Phase.ANALYZE_EXECUTE, Phase.REVIEW, Phase.FIX, Phase.REPLAN_REEXECUTE):
        assert "'resume' until it stops at READY_FOR_MERGE" in problem
    if phase in (Phase.MERGE, Phase.UPDATE_EPIC):
        assert "manual fallback in docs/usage.md" in problem
    if phase in (Phase.DONE, Phase.FAILED):
        assert "a new 'autoforge run' binds an issue to its native parent" in problem


def _record(kind: str, stage: str, target: dict, phase: str) -> dict:
    """The fields of an effect record a refusal names (a loaded state's are complete)."""
    return {"kind": kind, "stage": stage, "target": target, "owner": {"phase": phase}}


@pytest.mark.parametrize(
    "record, shown",
    [
        (
            _record("progress_comment", "attempted", {"epic_url": EPIC}, "UPDATE_EPIC"),
            f"progress_comment (attempted) on {EPIC} (the recorded EPIC), of UPDATE_EPIC",
        ),
        (
            _record("acceptance_comment", "intended", {"issue_url": ISSUE}, "FIX"),
            f"acceptance_comment (intended) on {ISSUE}, of FIX",
        ),
        (
            _record("operator_comment", "intended", {"issue_url": EPIC}, "FIX"),
            f"operator_comment (intended) on {EPIC} (the recorded EPIC), of FIX",
        ),
        (
            _record("push", "conflict", {"repository": "owner/repo", "ref": "x"}, "FIX"),
            "push (conflict) on owner/repo, of FIX",
        ),
    ],
)
def test_a_blocked_run_keeping_any_effect_record_is_refused(record, shown):
    """K8 on the EPIC, K9 on the issue, K10 on the EPIC, a K1 left by FIX: a rebind
    never migrates, drops or relabels a journaled write."""
    [problem] = state_problems(_state(Phase.BLOCKED, effect_records=[record]), _request())
    assert shown in problem
    assert "never migrates, drops or relabels a journaled write" in problem


@pytest.mark.parametrize(
    "fields, shown",
    [
        ({"entry_observation": {"phase": "UPDATE_EPIC"}}, "the entry observation of UPDATE_EPIC"),
        ({"completion_context": {"phase": "UPDATE_EPIC"}}, "the completion context of UPDATE_EPIC"),
        ({"replan_transaction": {"stage": "planned"}}, "a REPLAN_REEXECUTE journal"),
        ({"next_issue_rejections": ["#7 is not a sub-issue"]}, "1 rejected UPDATE_EPIC"),
    ],
)
def test_a_blocked_run_keeping_epic_scoped_context_is_refused(fields, shown):
    [problem] = state_problems(_state(Phase.BLOCKED, **fields), _request())
    assert shown in problem


def test_a_merge_batch_of_another_issue_is_refused_and_the_issues_own_is_not():
    own = _state(Phase.BLOCKED, merged_since_epic_update=1, counted_merged_prs=[OTHER_PR, PR])
    assert state_problems(own, _request()) == []
    notes = epic_rebind.informational_notes(own, _request())
    assert any("this issue's merged PR is counted" in n for n in notes)
    for batch in ([OTHER_PR], [OTHER_PR, PR]):
        state = _state(
            Phase.READY_FOR_MERGE, merged_since_epic_update=len(batch), counted_merged_prs=batch
        )
        [problem] = state_problems(state, _request())
        assert f"another issue's PR ({OTHER_PR})" in problem


def test_the_identity_and_the_expected_epic_must_be_the_runs():
    state = _state()
    problems = state_problems(state, _request(issue_url=f"{REPO}/issues/5", from_url=OTHER))
    assert any("current issue is" in p and "not --issue" in p for p in problems)
    assert any("not --from" in p and "stale" in p for p in problems)
    problems = state_problems(state, _request(run_id="af-20261101T000000Z-000000"))
    assert problems == [
        "the run in this state directory is af-20261101T000000Z-abcdef, not --run-id "
        "af-20261101T000000Z-000000"
    ]
    problems = state_problems(replace(state, current_issue_url=""), _request())
    assert any("names no current issue" in p for p in problems)


# -- what GitHub says -----------------------------------------------------------------------


def _graph(parent: str | None = NEW_EPIC, epic_state: str = "OPEN") -> FakeGitHub:
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "the work", parent=parent)
    gh.add_issue(EPIC, "the recorded EPIC", parent=None)
    gh.add_issue(NEW_EPIC, "the new EPIC", state=epic_state, parent=None)
    gh.add_issue(OTHER, "another EPIC", parent=None)
    return gh


def test_the_parent_is_read_twice_around_the_epic_and_only_with_get_issue_node():
    gh = _graph()
    observed = observe_parent(gh, _request())
    assert observed == ParentObservation("open", parse_issue_url(NEW_EPIC), "open")
    assert gh.calls == [
        ("get_issue_node", ISSUE),
        ("get_issue_node", NEW_EPIC),
        ("get_issue_node", ISSUE),
    ]
    assert parent_problems(_request(), observed) == []


def test_the_module_reads_github_through_get_issue_node_alone():
    """Structural: every attribute the module takes of a reader is in REBIND_READS."""
    tree = ast.parse(inspect.getsource(epic_rebind))
    used = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "reader"
    }
    assert used == REBIND_READS == {"get_issue_node"}


@pytest.mark.parametrize(
    "parent, epic_state, words",
    [
        (None, "OPEN", "GitHub names no native parent for issue #2"),
        (OTHER, "OPEN", f"GitHub names #300 {OTHER} as the issue's native parent, not --to #255"),
        (EPIC, "OPEN", "(the EPIC --from names) as the issue's native parent"),
        (NEW_EPIC, "CLOSED", f"--to {NEW_EPIC} is closed on GitHub"),
    ],
)
def test_to_must_be_the_issues_open_native_parent(parent, epic_state, words):
    """Parent drift either way: a rebind binds what GitHub names now, never --to's claim."""
    observed = observe_parent(_graph(parent, epic_state), _request())
    [problem] = parent_problems(_request(), observed)
    assert words in problem


def test_a_parent_in_another_repository_is_refused():
    observed = ParentObservation("open", parse_issue_url("https://github.com/x/y/issues/9"), "open")
    [problem] = parent_problems(_request(), observed)
    assert "in another repository" in problem


def test_an_issue_that_changes_between_its_reads_is_refused():
    gh = _graph()
    real = gh.get_issue_node

    def moving(url):
        node = real(url)
        if url == NEW_EPIC:
            gh.parents[ISSUE] = OTHER  # re-parented while --to is read
        return node

    gh.get_issue_node = moving
    with pytest.raises(EpicRebindError, match="changed on GitHub while it was being read"):
        observe_parent(gh, _request())


@pytest.mark.parametrize(
    "error, kind",
    [
        (GitHubUnavailableError("HTTP 502"), GitHubUnavailableError),
        (GitHubNotFoundError("HTTP 404"), GitHubNotFoundError),
        (GitHubError("HTTP 403"), GitHubError),
        (ExecutionError("gh: not found"), GitHubError),
    ],
)
def test_a_failed_read_keeps_its_class_and_never_falls_back(error, kind):
    gh = _graph()
    gh.issue_node_errors[NEW_EPIC] = error
    with pytest.raises(kind, match="Nothing was changed") as raised:
        observe_parent(gh, _request())
    assert not isinstance(raised.value, EpicRebindError)


def test_status_lines_warn_only_of_another_parent():
    common = {"recorded_epic_url": EPIC, "issue_url": ISSUE, "read_error": "", "rebind_hint": "H"}
    assert status_lines(parent=parse_issue_url(EPIC), **common) == []
    [warning] = status_lines(parent=parse_issue_url(NEW_EPIC), **common)
    assert warning.startswith("WARNING:") and "Nothing was changed. H" in warning
    [note] = status_lines(parent=None, **common)
    assert note.startswith("Parent:") and "names no native parent for #2" in note
    [note] = status_lines(parent=None, **{**common, "read_error": "HTTP 502"})
    assert note == "Parent:     not read (HTTP 502); the recorded EPIC is shown unchecked"


# -- the command, on runs the engine left --------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_path_is_a_repository(tmp_path):
    github_origin(git_repo(tmp_path))


@pytest.fixture
def fakes(monkeypatch, tmp_path, tmp_path_factory):
    """Every engine the CLI builds runs on one FakeGitHub, one origin and one scripted agent.

    The agent implements, reviews clean and ends the EPIC (``next_issue_url``
    null); ``NEW_EPIC`` and ``OTHER`` exist, open and parentless.
    """
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Feature")
    gh.add_issue(NEW_EPIC, "the new EPIC", parent=None)
    gh.add_issue(OTHER, "another EPIC", parent=None)
    origin = Origin.seeded(tmp_path_factory.mktemp("rebind-origin"), git_repo(tmp_path))
    gh.attach_origin(origin)

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "UPDATE_EPIC":
            return block(
                {
                    "phase": "UPDATE_EPIC",
                    "status": "success",
                    "progress": "Issue done; the PR is merged.",
                    "roadmap_section": "- [x] done",
                    "next_issue_url": None,
                }
            )
        [pr] = gh.prs.values()  # the PR the controller opened
        return block(review_result(1, pr.head_sha))

    provider = ScriptedProvider(agent)
    real = ControllerEngine.__init__

    def patched(self, *a, **k):
        k.setdefault("github", gh)
        k.setdefault(
            "providers", ProviderRegistry(overrides={"claude": provider, "opencode": provider})
        )
        real(self, *a, **k)
        connect_origin(self, origin)

    monkeypatch.setattr(ControllerEngine, "__init__", patched)
    monkeypatch.chdir(tmp_path)
    return {"gh": gh, "provider": provider, "origin": origin}


def _gate_open_config(tmp_path) -> str:
    cfg = tmp_path / "autoforge.json"
    cfg.write_text(json.dumps({"safety": {"allow_merge": True}}))
    return str(cfg)


def _flags(tmp_path) -> list[str]:
    return ["--config", _gate_open_config(tmp_path), "--state-dir", str(tmp_path / ".autoforge")]


def _at_ready_for_merge(tmp_path, capsys, fakes) -> list[str]:
    """``run`` to READY_FOR_MERGE (the gate closed: no --allow-merge), bound to EPIC #1;
    then the issue is moved to NEW_EPIC on GitHub. Returns the global flags."""
    flags = _flags(tmp_path)
    assert cli.main([*flags, "run", "--issue", ISSUE]) == 0
    capsys.readouterr()
    state = load_state(tmp_path / ".autoforge" / "state.json")
    assert state.phase is Phase.READY_FOR_MERGE and state.epic_url == EPIC
    fakes["pr"] = state.current_pr_url
    fakes["gh"].parents[ISSUE] = NEW_EPIC
    fakes["gh"].calls.clear()
    return flags


def _rebind() -> list[str]:
    return ["rebind-epic", "--issue", ISSUE, "--from", EPIC, "--to", NEW_EPIC, "--reason", REASON]


def _events(sd: Path) -> list[dict]:
    state = load_state(sd / "state.json")
    path = sd / "logs" / state.run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _rebind_records(sd: Path) -> list[dict]:
    return [e for e in _events(sd) if e["phase"].endswith("-epic-rebind")]


def test_the_dry_run_explains_the_rebind_and_changes_no_byte(tmp_path, capsys, fakes):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    before = state_tree(sd)
    run_id = load_state(sd / "state.json").run_id
    assert cli.main([*flags, *_rebind(), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"EPIC rebind of run {run_id} (dry run: nothing written)" in out
    assert "  phase:         READY_FOR_MERGE" in out
    assert f"  issue:         #2 {ISSUE} (open on GitHub)" in out
    assert f"  PR:            {fakes['pr']} (branch autoforge/2)" in out
    assert f"  recorded EPIC: {EPIC} (matches --from)" in out
    assert f"  --to:          #255 {NEW_EPIC} (open on GitHub)" in out
    assert f"  native parent: #255 {NEW_EPIC}" in out
    assert f"  decision:      would rebind from {EPIC} to {NEW_EPIC}" in out
    assert (
        f"  apply with:    autoforge {' '.join(flags)} rebind-epic --issue {ISSUE} --from {EPIC} "
        f"--to {NEW_EPIC} --run-id {run_id} --reason 'Correct run binding for issue #2'"
    ) in out
    assert "worktree:" in out and "autoforge/worktrees/2" in out
    assert state_tree(sd) == before
    assert fakes["gh"].calls == [
        ("get_issue_node", ISSUE),
        ("get_issue_node", NEW_EPIC),
        ("get_issue_node", ISSUE),
    ]
    assert fakes["provider"].calls[-1].phase == "REVIEW"  # no agent since the run stopped


def test_an_applied_rebind_changes_the_binding_alone_and_a_repeat_is_already_bound(
    tmp_path, capsys, fakes
):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    prior = json.loads((sd / "state.json").read_text(encoding="utf-8"))
    run_id = prior["run_id"]
    assert cli.main([*flags, *_rebind(), "--run-id", run_id]) == 0
    out = capsys.readouterr().out
    assert f"decision:      rebound from {EPIC} to {NEW_EPIC}" in out
    assert "continue with: autoforge" in out and " resume" in out
    after = json.loads((sd / "state.json").read_text(encoding="utf-8"))
    changed = {k for k in prior if prior[k] != after[k]}
    assert changed == {"epic_url", "updated_at"} and set(after) == set(prior)
    assert after["epic_url"] == NEW_EPIC
    [record] = _rebind_records(sd)
    assert record["phase"] == "READY_FOR_MERGE-epic-rebind" and not record.get("error")
    meta = record["metadata"]
    assert meta["decision"] == "rebound" and meta["operator_reason"] == REASON
    assert meta["recorded_epic_url"] == EPIC and meta["to_epic_url"] == NEW_EPIC
    assert meta["from_epic_url"] == EPIC and meta["native_parent_url"] == NEW_EPIC
    assert meta["run_id"] == run_id and meta["issue_url"] == ISSUE
    assert meta["pr_url"] == fakes["pr"]
    assert meta["prior_updated_at"] == prior["updated_at"] and meta["problems"] == []
    # No GitHub write, no agent, no merge.
    assert [c[0] for c in fakes["gh"].calls] == ["get_issue_node"] * 3
    assert fakes["gh"].merges == []

    snapshot = state_tree(sd)
    fakes["gh"].calls.clear()
    for dry in ([], ["--dry-run"]):
        assert cli.main([*flags, *_rebind(), *dry]) == 0
        out = capsys.readouterr().out
        assert f"already bound to {NEW_EPIC}: nothing to do, nothing written" in out
        assert "native parent: not read (the run already records --to" in out
        assert state_tree(sd) == snapshot
    assert fakes["gh"].calls == []


def test_a_conflicting_repeat_is_refused_with_nothing_changed(tmp_path, capsys, fakes):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    assert cli.main([*flags, *_rebind()]) == 0
    capsys.readouterr()
    before = (sd / "state.json").read_bytes()
    # --from is stale now: the run records NEW_EPIC.
    words = ["rebind-epic", "--issue", ISSUE, "--from", EPIC, "--to", OTHER, "--reason", "x"]
    assert cli.main([*flags, *words]) == 2
    err = capsys.readouterr().err
    assert f"the run records EPIC {NEW_EPIC}, not --from {EPIC}" in err
    assert "Nothing was changed" in err
    assert (sd / "state.json").read_bytes() == before
    # --to is not the native parent.
    words = ["rebind-epic", "--issue", ISSUE, "--from", NEW_EPIC, "--to", OTHER, "--reason", "x"]
    assert cli.main([*flags, *words]) == 2
    assert "as the issue's native parent, not --to #300" in capsys.readouterr().err
    assert (sd / "state.json").read_bytes() == before
    refusals = [r for r in _rebind_records(sd) if r["metadata"]["decision"] == "refused"]
    assert len(refusals) == 2 and all(
        r["error"].startswith("rebind-epic refused") for r in refusals
    )


@pytest.mark.parametrize("dry", [False, True])
def test_a_failed_read_refuses_with_exit_1_and_nothing_changed(tmp_path, capsys, fakes, dry):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    before = (sd / "state.json").read_bytes()
    fakes["gh"].issue_node_errors[NEW_EPIC] = GitHubUnavailableError("HTTP 502: Bad Gateway")
    assert cli.main([*flags, *_rebind(), *(["--dry-run"] if dry else [])]) == 1
    captured = capsys.readouterr()
    assert "HTTP 502" in (captured.out if dry else captured.err)
    assert (sd / "state.json").read_bytes() == before
    assert load_state(sd / "state.json").epic_url == EPIC


def test_a_stale_issue_or_run_id_is_refused(tmp_path, capsys, fakes):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    before = state_tree(sd)
    assert (
        cli.main([*flags, *_rebind(), "--run-id", "af-20200101T000000Z-000000", "--dry-run"]) == 2
    )
    out = capsys.readouterr().out
    assert "not --run-id af-20200101T000000Z-000000" in out and "would refuse" in out
    assert state_tree(sd) == before
    assert cli.main([*flags, *_rebind(), "--run-id", "af-20200101T000000Z-000000"]) == 2
    assert "not --run-id" in capsys.readouterr().err
    assert load_state(sd / "state.json").epic_url == EPIC


# -- crash and interruption at each persistence boundary ---------------------------------------


class Crash(BaseException):
    """A kill at an exact point: not an Exception, so nothing catches it on the way out."""


@pytest.mark.parametrize("boundary", ["before the log", "after the log", "at the replace"])
def test_an_interrupted_rebind_leaves_the_old_binding_and_the_same_command_applies_it(
    tmp_path, capsys, fakes, monkeypatch, boundary
):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    before = (sd / "state.json").read_bytes()
    with monkeypatch.context() as patch:
        if boundary == "before the log":

            def crash_log(self, report):
                raise Crash()

            patch.setattr(ControllerEngine, "_log_rebind", crash_log)
        elif boundary == "after the log":

            def crash_save(self):
                raise Crash()

            patch.setattr(ControllerEngine, "save", crash_save)
        else:
            import autoforge.safefs as safefs

            real_replace = os.replace

            def crash_replace(src, dst, *a, **k):
                # The state's atomic rename alone; the run log's writes go through.
                if dst == "state.json":
                    raise Crash()
                return real_replace(src, dst, *a, **k)

            patch.setattr(safefs.os, "replace", crash_replace)
        with pytest.raises(Crash):
            cli.main([*flags, *_rebind()])
    capsys.readouterr()
    assert (sd / "state.json").read_bytes() == before
    assert load_state(sd / "state.json").epic_url == EPIC
    logged = len(_rebind_records(sd))
    assert logged == (0 if boundary == "before the log" else 1)
    assert cli.main([*flags, *_rebind()]) == 0
    capsys.readouterr()
    assert load_state(sd / "state.json").epic_url == NEW_EPIC
    decisions = [r["metadata"]["decision"] for r in _rebind_records(sd)]
    assert decisions == ["rebound"] * (logged + 1)


def test_an_interruption_after_the_save_leaves_the_new_binding_and_a_repeat_is_already_bound(
    tmp_path, capsys, fakes, monkeypatch
):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    real = ControllerEngine.save

    def save_then_crash(self):
        real(self)
        raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(ControllerEngine, "save", save_then_crash)
        with pytest.raises(Crash):
            cli.main([*flags, *_rebind()])
    capsys.readouterr()
    assert load_state(sd / "state.json").epic_url == NEW_EPIC
    snapshot = state_tree(sd)
    assert cli.main([*flags, *_rebind()]) == 0
    assert "already bound" in capsys.readouterr().out
    assert state_tree(sd) == snapshot


# -- concurrency: L0, resume, the scheduler ----------------------------------------------------


def test_a_rebind_is_refused_while_another_controller_holds_the_lock(tmp_path, capsys, fakes):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    before = state_tree(sd)
    with ControllerLock(repository_lock_path(tmp_path)):
        assert cli.main([*flags, *_rebind()]) == 2
    assert "lock" in capsys.readouterr().err.lower()
    assert state_tree(sd) == before
    assert fakes["gh"].calls == []


def test_a_resume_while_the_rebind_holds_the_lock_is_refused(tmp_path, capsys, fakes):
    """The rebind's reads happen under L0: a concurrent resume exits 2 and runs nothing."""
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    gh = fakes["gh"]
    real = gh.get_issue_node
    seen: list[int] = []

    def read(url):
        if url == NEW_EPIC and not seen:
            seen.append(cli.main([*flags, "resume", "--allow-merge"]))
        return real(url)

    gh.get_issue_node = read
    assert cli.main([*flags, *_rebind()]) == 0
    assert seen == [2]
    assert gh.merges == [] and [c.phase for c in fakes["provider"].calls] == [
        "ANALYZE_EXECUTE",
        "REVIEW",
    ]
    assert load_state(tmp_path / ".autoforge" / "state.json").epic_url == NEW_EPIC


def test_the_scheduler_keeps_the_rebound_issue_held(tmp_path, capsys, fakes):
    """The reverse guard: the standalone run holds its issue before and after the rebind,
    so a scheduler admits nothing of it, whatever EPIC it records."""
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    held = standalone_hold(sd)
    assert held.issue_number == 2
    assert cli.main([*flags, *_rebind()]) == 0
    capsys.readouterr()
    assert standalone_hold(sd) == held


def test_a_scheduler_owned_issue_is_refused_and_its_namespace_never_rebound(
    tmp_path, capsys, fakes
):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    common = Path(os.path.realpath(tmp_path / ".git"))
    namespace = issue_namespace(common, 2)
    namespace.state_dir.mkdir(parents=True)
    save_state(load_state(sd / "state.json"), namespace.state_file)
    before, ns_before = state_tree(sd), state_tree(namespace.state_dir)

    assert cli.main([*flags, *_rebind(), "--dry-run"]) == 2
    out = capsys.readouterr().out
    assert "issue #2 is owned by the parallel EPIC scheduler" in out
    assert cli.main([*flags, *_rebind()]) == 2
    err = capsys.readouterr().err
    assert f"'rebind-epic' refuses issue {ISSUE}" in err and "Nothing was written" in err

    # The namespace's own state directory: refused before the lock, and in the dry run.
    ns_flags = ["--config", flags[1], "--state-dir", str(namespace.state_dir)]
    assert cli.main([*ns_flags, *_rebind()]) == 2
    assert "'rebind-epic' refuses state directory" in capsys.readouterr().err
    assert cli.main([*ns_flags, *_rebind(), "--dry-run"]) == 2
    assert "inside the parallel EPIC scheduler's root" in capsys.readouterr().out
    # Its status warns of the parent too, but points to no rebind.
    assert cli.main([*ns_flags, "status"]) == 0
    [warning] = [line for line in capsys.readouterr().out.splitlines() if "WARNING" in line]
    assert "rebind-epic acts on standalone runs only" in warning and "--dry-run" not in warning
    assert state_tree(sd) == before and state_tree(namespace.state_dir) == ns_before


# -- refusals on real BLOCKED and UPDATE_EPIC runs -------------------------------------------


def _assert_refused_unchanged(eng, words: str) -> None:
    before = eng.paths.state_file.read_bytes()
    eng.load()
    with eng.locked(), pytest.raises(EpicRebindError) as raised:
        eng.rebind_epic(_request())
    assert words in str(raised.value) and "Nothing was changed" in str(raised.value)
    assert eng.paths.state_file.read_bytes() == before
    dry = eng.rebind_epic(_request(), dry_run=True)
    assert dry.decision == "would-refuse" and dry.exit_code == 2
    assert any(words in p for p in dry.problems)
    assert eng.paths.state_file.read_bytes() == before


def _parented(gh: FakeGitHub) -> None:
    gh.add_issue(NEW_EPIC, "the new EPIC", parent=None)
    gh.parents[ISSUE] = NEW_EPIC


def test_a_run_in_update_epic_is_refused(tmp_state_dir):
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, [], github=gh)
    gh.add_pr(state="MERGED")
    eng.state.phase = Phase.UPDATE_EPIC
    eng.state.current_pr_url = PR
    eng.state.record_merge(PR)
    eng.save()
    _parented(gh)
    _assert_refused_unchanged(eng, "the run is in phase UPDATE_EPIC")


def test_a_run_blocked_with_a_k8_record_on_the_recorded_epic_is_refused(tmp_state_dir):
    """The progress comment's journal stays the recorded EPIC's: no rebind crosses it."""
    gh = FakeGitHub()
    epic_result = block(
        {
            "phase": "UPDATE_EPIC",
            "status": "success",
            "progress": "Shipped.",
            "roadmap_section": "- [x] shipped",
            "next_issue_url": None,
        }
    )
    eng = make_engine(tmp_state_dir, [epic_result], github=gh)
    gh.add_pr(state="MERGED")
    eng.state.phase = Phase.UPDATE_EPIC
    eng.state.current_pr_url = PR
    eng.state.last_review_result = "clean"
    eng.state.record_merge(PR)
    eng.save()
    real = eng._persist_effect
    calls = {"n": 0}

    def crash_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("power loss")
        return real(*a, **k)

    eng._persist_effect = crash_once
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    gh.add_comment(EPIC, 310, progress_comment_body())  # someone else's, meanwhile
    eng.load()
    assert eng.step().next_phase == "BLOCKED"
    state = load_state(eng.paths.state_file)
    assert [r["kind"] for r in state.effect_records] == ["progress_comment"]
    _parented(gh)
    _assert_refused_unchanged(eng, f"on {EPIC} (the recorded EPIC), of UPDATE_EPIC")


def test_a_run_blocked_on_an_operator_action_for_the_recorded_epic_is_refused(tmp_state_dir):
    """K10: FIX asks the operator to comment on the EPIC (#1, allowed as the run's EPIC)."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Ship the feature")
    note = "Coordination note for the EPIC: the scheduler contract changed."
    resolution = {
        "finding_id": "R1-F1",
        "resolution": "operator_acceptance",
        "rationale": "The finding asks for a coordination note on the EPIC; only the operator "
        "may post it, so the fixer requests it instead of posting.",
        "operator_acceptance": {
            "actions": [{"kind": "related_issue_comment", "issue": 1, "body": note}]
        },
    }
    cfg = default_config()
    cfg.safety = replace(cfg.safety, acceptance_actions="approve")
    eng = make_engine(tmp_state_dir, fixer(resolution), github=gh, cfg=cfg, origin=True)
    head = publish_pr_head(eng)
    gh.add_pr(head_sha=head)
    eng.state.phase = Phase.FIX
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = head
    eng.state.reviewed_head_sha = head
    eng.state.review_round = 1
    eng.state.last_review_comment_url = comment_url(PR, 100)
    eng.state.open_findings = [
        {
            "id": "R1-F1",
            "classification": "bug",
            "title": "notes missing",
            "location": "docs/adr.md:1",
            "required_resolution": "publish the coordination note",
        }
    ]
    assert eng.step().next_phase == "BLOCKED"
    state = load_state(eng.paths.state_file)
    k10 = [r for r in state.effect_records if r["kind"] == "operator_comment"]
    assert [r["target"]["issue_url"] for r in k10] == [EPIC]
    assert k10[0]["payload"]["body"] == operator_comment_body(note, ISSUE, PR, 1, "R1-F1", EPIC)
    _parented(gh)
    _assert_refused_unchanged(eng, f"operator_comment (intended) on {EPIC} (the recorded EPIC)")


# -- the run after a rebind ---------------------------------------------------------------


def test_a_resumed_run_writes_progress_and_roadmap_on_the_new_epic_only(tmp_path, capsys, fakes):
    """No merge without the gate; then MERGE and UPDATE_EPIC act on NEW_EPIC: its prompt,
    its read context, its progress comment and its roadmap. Nothing is written to EPIC, and
    no counter, round or budget was reset by the rebind."""
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    gh = fakes["gh"]
    sd = tmp_path / ".autoforge"
    prior = load_state(sd / "state.json")
    assert cli.main([*flags, *_rebind()]) == 0
    capsys.readouterr()
    rebound = load_state(sd / "state.json")
    for name in ("step_count", "review_round", "execution_attempt", "escalation_count"):
        assert getattr(rebound, name) == getattr(prior, name), name
    assert rebound.counted_merged_prs == prior.counted_merged_prs == []
    assert rebound.review_certificate == prior.review_certificate

    # The gate stays closed without --allow-merge: the rebind granted nothing.
    assert cli.main([*flags, "resume"]) == 0
    assert "The merge gate is closed" in capsys.readouterr().out
    assert gh.merges == []

    assert cli.main([*flags, "resume", "--allow-merge"]) == 0
    capsys.readouterr()
    assert load_state(sd / "state.json").phase is Phase.DONE
    assert len(gh.merges) == 1
    [update] = [c for c in fakes["provider"].calls if c.phase == "UPDATE_EPIC"]
    assert NEW_EPIC in update.prompt
    context = read_context_of(update.prompt)
    epic_file = (context / "epic.md").read_text(encoding="utf-8")
    assert NEW_EPIC in epic_file and "the new EPIC" in epic_file
    assert f"{EPIC}\n" not in epic_file
    assert f"- {ISSUE}: open" in (context / "epic-sub-issues.md").read_text(encoding="utf-8")
    writes = [c for c in gh.calls if c[0] in ("create_issue_comment", "edit_issue_body")]
    assert [w[:2] for w in writes] == [
        ("create_issue_comment", NEW_EPIC),
        ("edit_issue_body", NEW_EPIC),
    ]
    assert not any(EPIC == c[1] for c in gh.calls if c[0].startswith(("create_", "edit_")))
    assert gh.comments.get(EPIC, []) == []
