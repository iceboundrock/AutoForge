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
import contextlib
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
    post_progress_comment,
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


@pytest.mark.parametrize("since", [1, 0], ids=["roadmap-pending", "roadmap-written"])
def test_a_counted_merge_of_the_runs_own_pr_is_refused(since):
    """MERGE counts the PR in the save that enters UPDATE_EPIC: a counted one is an
    UPDATE_EPIC of this issue on the recorded EPIC, next or already run (R1-F2)."""
    own = _state(Phase.BLOCKED, merged_since_epic_update=since, counted_merged_prs=[OTHER_PR, PR])
    [problem] = state_problems(own, _request())
    assert f"the run's PR {PR} is merged and counted" in problem
    assert "a write to the recorded EPIC cannot be ruled out" in problem
    assert "manual fallback in docs/usage.md" in problem
    assert not any(
        "merged PR is counted" in n for n in epic_rebind.informational_notes(own, _request())
    )
    # Not counted yet (merged by hand, MERGE not run): nothing of UPDATE_EPIC happened.
    assert (
        state_problems(_state(Phase.READY_FOR_MERGE, counted_merged_prs=[OTHER_PR]), _request())
        == []
    )


def test_a_merge_batch_of_another_issue_is_refused():
    for batch in ([OTHER_PR], [OTHER_PR, PR]):
        state = _state(
            Phase.READY_FOR_MERGE, merged_since_epic_update=len(batch), counted_merged_prs=batch
        )
        problems = state_problems(state, _request())
        assert any(f"another issue's PR ({OTHER_PR})" in p for p in problems)
        assert any("is merged and counted" in p for p in problems) is (PR in batch)


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


def test_the_module_reads_github_through_rebind_reads_alone():
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
    assert used == REBIND_READS == {"get_issue_node", "get_issue_comments"}


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


def test_the_recorded_epics_comments_refuse_a_progress_comment_of_the_issue_for_any_pr():
    """R1-F2: a progress comment for the issue on the EPIC the run leaves would stay there
    while UPDATE_EPIC posts another on --to; another issue's is the EPIC's normal history."""
    gh = _graph()
    gh.add_comment(EPIC, 301, progress_comment_body(f"{REPO}/issues/7", OTHER_PR))
    observed = epic_rebind.observe_recorded_epic(gh, _state())
    assert observed is not None and observed.comments == 1 and observed.progress_urls == ()
    assert epic_rebind.recorded_epic_problems(observed) == []
    assert gh.calls == [("get_issue_comments", EPIC)]
    gh.add_comment(EPIC, 302, progress_comment_body(ISSUE, f"{REPO}/pull/99"))  # another PR
    observed = epic_rebind.observe_recorded_epic(gh, _state())
    [problem] = epic_rebind.recorded_epic_problems(observed)
    assert f"already carries 1 progress comment(s) for issue #2 ({comment_url(EPIC, 302)})" in (
        problem
    )
    assert "let UPDATE_EPIC post another on --to" in problem


def test_an_unreadable_progress_marker_on_the_recorded_epic_is_refused():
    gh = _graph()
    gh.add_comment(EPIC, 303, "Progress\n\n<!-- ai-epic-progress: {not json -->\n")
    observed = epic_rebind.observe_recorded_epic(gh, _state())
    [problem] = epic_rebind.recorded_epic_problems(observed)
    assert "already carries a progress comment for issue #2 cannot be established" in problem


@pytest.mark.parametrize(
    "error, kind",
    [
        (GitHubUnavailableError("HTTP 502"), GitHubUnavailableError),
        (GitHubError("HTTP 403"), GitHubError),
        (ExecutionError("gh: not found"), GitHubError),
    ],
)
def test_a_failed_read_of_the_recorded_epics_comments_is_never_an_empty_listing(error, kind):
    gh = _graph()
    gh.comments_error = error
    with pytest.raises(kind, match="reading the comments of the recorded EPIC") as raised:
        epic_rebind.observe_recorded_epic(gh, _state())
    assert not isinstance(raised.value, EpicRebindError)


