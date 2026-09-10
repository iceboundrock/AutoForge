"""LOCAL mode: feature Markdown -> implement -> review -> fix -> DONE.

Every test here runs against a real temporary git repository (the local
trust boundary is `git` itself) with scripted agents and an
``ExplodingGitHub`` that fails the test if anything reaches for GitHub.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autoforge.cli import main
from autoforge.config import default_config
from autoforge.doctor import Doctor
from autoforge.errors import ConfigurationError, StateError, VerificationError
from autoforge.local_workspace import (
    LocalWorkspace,
    init_feature_file,
    read_feature_spec,
    resolve_feature_spec,
)
from autoforge.state import AutoForgeState, StatePaths, load_state, save_state
from autoforge.transitions import Phase, WorkflowMode

from .conftest import (
    FEATURE_MD,
    block,
    commit_all,
    git_repo,
    make_local_engine,
    write_feature,
)

IMPL_FILE = "src/app.py"


# -- helpers ------------------------------------------------------------------
def local_repo(tmp_path) -> Path:
    """A git repository with one committed file and a feature specification."""
    root = git_repo(tmp_path)
    (root / "src").mkdir(exist_ok=True)
    (root / IMPL_FILE).write_text("def main():\n    pass\n", encoding="utf-8")
    write_feature(root)
    commit_all(root, "initial")
    return root


def impl_result(changed: bool = True, summary: str = "Implemented the filter.") -> str:
    return block(
        {
            "phase": "ANALYZE_EXECUTE",
            "status": "success",
            "summary": summary,
            "changed_workspace": changed,
            "tests_attempted": ["pytest -q"],
        }
    )


def review_result(fingerprint: str, round: int = 1, findings: list[dict] | None = None) -> str:
    findings = findings or []
    return block(
        {
            "phase": "REVIEW",
            "status": "success",
            "round": round,
            "reviewed_workspace_fingerprint": fingerprint,
            "needs_fix_round": bool(findings),
            "findings": findings,
            "observations": ["Reads cleanly."],
        }
    )


def finding(round: int = 1, n: int = 1) -> dict:
    return {
        "id": f"R{round}-F{n}",
        "classification": "non-blocked",
        "title": "Missing test",
        "location": IMPL_FILE,
        "required_resolution": "Add a unit test for the date filter.",
    }


def fix_result(finding_ids: list[str], changed: bool = True, resolution: str = "fixed") -> str:
    return block(
        {
            "phase": "FIX",
            "status": "success",
            "changed_workspace": changed,
            "resolutions": [
                {
                    "finding_id": fid,
                    "resolution": resolution,
                    "rationale": "The behaviour is already covered elsewhere in the suite.",
                }
                for fid in finding_ids
            ],
            "blocked_reason": "",
        }
    )


def touch_impl(root: Path, text: str) -> None:
    (root / IMPL_FILE).write_text(text, encoding="utf-8")


def scripted(engine, root: Path, steps: list):
    """Handler that mutates the workspace like a real agent would.

    ``steps`` are ``(mutate(root) | None, stdout_factory(engine))`` pairs
    consumed in order.
    """
    queue = list(steps)

    def handler(req):
        mutate, stdout = queue.pop(0)
        if mutate is not None:
            mutate(root)
        return stdout(engine)

    return handler


# -- `local init` ---------------------------------------------------------------
def test_local_init_creates_the_template(tmp_path):
    root = git_repo(tmp_path)
    ws = LocalWorkspace(workdir=root, state_dir=".autoforge")
    path = init_feature_file(ws, "add-transaction-filter")
    assert path == root / "features" / "add-transaction-filter.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Feature: Add Transaction Filter")
    for section in (
        "## Problem",
        "## Requirements",
        "## Acceptance Criteria",
        "## Non-goals",
        "## Notes / Decisions",
    ):
        assert section in text
    assert "- [ ]" in text
    # Feature specs are project content, never runtime state.
    assert ".autoforge" not in str(path)


def test_local_init_refuses_to_overwrite(tmp_path):
    root = git_repo(tmp_path)
    ws = LocalWorkspace(workdir=root, state_dir=".autoforge")
    path = init_feature_file(ws, "keepme")
    path.write_text("# Feature: hand written\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="already exists"):
        init_feature_file(ws, "keepme")
    assert path.read_text(encoding="utf-8") == "# Feature: hand written\n"
    # ... unless overwriting is asked for explicitly.
    init_feature_file(ws, "keepme", overwrite=True)
    assert "## Acceptance Criteria" in path.read_text(encoding="utf-8")


def test_local_init_rejects_a_slug_with_a_path_separator(tmp_path):
    root = git_repo(tmp_path)
    ws = LocalWorkspace(workdir=root, state_dir=".autoforge")
    with pytest.raises(ConfigurationError, match="invalid feature slug"):
        init_feature_file(ws, "../../etc/passwd")


# -- feature specification resolution -------------------------------------------
def test_feature_spec_outside_the_repository_is_rejected(tmp_path):
    root = git_repo(tmp_path / "repo")
    outside = tmp_path / "outside.md"
    outside.write_text(FEATURE_MD, encoding="utf-8")
    ws = LocalWorkspace(workdir=root, state_dir=".autoforge")
    with pytest.raises(ConfigurationError, match="outside the repository"):
        resolve_feature_spec(ws, outside)
    with pytest.raises(ConfigurationError, match="outside the repository"):
        resolve_feature_spec(ws, "../outside.md")


def test_feature_spec_must_be_a_regular_markdown_file(tmp_path):
    root = local_repo(tmp_path)
    ws = LocalWorkspace(workdir=root, state_dir=".autoforge")

    (root / "features" / "dir.md").mkdir()
    with pytest.raises(ConfigurationError, match="not a regular file"):
        resolve_feature_spec(ws, "features/dir.md")

    (root / "features" / "link.md").symlink_to(root / "features" / "add-filter.md")
    with pytest.raises(ConfigurationError, match="symbolic link"):
        resolve_feature_spec(ws, "features/link.md")

    (root / "features" / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Markdown"):
        resolve_feature_spec(ws, "features/notes.txt")

    (root / "features" / "empty.md").write_text("   \n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="empty"):
        read_feature_spec(ws, "features/empty.md")


# -- run creation ----------------------------------------------------------------
def test_new_local_run_freezes_the_specification(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    state = eng.state
    assert state.mode == WorkflowMode.LOCAL
    assert state.phase == Phase.INITIALIZING
    assert state.feature_spec_path == "features/add-filter.md"
    import hashlib

    expected = hashlib.sha256((root / "features" / "add-filter.md").read_bytes()).hexdigest()
    assert state.feature_spec_sha256 == expected
    assert len(state.base_head_sha) == 40
    assert state.repository == "" and state.epic_url == ""


def test_dirty_working_tree_is_refused_unless_allowed(tmp_path):
    root = local_repo(tmp_path)
    touch_impl(root, "# unrelated local edit\n")
    with pytest.raises(ConfigurationError, match="--allow-dirty"):
        make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    eng = make_local_engine(
        root / ".autoforge", "features/add-filter.md", workdir=root, allow_dirty=True
    )
    assert eng.state.baseline_dirty_paths == [IMPL_FILE]


def test_an_uncommitted_feature_file_is_an_acceptable_baseline(tmp_path):
    root = local_repo(tmp_path)
    write_feature(root, "brand-new")
    eng = make_local_engine(root / ".autoforge", "features/brand-new.md", workdir=root)
    assert eng.state.baseline_dirty_paths == []


# -- fingerprint ------------------------------------------------------------------
def test_fingerprint_tracks_tracked_untracked_and_ignores_state_dir(tmp_path):
    root = local_repo(tmp_path)
    ws = LocalWorkspace(workdir=root, state_dir=".autoforge")
    base = ws.status().fingerprint

    touch_impl(root, "def main():\n    return 1\n")
    tracked = ws.status().fingerprint
    assert tracked != base

    (root / "src" / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    untracked = ws.status().fingerprint
    assert untracked != tracked

    (root / "src" / "new_module.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert ws.status().fingerprint != untracked

    (root / ".autoforge" / "logs").mkdir(parents=True, exist_ok=True)
    (root / ".autoforge" / "logs" / "run.log").write_text("noise\n", encoding="utf-8")
    (root / ".autoforge" / "state.json").write_text("{}", encoding="utf-8")
    assert ws.status().fingerprint == ws.status().fingerprint
    stable = ws.status().fingerprint
    (root / ".autoforge" / "logs" / "run.log").write_text("more noise\n", encoding="utf-8")
    assert ws.status().fingerprint == stable


def test_fingerprint_notices_a_deleted_tracked_file(tmp_path):
    root = local_repo(tmp_path)
    ws = LocalWorkspace(workdir=root, state_dir=".autoforge")
    base = ws.status().fingerprint
    (root / IMPL_FILE).unlink()
    assert ws.status().fingerprint != base


# -- the happy path ----------------------------------------------------------------
def test_clean_review_reaches_done_without_any_commit(tmp_path):
    root = local_repo(tmp_path)
    head_before = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (
                lambda r: touch_impl(r, "def main():\n    return 'filtered'\n"),
                lambda e: impl_result(),
            ),
            (None, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    outcomes = eng.run(max_steps=5)
    assert eng.state.phase == Phase.DONE
    assert [o.next_phase for o in outcomes] == ["ANALYZE_EXECUTE", "REVIEW", "DONE"]
    # No commit was required and HEAD never moved: the work is in the tree.
    head_after = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_after == head_before
    assert eng.state.reviewed_workspace_fingerprint == eng.state.workspace_fingerprint


def test_findings_route_through_fix_and_back_to_review(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint, 1, [finding(1)])),
            (lambda r: touch_impl(r, "v2\n"), lambda e: fix_result(["R1-F1"])),
            (None, lambda e: review_result(e.state.workspace_fingerprint, 2)),
        ],
    )
    outcomes = eng.run(max_steps=8)
    assert [o.next_phase for o in outcomes] == [
        "ANALYZE_EXECUTE",
        "REVIEW",
        "FIX",
        "REVIEW",
        "DONE",
    ]
    assert eng.state.local_fix_rounds == 1
    assert eng.state.open_findings == []


def test_findings_after_the_fix_budget_block(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint, 1, [finding(1)])),
            (lambda r: touch_impl(r, "v2\n"), lambda e: fix_result(["R1-F1"])),
            (None, lambda e: review_result(e.state.workspace_fingerprint, 2, [finding(2)])),
        ],
    )
    eng.run(max_steps=8)
    assert eng.state.phase == Phase.BLOCKED
    assert "max_fix_rounds" in eng.state.block_reason
    assert len(eng.state.open_findings) == 1


def test_review_is_bound_to_the_controller_fingerprint(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result("f" * 64)),
        ],
    )
    with pytest.raises(VerificationError, match="fingerprint mismatch"):
        eng.run(max_steps=5)
    # The phase is not advanced and the refusal is recorded for the operator.
    assert eng.state.phase == Phase.REVIEW
    assert any("fingerprint" in f for f in eng.state.verification_failures)


def test_a_reviewer_that_edits_the_workspace_is_refused(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            # The reviewer echoes the bound fingerprint but rewrites the code.
            (
                lambda r: touch_impl(r, "reviewer sneaked this in\n"),
                lambda e: review_result(e.state.workspace_fingerprint),
            ),
        ],
    )
    with pytest.raises(VerificationError, match="must not change what it reviews"):
        eng.run(max_steps=5)
    assert eng.state.phase == Phase.REVIEW


def test_implementation_that_changes_nothing_is_refused(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    eng.provider._handler = scripted(eng, root, [(None, lambda e: impl_result(changed=True))])
    with pytest.raises(VerificationError):
        eng.run(max_steps=3)
    assert eng.state.phase == Phase.ANALYZE_EXECUTE


# -- the frozen specification --------------------------------------------------------
def test_specification_edited_during_implementation_is_detected(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)

    def rewrite(r):
        (r / "features" / "add-filter.md").write_text(
            "# Feature: Trivial\n\n## Acceptance Criteria\n\n- [x] nothing\n", encoding="utf-8"
        )

    eng.provider._handler = scripted(eng, root, [(rewrite, lambda e: impl_result())])
    with pytest.raises(VerificationError, match="changed after ANALYZE_EXECUTE"):
        eng.run(max_steps=3)
    assert eng.state.phase == Phase.ANALYZE_EXECUTE


def test_specification_edited_before_review_is_detected(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    eng.step()  # INITIALIZING -> ANALYZE_EXECUTE
    eng.step()  # implementation
    (root / "features" / "add-filter.md").write_text("# Feature: rewritten\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="changed before REVIEW"):
        eng.step()


# -- validation commands ---------------------------------------------------------------
def test_failing_validation_command_prevents_advancement(tmp_path):
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [["false"]]
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root, cfg=cfg)
    eng.provider._handler = scripted(
        eng, root, [(lambda r: touch_impl(r, "v1\n"), lambda e: impl_result())]
    )
    with pytest.raises(VerificationError, match="validation command"):
        eng.run(max_steps=3)
    assert eng.state.phase == Phase.ANALYZE_EXECUTE


def test_passing_validation_command_allows_advancement(tmp_path):
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [["true"]]
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root, cfg=cfg)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    eng.run(max_steps=5)
    assert eng.state.phase == Phase.DONE


# -- dry run -------------------------------------------------------------------------
def test_local_dry_run_has_no_side_effects(tmp_path):
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [["touch", str(root / "SHOULD_NOT_EXIST")]]
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root, cfg=cfg)
    outcomes = eng.run(max_steps=3, dry_run=True)
    assert eng.provider.calls == []
    assert not (root / "SHOULD_NOT_EXIST").exists()
    assert not (root / ".autoforge" / "state.json").exists()
    plan = outcomes[0].plan
    assert plan.phase == "INITIALIZING"
    notes = " ".join(plan.notes)
    assert "LOCAL" in notes
    assert "features/add-filter.md" in notes
    assert eng.state.feature_spec_sha256[:16] in notes


# -- resume ------------------------------------------------------------------------------
def test_local_run_is_resumable_from_persisted_state(tmp_path):
    root = local_repo(tmp_path)
    paths = StatePaths.from_state_dir(root / ".autoforge")
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    eng.provider._handler = scripted(
        eng, root, [(lambda r: touch_impl(r, "v1\n"), lambda e: impl_result())]
    )
    eng.step()
    eng.step()
    assert eng.state.phase == Phase.REVIEW

    # A fresh process: nothing but state.json and the working tree survive.
    eng2 = make_local_engine(
        root / ".autoforge", "features/add-filter.md", workdir=root, start=False
    )
    state = eng2.load()
    assert state.mode == WorkflowMode.LOCAL
    assert state.phase == Phase.REVIEW
    eng2.provider._handler = scripted(
        eng2, root, [(None, lambda e: review_result(e.state.workspace_fingerprint))]
    )
    eng2.run(max_steps=3)
    assert eng2.state.phase == Phase.DONE
    assert load_state(paths.state_file).phase == Phase.DONE


# -- backward compatibility -----------------------------------------------------------------
def test_saved_state_without_a_mode_field_loads_as_remote(tmp_path):
    """Pre-local state files have no 'mode': they are REMOTE runs."""
    paths = StatePaths.from_state_dir(tmp_path / ".autoforge")
    state = AutoForgeState(
        run_id="r1",
        repository="owner/repo",
        epic_url="https://github.com/owner/repo/issues/1",
        current_issue_url="https://github.com/owner/repo/issues/2",
        phase=Phase.REVIEW,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    save_state(state, paths.state_file)
    raw = json.loads(paths.state_file.read_text(encoding="utf-8"))
    raw.pop("mode")
    paths.state_file.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_state(paths.state_file)
    assert loaded.mode == WorkflowMode.REMOTE
    assert loaded.phase == Phase.REVIEW


def test_unknown_mode_fails_loudly(tmp_path):
    paths = StatePaths.from_state_dir(tmp_path / ".autoforge")
    state = AutoForgeState(
        run_id="r1",
        repository="owner/repo",
        epic_url="e",
        phase=Phase.REVIEW,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    save_state(state, paths.state_file)
    raw = json.loads(paths.state_file.read_text(encoding="utf-8"))
    raw["mode"] = "HYBRID"
    paths.state_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(StateError):
        load_state(paths.state_file)


# -- doctor ---------------------------------------------------------------------------------
def test_local_doctor_never_runs_gh(tmp_path):
    root = local_repo(tmp_path)
    seen: list[list[str]] = []

    def runner(req):
        seen.append(list(req.command))
        from autoforge.executor import ExecutionResult

        stdout = str(root) if req.command[:2] == ["git", "rev-parse"] else "ok"
        return ExecutionResult(
            command=list(req.command),
            cwd=req.cwd,
            exit_code=0,
            stdout=stdout,
            stderr="",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
        )

    doc = Doctor(cwd=str(root), state_dir=str(root / ".autoforge"), runner=runner)
    results = doc.run_local(feature_spec_path="features/add-filter.md")
    assert all(cmd[0] != "gh" for cmd in seen), seen
    assert not any("remote" in cmd for cmd in seen)
    names = {r.name for r in results}
    assert "gh authenticated" not in names and "GitHub remote" not in names
    assert {"implementation agent available", "review agent available"} <= names
    assert [r for r in results if r.name == "feature specification"][0].ok


# -- CLI ------------------------------------------------------------------------------------
def test_cli_local_init_and_status(tmp_path, monkeypatch, capsys):
    root = git_repo(tmp_path)
    monkeypatch.chdir(root)
    assert main(["local", "init", "add-transaction-filter"]) == 0
    assert (root / "features" / "add-transaction-filter.md").is_file()
    capsys.readouterr()
    # A second init must not clobber the operator's edits.
    (root / "features" / "add-transaction-filter.md").write_text("# Feature: mine\n", "utf-8")
    assert main(["local", "init", "add-transaction-filter"]) == 2
    assert (root / "features" / "add-transaction-filter.md").read_text("utf-8") == (
        "# Feature: mine\n"
    )


def test_cli_local_status_hides_github_fields(tmp_path, monkeypatch, capsys):
    root = local_repo(tmp_path)
    monkeypatch.chdir(root)
    eng = make_local_engine(root / ".autoforge", "features/add-filter.md", workdir=root)
    save_state(eng.state, eng.paths.state_file)
    capsys.readouterr()
    assert main(["--state-dir", str(root / ".autoforge"), "status"]) == 0
    out = capsys.readouterr().out
    assert "local mode" in out
    assert "features/add-filter.md" in out
    assert "PR:" not in out and "EPIC:" not in out
    assert "Workspace fingerprint:" in out

    assert main(["--state-dir", str(root / ".autoforge"), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "LOCAL"
    assert payload["feature_spec_path"] == "features/add-filter.md"


# -- GitHub isolation ---------------------------------------------------------------------------
def test_a_local_run_makes_zero_gh_invocations(tmp_path):
    """No `gh` argv, and no GitHubClient is ever constructed."""
    from autoforge.engine import ControllerEngine
    from autoforge.executor import execute
    from autoforge.providers import ProviderRegistry, ScriptedProvider

    root = local_repo(tmp_path)
    executed: list[list[str]] = []

    def recording_runner(req):
        executed.append(list(req.command))
        return execute(req)

    cfg = default_config()
    cfg.local.validation_commands = [["true"]]
    provider = ScriptedProvider()
    eng = ControllerEngine(
        config=cfg,
        state_dir=root / ".autoforge",
        workdir=root,
        runner=recording_runner,
        github=None,  # nothing is injected: constructing one would be the bug
        providers=ProviderRegistry(overrides={"claude": provider, "opencode": provider}),
    )
    eng.new_local_run("features/add-filter.md")
    eng.provider = provider
    provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    eng.run(max_steps=5)
    assert eng.state.phase == Phase.DONE
    assert executed, "the local workspace must actually shell out to git"
    assert all(argv[0] != "gh" for argv in executed), executed
    assert eng._github is None


def test_local_prompts_never_mention_github_operations():
    from autoforge.prompts import load_template

    forbidden = ("gh pr", "gh issue", "git push", "git commit", "pull request", "follow-up issue")
    negations = ("never", "not", "no ", "do not", "forbidden", "without", "there is no")
    for name in ("local_common.md", "local_analyze_execute.md", "local_review.md", "local_fix.md"):
        text = load_template(name).lower()
        # Paragraph granularity: a bullet may read "- `git push`" while the
        # prohibition ("You must never:") sits in the paragraph's lead-in.
        for para in text.split("\n\n"):
            for phrase in forbidden:
                if phrase in para:
                    assert any(word in para for word in negations), f"{name}: {para!r}"


# -- transitions --------------------------------------------------------------------------------
def test_local_topology_excludes_the_github_phases():
    from autoforge.errors import StateTransitionError
    from autoforge.transitions import is_legal, validate_transition

    assert is_legal(Phase.REVIEW, Phase.DONE, WorkflowMode.LOCAL)
    assert not is_legal(Phase.REVIEW, Phase.READY_FOR_MERGE, WorkflowMode.LOCAL)
    assert not is_legal(Phase.REVIEW, Phase.REPLAN_REEXECUTE, WorkflowMode.LOCAL)
    # ... and the remote topology is unchanged.
    assert is_legal(Phase.REVIEW, Phase.READY_FOR_MERGE, WorkflowMode.REMOTE)
    assert not is_legal(Phase.REVIEW, Phase.DONE, WorkflowMode.REMOTE)
    with pytest.raises(StateTransitionError, match="LOCAL mode"):
        validate_transition(Phase.REVIEW, Phase.MERGE, WorkflowMode.LOCAL)


# -- result protocol ----------------------------------------------------------------------------
def test_local_fix_cannot_defer_a_finding_to_a_github_issue():
    from autoforge.errors import ControlResultValidationError
    from autoforge.result_parser import parse_control_result

    payload = {
        "phase": "FIX",
        "status": "success",
        "changed_workspace": False,
        "resolutions": [{"finding_id": "R1-F1", "resolution": "follow_up_created"}],
    }
    with pytest.raises(ControlResultValidationError, match="unresolved"):
        parse_control_result(block(payload), Phase.FIX, WorkflowMode.LOCAL)


def test_local_mode_rejects_the_github_phases_in_the_protocol():
    from autoforge.errors import ControlResultValidationError
    from autoforge.result_parser import parse_control_result

    payload = {"phase": "READY_FOR_MERGE", "status": "success"}
    with pytest.raises(ControlResultValidationError, match="REMOTE"):
        parse_control_result(block(payload), Phase.READY_FOR_MERGE, WorkflowMode.LOCAL)


# -- CLI dry run ---------------------------------------------------------------------------------
def test_cli_local_run_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    root = local_repo(tmp_path)
    monkeypatch.chdir(root)
    assert main(["local", "run", "features/add-filter.md", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "INITIALIZING" in out
    assert "mode: LOCAL" in out
    assert not (root / ".autoforge" / "state.json").exists()


def test_cli_local_run_refuses_a_dirty_tree(tmp_path, monkeypatch, capsys):
    root = local_repo(tmp_path)
    monkeypatch.chdir(root)
    touch_impl(root, "unrelated\n")
    assert main(["local", "run", "features/add-filter.md", "--dry-run"]) == 2
    assert "--allow-dirty" in capsys.readouterr().err