def _transition(**over) -> epic_rebind.RebindTransition:
    fields = {
        "run_id": "af-20261101T000000Z-abcdef",
        "issue": parse_issue_url(ISSUE),
        "old": parse_issue_url(EPIC),
        "new": parse_issue_url(NEW_EPIC),
        "prior_updated_at": "2026-11-01T00:00:00+00:00",
        "at": "2026-11-01T01:00:00+00:00",
    }
    fields.update(over)
    return epic_rebind.RebindTransition(**fields)


def test_a_repeat_is_the_journaled_transition_in_effect_and_nothing_else():
    """R1-F3: --to recorded is ``already-bound`` only for the rebind that bound it."""
    bound = _state(epic_url=NEW_EPIC)
    journal = [_transition()]
    assert epic_rebind.is_already_bound(bound, _request(), journal)
    assert state_problems(bound, _request(), journal) == []
    # Another --from with the same --to: no repeat of the rebind that happened.
    other = _request(from_url=OTHER)
    assert not epic_rebind.is_already_bound(bound, other, journal)
    [problem] = state_problems(bound, other, journal)
    assert f"since its rebind from {EPIC} for issue {ISSUE}" in problem
    assert f"names --from {OTHER}" in problem and "not a repeat of that rebind" in problem
    # A run bound to --to since it was created: nothing in the journal.
    for empty in ([], [_transition(run_id="af-20261101T000000Z-000000")]):
        [problem] = state_problems(bound, _request(), empty)
        assert "no rebind of run af-20261101T000000Z-abcdef to it is in its journal" in problem
    # The latest entry to the recorded EPIC decides: EPIC -> 255, 255 -> 300, 300 -> 255.
    back = [
        _transition(),
        _transition(old=parse_issue_url(NEW_EPIC), new=parse_issue_url(OTHER)),
        _transition(old=parse_issue_url(OTHER), new=parse_issue_url(NEW_EPIC)),
    ]
    assert epic_rebind.is_already_bound(bound, other, back)
    assert not epic_rebind.is_already_bound(bound, _request(), back)
    # An entry whose save never happened (255 -> 300 while the run still records 255).
    unsaved = [
        _transition(),
        _transition(old=parse_issue_url(NEW_EPIC), new=parse_issue_url(OTHER)),
    ]
    assert epic_rebind.is_already_bound(bound, _request(), unsaved)


def test_the_journal_is_strict_and_bounded():
    many = [_transition(at=f"2026-11-01T00:00:{i:02d}+00:00") for i in range(40)]
    data = epic_rebind.render_journal(many)
    kept = epic_rebind.parse_journal(data)
    assert len(kept) == epic_rebind.MAX_JOURNAL_TRANSITIONS and kept == many[-32:]
    assert epic_rebind.parse_journal(None) == []
    good = json.loads(data)
    bad = [
        b"\xff",
        b"[]",
        json.dumps({**good, "extra": 1}).encode(),
        json.dumps({**good, "schema": 2}).encode(),
        json.dumps({**good, "schema": True}).encode(),
        json.dumps({**good, "transitions": good["transitions"] * 2}).encode(),
        json.dumps({**good, "transitions": [{**good["transitions"][0], "x": ""}]}).encode(),
        json.dumps({**good, "transitions": [{**good["transitions"][0], "run_id": 1}]}).encode(),
        json.dumps(
            {**good, "transitions": [{**good["transitions"][0], "to_epic_url": "x"}]}
        ).encode(),
    ]
    for raw in bad:
        with pytest.raises(EpicRebindError, match="cannot read its journal of applied rebinds"):
            epic_rebind.parse_journal(raw)


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
    assert (
        "  old progress:  none (progress comments for #2 on the recorded EPIC, 0 comment(s) read)"
        in out
    )
    assert "note:          a preview reads without the controller lock; the applied command" in out
    assert f"  decision:      would rebind from {EPIC} to {NEW_EPIC}" in out
    assert (
        f"  apply with:    autoforge {' '.join(flags)} rebind-epic --issue {ISSUE} --from {EPIC} "
        f"--to {NEW_EPIC} --run-id {run_id} --reason 'Correct run binding for issue #2'"
    ) in out
    assert "worktree:" in out and "autoforge/worktrees/2" in out
    assert state_tree(sd) == before
    assert not (sd / epic_rebind.JOURNAL_NAME).exists()
    assert fakes["gh"].calls == [
        ("get_issue_node", ISSUE),
        ("get_issue_node", NEW_EPIC),
        ("get_issue_node", ISSUE),
        ("get_issue_comments", EPIC),
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
    assert (
        meta["recorded_epic_progress_comments"] == [] and meta["recorded_epic_comments_read"] == 0
    )
    # No GitHub write, no agent, no merge.
    assert [c[0] for c in fakes["gh"].calls] == ["get_issue_node"] * 3 + ["get_issue_comments"]
    assert fakes["gh"].merges == []
    # The journal holds the transition, bound to the run, the issue and the prior state.
    journal = json.loads((sd / epic_rebind.JOURNAL_NAME).read_text(encoding="utf-8"))
    [entry] = journal["transitions"]
    assert journal["schema"] == 1 and entry["run_id"] == run_id
    assert (entry["issue_url"], entry["from_epic_url"], entry["to_epic_url"]) == (
        ISSUE,
        EPIC,
        NEW_EPIC,
    )
    assert entry["prior_updated_at"] == prior["updated_at"]
    assert os.stat(sd / epic_rebind.JOURNAL_NAME).st_mode & 0o777 == 0o600

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
    journal = (sd / epic_rebind.JOURNAL_NAME).read_bytes()
    # Another --from with the same --to (R1-F3): it names the recorded EPIC, but repeats no
    # rebind that happened, so it is refused, never reported as already applied.
    words = ["rebind-epic", "--issue", ISSUE, "--from", OTHER, "--to", NEW_EPIC, "--reason", "x"]
    snapshot = state_tree(sd)
    assert cli.main([*flags, *words, "--dry-run"]) == 2
    out = capsys.readouterr().out
    assert "already bound" not in out and "would refuse" in out
    assert f"since its rebind from {EPIC} for issue {ISSUE}" in out
    assert state_tree(sd) == snapshot
    assert cli.main([*flags, *words]) == 2
    err = capsys.readouterr().err
    assert f"names --from {OTHER} for --issue {ISSUE}: it is not a repeat of that rebind" in err
    assert (sd / "state.json").read_bytes() == before
    assert (sd / epic_rebind.JOURNAL_NAME).read_bytes() == journal
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
    assert len(refusals) == 3 and all(
        r["error"].startswith("rebind-epic refused") for r in refusals
    )
    assert (sd / epic_rebind.JOURNAL_NAME).read_bytes() == journal


def test_a_run_bound_to_to_since_it_was_created_is_refused_not_already_bound(
    tmp_path, capsys, fakes
):
    """R1-F3: a run that records --to from the start has no rebind to repeat."""
    fakes["gh"].parents[ISSUE] = NEW_EPIC  # run binds to the native parent (#293)
    flags = _flags(tmp_path)
    assert cli.main([*flags, "run", "--issue", ISSUE]) == 0
    capsys.readouterr()
    sd = tmp_path / ".autoforge"
    assert load_state(sd / "state.json").epic_url == NEW_EPIC
    before = (sd / "state.json").read_bytes()
    fakes["gh"].calls.clear()
    assert cli.main([*flags, *_rebind(), "--dry-run"]) == 2
    out = capsys.readouterr().out
    assert "already bound" not in out
    assert "no rebind of run" in out and "to it is in its journal" in out
    assert cli.main([*flags, *_rebind()]) == 2
    err = capsys.readouterr().err
    assert "repeats no rebind that happened" in err and "Nothing was changed" in err
    assert (sd / "state.json").read_bytes() == before
    assert not (sd / epic_rebind.JOURNAL_NAME).exists()
    [refusal] = _rebind_records(sd)
    assert refusal["metadata"]["decision"] == "refused"
    assert not any(c[0].startswith(("create_", "edit_")) for c in fakes["gh"].calls)


def test_a_journal_that_does_not_parse_refuses_every_rebind(tmp_path, capsys, fakes):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    (sd / epic_rebind.JOURNAL_NAME).write_text("{", encoding="utf-8")
    before = (sd / "state.json").read_bytes()
    assert cli.main([*flags, *_rebind(), "--dry-run"]) == 2
    assert "cannot read its journal of applied rebinds" in capsys.readouterr().out
    assert cli.main([*flags, *_rebind()]) == 2
    assert "Moving it aside is safe" in capsys.readouterr().err
    assert (sd / "state.json").read_bytes() == before
    assert (sd / epic_rebind.JOURNAL_NAME).read_text(encoding="utf-8") == "{"


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


@pytest.mark.parametrize(
    "boundary", ["before the log", "at the journal", "after the journal", "at the replace"]
)
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
        elif boundary == "after the journal":

            def crash_save(self):
                raise Crash()

            patch.setattr(ControllerEngine, "save", crash_save)
        else:
            import autoforge.safefs as safefs

            real_replace = os.replace
            name = "state.json" if boundary == "at the replace" else epic_rebind.JOURNAL_NAME

            def crash_replace(src, dst, *a, **k):
                # One atomic rename alone; the run log's writes go through.
                if dst == name:
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
    journaled = boundary in ("after the journal", "at the replace")
    journal = sd / epic_rebind.JOURNAL_NAME
    assert journal.exists() is journaled
    assert cli.main([*flags, *_rebind()]) == 0
    capsys.readouterr()
    assert load_state(sd / "state.json").epic_url == NEW_EPIC
    decisions = [r["metadata"]["decision"] for r in _rebind_records(sd)]
    assert decisions == ["rebound"] * (logged + 1)
    # The entry of an interrupted attempt stays; the one in effect is the latest.
    entries = epic_rebind.parse_journal(journal.read_bytes())
    assert len(entries) == (2 if journaled else 1)
    assert {(e.old.canonical, e.new.canonical) for e in entries} == {(EPIC, NEW_EPIC)}
    # A repeat now is the very transition in effect.
    snapshot = state_tree(sd)
    assert cli.main([*flags, *_rebind()]) == 0
    assert "already bound" in capsys.readouterr().out
    assert state_tree(sd) == snapshot


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


def test_the_preview_reports_a_held_controller_lock_and_writes_no_lock_file(
    tmp_path, capsys, fakes
):
    """R1-F4: the dry run probes L0 without taking it or creating anything, and says the
    applied command decides again under the lock."""
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    lock_dir = tmp_path / ".git" / "autoforge"
    with ControllerLock(repository_lock_path(tmp_path)):
        before, lock_before = state_tree(sd), state_tree(lock_dir)
        assert cli.main([*flags, *_rebind(), "--dry-run"]) == 2
        out = capsys.readouterr().out
        assert "would refuse" in out and "would rebind" not in out
        assert "holds the controller lock" in out and "checks everything again under" in out
        assert state_tree(sd) == before and state_tree(lock_dir) == lock_before
    # Free again, with no lock file at all: the same preview would rebind, and the probe
    # created no lock file and wrote no PID.
    (lock_dir / "controller.lock").unlink()
    lock_before = state_tree(lock_dir)
    assert cli.main([*flags, *_rebind(), "--dry-run"]) == 0
    assert "would rebind" in capsys.readouterr().out
    assert state_tree(lock_dir) == lock_before and state_tree(sd) == before
    assert not (lock_dir / "controller.lock").exists()


def test_an_already_bound_scheduler_owned_run_is_refused_not_already_bound(tmp_path, capsys, fakes):
    """R1-F4: scheduler refusals come before the already-bound shortcut, in the preview
    as in the applied command, for an owned issue and for a namespace's own state."""
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    assert cli.main([*flags, *_rebind()]) == 0  # the standalone run is bound to NEW_EPIC
    capsys.readouterr()
    common = Path(os.path.realpath(tmp_path / ".git"))
    namespace = issue_namespace(common, 2)
    namespace.state_dir.mkdir(parents=True)
    save_state(load_state(sd / "state.json"), namespace.state_file)
    (namespace.state_dir / epic_rebind.JOURNAL_NAME).write_bytes(
        (sd / epic_rebind.JOURNAL_NAME).read_bytes()
    )
    before, ns_before = state_tree(sd), state_tree(namespace.directory)
    ns_flags = ["--config", flags[1], "--state-dir", str(namespace.state_dir)]
    for where, words in ((flags, "is owned by the parallel EPIC scheduler"), (ns_flags, "")):
        assert cli.main([*where, *_rebind(), "--dry-run"]) == 2
        out = capsys.readouterr().out
        assert "already bound" not in out and "would refuse" in out
        assert (words or "inside the parallel EPIC scheduler's root") in out
        assert cli.main([*where, *_rebind()]) == 2
        err = capsys.readouterr().err
        assert "already bound" not in err and "refuses" in err
    assert state_tree(sd) == before and state_tree(namespace.directory) == ns_before


def _second_clone(tmp_path_factory) -> Path:
    """Another local clone of the same GitHub repository (owner/repo): another git common dir."""
    return github_origin(git_repo(tmp_path_factory.mktemp("clone-b")))


def test_a_state_directory_of_another_clone_is_refused_while_its_controller_runs(
    tmp_path, capsys, fakes, monkeypatch, tmp_path_factory
):
    """R1-F1: from clone B, ``--state-dir`` naming clone A's run takes B's lock, which does
    not guard A's state, while A's own controller holds A's: refused before any lock or
    write, in the preview too, and also when A's lock is free."""
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    clone = _second_clone(tmp_path_factory)
    monkeypatch.chdir(clone)
    own = os.path.realpath(tmp_path / ".git")
    theirs = os.path.realpath(clone / ".git")
    for held in (True, False):
        with contextlib.ExitStack() as stack:
            if held:
                stack.enter_context(ControllerLock(repository_lock_path(tmp_path)))
            before = state_tree(sd)
            assert cli.main([*flags, *_rebind(), "--dry-run"]) == 2
            out = capsys.readouterr().out
            assert "would refuse" in out and f"git common dir is {own}" in out
            assert f"this command runs in the one whose git common dir is {theirs}" in out
            assert cli.main([*flags, *_rebind()]) == 2
            err = capsys.readouterr().err
            assert f"belongs to the repository whose git common dir is {own}" in err
            assert "no lock taken" in err
            assert state_tree(sd) == before
            assert not (clone / ".git" / "autoforge").exists()  # B's lock was never taken
    assert load_state(sd / "state.json").epic_url == EPIC
    assert _rebind_records(sd) == []


def test_a_scheduler_namespace_of_another_clone_is_refused(
    tmp_path, capsys, fakes, monkeypatch, tmp_path_factory
):
    """R1-F1: A's namespace named from clone B passes B's scheduler-root and ownership
    checks, so the repository binding is what refuses it."""
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    namespace = issue_namespace(Path(os.path.realpath(tmp_path / ".git")), 2)
    namespace.state_dir.mkdir(parents=True)
    save_state(load_state(sd / "state.json"), namespace.state_file)
    clone = _second_clone(tmp_path_factory)
    monkeypatch.chdir(clone)
    ns_flags = ["--config", flags[1], "--state-dir", str(namespace.state_dir)]
    before = state_tree(namespace.directory)
    assert cli.main([*ns_flags, *_rebind(), "--dry-run"]) == 2
    out = capsys.readouterr().out
    assert "would refuse" in out and "belongs to the repository whose git common dir" in out
    assert cli.main([*ns_flags, *_rebind()]) == 2
    assert "belongs to the repository whose git common dir" in capsys.readouterr().err
    assert state_tree(namespace.directory) == before
    assert not (clone / ".git" / "autoforge").exists()


def test_a_state_directory_in_no_repository_is_refused(tmp_path, capsys, fakes, tmp_path_factory):
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    outside = tmp_path_factory.mktemp("no-repository") / "state"
    outside.mkdir()
    save_state(load_state(tmp_path / ".autoforge" / "state.json"), outside / "state.json")
    out_flags = ["--config", flags[1], "--state-dir", str(outside)]
    before = state_tree(outside)
    assert cli.main([*out_flags, *_rebind(), "--dry-run"]) == 2
    assert "cannot be established" in capsys.readouterr().out
    assert cli.main([*out_flags, *_rebind()]) == 2
    assert "cannot be established" in capsys.readouterr().err
    assert state_tree(outside) == before


def test_the_engine_refuses_a_foreign_state_directory_under_its_own_lock(
    tmp_path, capsys, fakes, tmp_path_factory
):
    """The engine's own check, for a caller that took this checkout's lock anyway: nothing
    is written to the other repository's state directory, its run log included."""
    _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    clone = _second_clone(tmp_path_factory)
    eng = ControllerEngine(config=default_config(), state_dir=sd, workdir=clone)
    eng.load()
    before = state_tree(sd)
    with eng.locked(), pytest.raises(EpicRebindError, match="belongs to the repository"):
        eng.rebind_epic(_request())
    assert state_tree(sd) == before


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


@pytest.mark.parametrize("found", ["an unjournaled progress comment", "an unreadable listing"])
def test_a_run_blocked_by_update_epic_before_any_journal_is_refused(tmp_state_dir, found):
    """R1-F2: UPDATE_EPIC blocks on what it found on the recorded EPIC before it records an
    entry observation or an effect, so the BLOCKED run keeps nothing EPIC-scoped; its own
    counted merge is what refuses, so no progress comment is ever posted on --to beside the
    one on the recorded EPIC."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, [], github=gh)
    gh.add_pr(state="MERGED")
    eng.state.phase = Phase.UPDATE_EPIC
    eng.state.current_pr_url = PR
    eng.state.last_review_result = "clean"
    eng.state.record_merge(PR)
    eng.save()
    if found == "an unjournaled progress comment":
        post_progress_comment(gh)  # a legacy agent's, or anyone's
    else:
        gh.comments_error = GitHubError("HTTP 403: Resource not accessible")
    assert eng.step().next_phase == "BLOCKED"
    gh.comments_error = None
    state = load_state(eng.paths.state_file)
    assert state.effect_records == [] and state.entry_observation == {}
    assert state.completion_context == {} and eng.provider.calls == []
    _parented(gh)
    _assert_refused_unchanged(eng, f"the run's PR {PR} is merged and counted")
    assert not any(c[0].startswith(("create_", "edit_")) for c in gh.calls)


def test_a_progress_comment_of_the_issue_on_the_recorded_epic_is_refused(tmp_path, capsys, fakes):
    """R1-F2: a run whose PR is not merged yet, with a progress comment for its issue on the
    recorded EPIC (an agent of the previous contract posted it, or a human): refused."""
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    fakes["gh"].add_comment(EPIC, 310, progress_comment_body(ISSUE, fakes["pr"]))
    before = state_tree(sd)
    assert cli.main([*flags, *_rebind(), "--dry-run"]) == 2
    out = capsys.readouterr().out
    assert f"old progress:  {comment_url(EPIC, 310)}" in out
    assert "already carries 1 progress comment(s) for issue #2" in out
    assert state_tree(sd) == before
    assert cli.main([*flags, *_rebind()]) == 2
    assert "let UPDATE_EPIC post another on --to" in capsys.readouterr().err
    assert load_state(sd / "state.json").epic_url == EPIC
    assert not (sd / epic_rebind.JOURNAL_NAME).exists()


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize(
    "error", [GitHubUnavailableError("HTTP 502"), GitHubError("HTTP 403")], ids=["502", "403"]
)
def test_an_incomplete_comment_listing_of_the_recorded_epic_changes_nothing(
    tmp_path, capsys, fakes, dry, error
):
    """Incomplete GitHub evidence: the listing read to its end or the rebind is undecided."""
    flags = _at_ready_for_merge(tmp_path, capsys, fakes)
    sd = tmp_path / ".autoforge"
    before = (sd / "state.json").read_bytes()
    fakes["gh"].comments_error = error
    assert cli.main([*flags, *_rebind(), *(["--dry-run"] if dry else [])]) == 1
    captured = capsys.readouterr()
    assert "reading the comments of the recorded EPIC" in (captured.out if dry else captured.err)
    assert (sd / "state.json").read_bytes() == before
    assert not (sd / epic_rebind.JOURNAL_NAME).exists()


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
