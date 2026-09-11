"""LOCAL mode: feature Markdown -> implement -> review -> fix -> DONE.

Every test here runs against a real temporary git repository (the local
trust boundary is `git` itself) with scripted agents and an
``ExplodingGitHub`` that fails the test if anything reaches for GitHub.
"""

from __future__ import annotations

import json
import subprocess
import sys
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
from autoforge.safefs import UnsafePathError
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
    ws = LocalWorkspace(workdir=root)
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
    ws = LocalWorkspace(workdir=root)
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
    ws = LocalWorkspace(workdir=root)
    with pytest.raises(ConfigurationError, match="invalid feature slug"):
        init_feature_file(ws, "../../etc/passwd")


# -- feature specification resolution -------------------------------------------
def test_feature_spec_outside_the_repository_is_rejected(tmp_path):
    root = git_repo(tmp_path / "repo")
    outside = tmp_path / "outside.md"
    outside.write_text(FEATURE_MD, encoding="utf-8")
    ws = LocalWorkspace(workdir=root)
    with pytest.raises(ConfigurationError, match="outside the repository"):
        resolve_feature_spec(ws, outside)
    with pytest.raises(ConfigurationError, match="outside the repository"):
        resolve_feature_spec(ws, "../outside.md")


def test_feature_spec_must_be_a_regular_markdown_file(tmp_path):
    root = local_repo(tmp_path)
    ws = LocalWorkspace(workdir=root)

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
    eng = make_local_engine(root, "features/add-filter.md")
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
        make_local_engine(root, "features/add-filter.md")
    eng = make_local_engine(root, "features/add-filter.md", allow_dirty=True)
    assert eng.state.baseline_dirty_paths == [IMPL_FILE]


def test_an_uncommitted_feature_file_is_an_acceptable_baseline(tmp_path):
    root = local_repo(tmp_path)
    write_feature(root, "brand-new")
    eng = make_local_engine(root, "features/brand-new.md")
    assert eng.state.baseline_dirty_paths == []


# -- fingerprint ------------------------------------------------------------------
def test_fingerprint_tracks_tracked_untracked_and_clean_alike(tmp_path):
    root = local_repo(tmp_path)
    ws = LocalWorkspace(workdir=root)
    base = ws.snapshot().fingerprint

    touch_impl(root, "def main():\n    return 1\n")
    tracked = ws.snapshot().fingerprint
    assert tracked != base

    (root / "src" / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    untracked = ws.snapshot().fingerprint
    assert untracked != tracked

    (root / "src" / "new_module.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert ws.snapshot().fingerprint != untracked

    # Writing under the git directory -- where LOCAL state now lives -- moves
    # nothing: it is excluded by inode identity, not by name.
    state_dir = ws.local_state_dir()
    (state_dir / "logs").mkdir(parents=True, exist_ok=True)
    (state_dir / "logs" / "run.log").write_text("noise\n", encoding="utf-8")
    (state_dir / "state.json").write_text("{}", encoding="utf-8")
    stable = ws.snapshot().fingerprint
    (state_dir / "logs" / "run.log").write_text("more noise\n", encoding="utf-8")
    assert ws.snapshot().fingerprint == stable


def test_fingerprint_notices_a_mode_change_on_an_already_dirty_file(tmp_path):
    """R3-F3: content + status code do not describe a working tree; the mode does too.

    On a file that is *already* modified, `chmod +x` moves neither the
    content digest nor the porcelain code (it stays " M"), so the fingerprint
    was identical before and after. A reviewer could therefore make a script
    executable after the review it was bound to, and the post-review equality
    check still passed — while what a validation command does with that file
    changed.
    """
    root = local_repo(tmp_path)
    ws = LocalWorkspace(workdir=root)
    script = root / IMPL_FILE
    script.write_text("print('hi')\n", encoding="utf-8")  # dirty, mode unchanged
    dirty = ws.snapshot().fingerprint

    script.chmod(0o755)
    assert ws.snapshot().fingerprint != dirty
    assert [e.mode for e in ws.snapshot().entries if e.path == IMPL_FILE] == ["0755"]

    # ... and back again: the fingerprint is a function of the tree, not a ratchet.
    script.chmod(0o644)
    assert ws.snapshot().fingerprint == dirty

    # The same holds for a file that is only *newly* executable and untracked.
    extra = root / "src" / "tool.sh"
    extra.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    before = ws.snapshot().fingerprint
    extra.chmod(0o755)
    assert ws.snapshot().fingerprint != before


def test_fingerprint_notices_a_deleted_tracked_file(tmp_path):
    root = local_repo(tmp_path)
    ws = LocalWorkspace(workdir=root)
    base = ws.snapshot().fingerprint
    (root / IMPL_FILE).unlink()
    assert ws.snapshot().fingerprint != base


# -- the happy path ----------------------------------------------------------------
def test_clean_review_reaches_done_without_any_commit(tmp_path):
    root = local_repo(tmp_path)
    head_before = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    eng = make_local_engine(root, "features/add-filter.md")
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
    eng = make_local_engine(root, "features/add-filter.md")
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
    eng = make_local_engine(root, "features/add-filter.md")
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
    eng = make_local_engine(root, "features/add-filter.md")
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
    eng = make_local_engine(root, "features/add-filter.md")
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
    eng = make_local_engine(root, "features/add-filter.md")
    eng.provider._handler = scripted(eng, root, [(None, lambda e: impl_result(changed=True))])
    with pytest.raises(VerificationError):
        eng.run(max_steps=3)
    assert eng.state.phase == Phase.ANALYZE_EXECUTE


# -- the frozen specification --------------------------------------------------------
def test_specification_edited_during_implementation_is_detected(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")

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
    eng = make_local_engine(root, "features/add-filter.md")
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
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
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
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
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
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    outcomes = eng.run(max_steps=3, dry_run=True)
    assert eng.provider.calls == []
    assert not (root / "SHOULD_NOT_EXIST").exists()
    assert not eng.paths.state_file.exists()
    plan = outcomes[0].plan
    assert plan.phase == "INITIALIZING"
    notes = " ".join(plan.notes)
    assert "LOCAL" in notes
    assert "features/add-filter.md" in notes
    assert eng.state.feature_spec_sha256[:16] in notes


# -- resume ------------------------------------------------------------------------------
def test_local_run_is_resumable_from_persisted_state(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    eng.provider._handler = scripted(
        eng, root, [(lambda r: touch_impl(r, "v1\n"), lambda e: impl_result())]
    )
    eng.step()
    eng.step()
    assert eng.state.phase == Phase.REVIEW

    # A fresh process: nothing but state.json and the working tree survive.
    eng2 = make_local_engine(root, "features/add-filter.md", start=False)
    state = eng2.load()
    assert state.mode == WorkflowMode.LOCAL
    assert state.phase == Phase.REVIEW
    eng2.provider._handler = scripted(
        eng2, root, [(None, lambda e: review_result(e.state.workspace_fingerprint))]
    )
    eng2.run(max_steps=3)
    assert eng2.state.phase == Phase.DONE
    assert load_state(eng2.paths.state_file).phase == Phase.DONE


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

        stdout = str(root) if "rev-parse" in req.command else "ok"
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
    # One check per *reachable* local profile's CLI, derived from the config
    # rather than a fixed claude+opencode pair (PR #44, R1-F10).
    agent_checks = sorted(n for n in names if n.startswith("agent "))
    assert agent_checks == [
        "agent 'claude' available (analyze_execute, fix)",
        "agent 'opencode' available (review_round_1, review_round_2_5)",
    ], agent_checks
    assert [r for r in results if r.name == "feature specification"][0].ok


def test_local_doctor_checks_only_the_reachable_providers(tmp_path):
    """A local config that never reaches a Claude profile must not need `claude`.

    `_agent_commands` used to scan *every* configured profile and then always
    check one Claude and one OpenCode binary, so a valid OpenCode-only local
    setup failed doctor because an unrelated remote profile mentioned a CLI it
    would never run (PR #44, R1-F10).
    """
    root = local_repo(tmp_path)
    cfg_path = root / "autoforge.toml"
    seen: list[list[str]] = []

    def runner(req):
        seen.append(list(req.command))
        from autoforge.executor import ExecutionResult

        stdout = str(root) if "rev-parse" in req.command else "ok"
        return ExecutionResult(
            command=list(req.command),
            cwd=req.cwd,
            exit_code=0,
            stdout=stdout,
            stderr="",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
        )

    # Zero fix rounds: only analyze_execute and review_round_1 are reachable,
    # and both are `scripted`, so no external agent CLI is needed at all.
    cfg_path.write_text(
        "version = 1\n"
        "[local]\nmax_fix_rounds = 0\n"
        '[profiles.analyze_execute]\nprovider = "scripted"\ncommand = "/bin/true"\n'
        '[profiles.review_round_1]\nprovider = "scripted"\ncommand = "/bin/true"\n',
        encoding="utf-8",
    )
    doc = Doctor(
        config_path=str(cfg_path),
        cwd=str(root),
        state_dir=str(root / ".autoforge"),
        runner=runner,
    )
    results = doc.run_local()
    assert all(r.ok for r in results), [(r.name, r.detail) for r in results if not r.ok]
    assert not any(cmd[0] in ("claude", "opencode") for cmd in seen), seen

    # One reviewer profile keeps its real CLI: exactly that binary is checked,
    # and the Claude-backed `fix` profile is still unreachable, so `claude` is
    # never probed.
    cfg_path.write_text(
        "version = 1\n"
        "[local]\nmax_fix_rounds = 0\n"
        '[profiles.analyze_execute]\nprovider = "scripted"\ncommand = "/bin/true"\n'
        '[profiles.review_round_1]\ncommand = "oc"\n',
        encoding="utf-8",
    )
    seen.clear()
    results = Doctor(
        config_path=str(cfg_path),
        cwd=str(root),
        state_dir=str(root / ".autoforge"),
        runner=runner,
    ).run_local()
    assert all(r.ok for r in results), [(r.name, r.detail) for r in results if not r.ok]
    assert ["oc", "--version"] in seen
    assert not any(cmd[0] == "claude" for cmd in seen), seen
    assert "agent 'oc' available (review_round_1)" in {r.name for r in results}


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
    eng = make_local_engine(root, "features/add-filter.md")
    save_state(eng.state, eng.paths.state_file)
    capsys.readouterr()
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "local mode" in out
    assert "features/add-filter.md" in out
    assert "PR:" not in out and "EPIC:" not in out
    assert "Workspace fingerprint:" in out

    assert main(["status", "--json"]) == 0
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
        workdir=root,
        runner=recording_runner,
        github=None,  # nothing is injected: constructing one would be the bug
        providers=ProviderRegistry(overrides={"claude": provider, "opencode": provider}),
    )
    eng.bind_local_state_dir()
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
    assert not (root / ".git" / "autoforge" / "state" / "state.json").exists()


def test_cli_local_run_refuses_a_dirty_tree(tmp_path, monkeypatch, capsys):
    root = local_repo(tmp_path)
    monkeypatch.chdir(root)
    touch_impl(root, "unrelated\n")
    assert main(["local", "run", "features/add-filter.md", "--dry-run"]) == 2
    assert "--allow-dirty" in capsys.readouterr().err


# -- required profiles ---------------------------------------------------------------------------
def test_local_required_profiles_follow_the_configured_review_bound(tmp_path):
    """A reachable reviewer profile is required at config time, not at round 6.

    Local review rounds are routed exactly like remote ones, so a large
    `local.max_fix_rounds` reaches `review_round_6_plus`. Requiring a fixed
    list would turn a missing profile into a `ConfigurationError` raised five
    fix rounds into a run (PR #44, O1).
    """
    from autoforge.profiles import local_required_profiles

    cfg = default_config()
    cfg.local.max_fix_rounds = 1
    assert local_required_profiles(cfg) == [
        "analyze_execute",
        "fix",
        "review_round_1",
        "review_round_2_5",
    ]
    # 0 fix rounds: FIX is unreachable and so is every review pass after the
    # first, so neither may be required (PR #44, R1-F7).
    cfg.local.max_fix_rounds = 0
    assert local_required_profiles(cfg) == ["analyze_execute", "review_round_1"]
    # 5 fix rounds == 6 review passes: the round 6+ reviewer becomes reachable.
    cfg.local.max_fix_rounds = 5
    assert local_required_profiles(cfg) == [
        "analyze_execute",
        "fix",
        "review_round_1",
        "review_round_2_5",
        "review_round_6_plus",
    ]
    # Never the phases a local run cannot enter.
    assert "replan_reexecute" not in local_required_profiles(cfg)
    assert "update_epic" not in local_required_profiles(cfg)


def test_a_reachable_reviewer_profile_is_validated_before_the_run_starts(tmp_path):
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.max_fix_rounds = 5
    del cfg.profiles["review_round_6_plus"]

    # `local run` validates right after creating the run, before any agent.
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    assert eng.mode == WorkflowMode.LOCAL
    with pytest.raises(ConfigurationError, match="review_round_6_plus"):
        eng.validate_config()
    # ... and the same config is fine while that round stays unreachable.
    cfg.local.max_fix_rounds = 1
    eng.validate_config()


def test_local_doctor_requires_only_the_reachable_reviewer_profiles(tmp_path):
    """`local doctor` applies the same derived requirement (PR #44, O1)."""
    from autoforge.profiles import local_required_profiles

    root = local_repo(tmp_path)
    cfg_path = root / "cfg.json"

    def doctor_for(max_fix_rounds: int) -> Doctor:
        # A profile cannot be deleted by a config file, but an unusable one
        # (empty model) fails `validate_profile` the same way a missing one does.
        cfg_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "local": {"max_fix_rounds": max_fix_rounds},
                    "profiles": {"review_round_6_plus": {"model": ""}},
                }
            ),
            encoding="utf-8",
        )
        return Doctor(cwd=str(root), config_path=str(cfg_path), state_dir=str(root / ".autoforge"))

    broken = doctor_for(5).check_config(local_required_profiles)
    assert not broken.ok and "review_round_6_plus" in broken.detail
    # Unreachable at the default bound: not required, so not a failure.
    assert doctor_for(1).check_config(local_required_profiles).ok


# -- secret redaction ----------------------------------------------------------------------------
def test_failing_validation_output_is_redacted_before_it_is_persisted(tmp_path):
    """A project's own test output can print a token; `state.json` is cleartext.

    PR #44, O2: the output tail of a failed validation command reaches
    `state.verification_failures`, so it passes through `redact` first.
    """
    import sys

    root = local_repo(tmp_path)
    cfg = default_config()
    secret = "ghp_" + "A" * 36
    cfg.local.validation_commands = [
        [sys.executable, "-c", f"import sys; print('GITHUB_TOKEN={secret}'); sys.exit(1)"]
    ]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng, root, [(lambda r: touch_impl(r, "v1\n"), lambda e: impl_result())]
    )
    with pytest.raises(VerificationError) as excinfo:
        eng.run(max_steps=3)
    assert secret not in str(excinfo.value)
    assert "***REDACTED***" in str(excinfo.value)

    # ANALYZE_EXECUTE failures are not persisted, but the same tail reaching
    # state via a FIX failure must be redacted too: assert on the recorder.
    eng.state.verification_failures = []
    eng._record_verification_failure(Phase.FIX, VerificationError(f"tail: GITHUB_TOKEN={secret}"))
    eng._save()
    raw = eng.paths.state_file.read_text(encoding="utf-8")
    assert secret not in raw
    assert "***REDACTED***" in raw


# -- CLI guard wording ---------------------------------------------------------------------------
def test_the_existing_run_guard_names_the_subcommand_that_was_typed(tmp_path, monkeypatch, capsys):
    """The advice must be copy-pasteable for the command in hand (PR #44, O4)."""
    from autoforge.cli import _existing_run_guard

    root = local_repo(tmp_path)
    monkeypatch.chdir(root)
    paths = StatePaths.from_state_dir(root / ".autoforge")
    eng = make_local_engine(root, "features/add-filter.md")
    save_state(eng.state, paths.state_file)

    assert _existing_run_guard(paths, force=False, command="local run") == (2, False)
    assert "'local run --force' to discard it" in capsys.readouterr().err
    # The remote wording is the pre-existing one, unchanged.
    assert _existing_run_guard(paths, force=False) == (2, False)
    assert "'run --force' to discard it" in capsys.readouterr().err

    paths.state_file.write_text('{"phase": "REVIEW", "run_id": ', encoding="utf-8")
    assert _existing_run_guard(paths, force=False, command="local run") == (2, False)
    assert "'local run --force' to move it aside" in capsys.readouterr().err


# -- PR #44 review regressions -------------------------------------------------
def test_an_unresolved_finding_blocks_instead_of_reaching_a_clean_review(tmp_path):
    """R1-F1: `unresolved` is an agent-reported blocker, not a passed baton.

    FIX used to clear `open_findings` for every reported resolution whatever
    its disposition, so a finding the fix agent explicitly could *not* resolve
    went back to a fresh reviewer with no memory of it. A clean round 2 then
    carried the run to DONE with an acknowledged, unaddressed finding in the
    tree.
    """
    root = local_repo(tmp_path)
    cfg = default_config()
    # A validation command that appends one character per run, so the test can
    # tell "ran for ANALYZE_EXECUTE" from "ran again for FIX": an unresolved
    # finding is terminal *before* the FIX round's validation is executed.
    tally = root / "VALIDATION_RUNS"
    cfg.local.validation_commands = [
        [sys.executable, "-c", f"open({str(tally)!r}, 'a').write('x')"]
    ]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)

    def two_findings(e):
        return review_result(e.state.workspace_fingerprint, 1, [finding(1, 1), finding(1, 2)])

    def one_unresolved(e):
        return block(
            {
                "phase": "FIX",
                "status": "success",
                "changed_workspace": True,
                "resolutions": [
                    {
                        "finding_id": "R1-F1",
                        "resolution": "fixed",
                        "rationale": "Added the missing unit test for the date filter.",
                    },
                    {
                        "finding_id": "R1-F2",
                        "resolution": "unresolved",
                        "rationale": (
                            "The filter needs a schema migration that is out of scope for "
                            "this feature specification; a human has to decide."
                        ),
                    },
                ],
                "blocked_reason": "",
            }
        )

    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, two_findings),
            (lambda r: touch_impl(r, "v2\n"), one_unresolved),
        ],
    )
    outcomes = eng.run(max_steps=8)
    assert eng.state.phase == Phase.BLOCKED
    assert [o.next_phase for o in outcomes][-1] == "BLOCKED"
    # Only the unresolved finding survives, and it survives by id.
    assert [f["id"] for f in eng.state.open_findings] == ["R1-F2"]
    assert eng.state.last_review_result == "unresolved"
    assert "explicitly unresolved" in eng.state.block_reason
    # The rationale reaches the operator rather than being swallowed.
    assert "schema migration" in eng.state.block_reason
    assert tally.read_text(encoding="utf-8") == "x", "FIX validation must not have run"
    # The fix round still counted: it consumed an agent invocation.
    assert eng.state.local_fix_rounds == 1


def test_a_no_change_with_rationale_resolution_still_advances(tmp_path):
    """The other non-`fixed` disposition is a *resolution* and must not block.

    Guards the R1-F1 fix against over-reach: `no_change_with_rationale` is the
    agent judging the finding answered, which is a resolution; only
    `unresolved` is "I could not do it".
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint, 1, [finding(1)])),
            (
                lambda r: touch_impl(r, "v2\n"),
                lambda e: fix_result(["R1-F1"], resolution="no_change_with_rationale"),
            ),
            (None, lambda e: review_result(e.state.workspace_fingerprint, 2)),
        ],
    )
    eng.run(max_steps=8)
    assert eng.state.phase == Phase.DONE
    assert eng.state.open_findings == []


def test_a_failed_phase_is_re_invoked_against_its_original_baseline(tmp_path):
    """R1-F2: an unverified write phase must stay resumable.

    The controller persists the invocation checkpoint *before* the agent runs,
    so a crash, a malformed CONTROL_RESULT or a failing validation command all
    leave the same record. Without it the retry compared the tree against the
    tree the failed attempt had already written and rejected it as "unchanged
    since this phase was first invoked" — a dead-locked run whose only escape
    was hand-editing `state.json`.
    """
    root = local_repo(tmp_path)
    cfg = default_config()
    marker = root / "PASS_VALIDATION"
    # Exits 0 only once the marker exists: the first attempt's validation
    # fails, the second one passes.
    cfg.local.validation_commands = [["test", "-e", str(marker)]]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    ws = LocalWorkspace(workdir=root)
    baseline = ws.snapshot().fingerprint

    eng.provider._handler = scripted(
        eng, root, [(lambda r: touch_impl(r, "v1\n"), lambda e: impl_result())]
    )
    eng.step()  # INITIALIZING -> ANALYZE_EXECUTE
    with pytest.raises(VerificationError, match="validation command"):
        eng.step()

    # The phase did not advance, and the checkpoint records both that it ran
    # and the fingerprint it started from.
    assert eng.state.phase == Phase.ANALYZE_EXECUTE
    assert eng.state.local_pending_phase == "ANALYZE_EXECUTE"
    assert eng.state.local_pending_fingerprint == baseline
    assert eng.state.local_pending_attempts == 1
    # It is durable, not in-memory: a fresh process sees the same thing.
    reloaded = load_state(eng.paths.state_file)
    assert reloaded.local_pending_fingerprint == baseline

    # Resume. The work from the first attempt is already in the tree, so this
    # attempt changes nothing further and honestly says so.
    marker.write_text("ok\n", encoding="utf-8")
    eng.provider._handler = scripted(eng, root, [(None, lambda e: impl_result(changed=False))])
    outcome = eng.step()
    assert outcome.next_phase == "REVIEW"
    assert eng.state.phase == Phase.REVIEW
    # Resolved: the checkpoint is closed so the next phase entry starts clean.
    assert eng.state.local_pending_phase == ""
    assert eng.state.local_pending_attempts == 0


def test_the_resumed_phase_prompt_tells_the_agent_about_the_earlier_attempt(tmp_path):
    """A re-invoked agent must be told work may already be in the tree.

    Otherwise it re-implements from scratch over its own half-finished output.
    """
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [["false"]]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng, root, [(lambda r: touch_impl(r, "v1\n"), lambda e: impl_result())]
    )
    eng.step()
    with pytest.raises(VerificationError):
        eng.step()

    prompts: list[str] = []

    def capture(req):
        prompts.append(req.prompt)
        return impl_result(changed=False)

    eng.provider._handler = capture
    with pytest.raises(VerificationError):
        eng.step()
    assert prompts, "the phase was not re-invoked"
    assert "Earlier attempt at this phase" in prompts[0]
    assert "never produced a result the controller could verify" in prompts[0]
    assert "continue it rather than starting over" in prompts[0]

    # The plan a human sees reports the same checkpoint, with the bound.
    notes = " ".join(eng.plan_step().notes)
    assert "was checkpointed and never verified" in notes
    assert "2 of 3 attempt(s) used" in notes


def test_a_write_phase_that_never_verifies_blocks_instead_of_looping(tmp_path):
    """The retry in R1-F2 is bounded: three attempts, then BLOCKED.

    Re-invoking a write-capable agent is cheap in LOCAL mode (the only side
    effect is the working tree) but it is not free, and a phase that can never
    be verified must not become an infinite `resume` loop.
    """
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [["false"]]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)

    counter = {"n": 0}

    def always_writes(req):
        counter["n"] += 1
        touch_impl(root, f"v{counter['n']}\n")
        return impl_result()

    eng.provider._handler = always_writes
    eng.step()  # INITIALIZING -> ANALYZE_EXECUTE
    for _ in range(3):
        with pytest.raises(VerificationError):
            eng.step()
    assert eng.state.local_pending_attempts == 3
    assert counter["n"] == 3

    # The fourth entry refuses to launch the agent at all.
    outcome = eng.step()
    assert outcome.next_phase == "BLOCKED"
    assert eng.state.phase == Phase.BLOCKED
    assert counter["n"] == 3, "a blocked phase must not invoke the agent"
    assert "without ever producing a verified result" in eng.state.block_reason


def test_a_review_that_fails_verification_leaves_no_pending_checkpoint(tmp_path):
    """REVIEW is read-only, so it is not checkpointed as a write phase."""
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result("f" * 64)),
        ],
    )
    eng.step()
    eng.step()
    with pytest.raises(VerificationError):
        eng.step()
    assert eng.state.phase == Phase.REVIEW
    assert eng.state.local_pending_phase == ""


# -- the git anchor ---------------------------------------------------------------
def test_an_agent_that_commits_blocks_the_run(tmp_path):
    """A LOCAL run is pinned to the HEAD and branch it started from.

    This is LOCAL mode's analogue of "bind reviews to the PR HEAD SHA": the
    findings and the frozen specification describe the tree as anchored, and
    the controller cannot tell an agent's commit from an operator's.
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")

    def commit(r):
        touch_impl(r, "v1\n")
        commit_all(r, "the agent committed")

    eng.provider._handler = scripted(eng, root, [(commit, lambda e: impl_result())])
    eng.step()  # INITIALIZING -> ANALYZE_EXECUTE
    outcome = eng.step()
    assert outcome.next_phase == "BLOCKED"
    assert eng.state.phase == Phase.BLOCKED
    assert "HEAD moved" in eng.state.block_reason
    assert "Nothing was rolled back" in eng.state.block_reason


def test_an_agent_that_switches_branches_blocks_the_run(tmp_path):
    """Branch identity is part of the anchor, even when HEAD does not move."""
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    assert eng.state.base_branch, "a fresh repository has a checked-out branch"

    def switch(r):
        touch_impl(r, "v1\n")
        # A new branch at the same commit: HEAD is unchanged, identity is not.
        subprocess.run(["git", "-C", str(r), "checkout", "-q", "-b", "sidetrack"], check=True)

    eng.provider._handler = scripted(eng, root, [(switch, lambda e: impl_result())])
    eng.step()
    outcome = eng.step()
    assert outcome.next_phase == "BLOCKED"
    assert "checked-out branch changed" in eng.state.block_reason
    assert "HEAD moved" not in eng.state.block_reason


def test_a_head_move_before_the_agent_runs_blocks_without_invoking_it(tmp_path):
    """The anchor is checked on the way in as well as on the way out."""
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    eng.step()  # INITIALIZING -> ANALYZE_EXECUTE

    invoked = {"n": 0}

    def handler(req):
        invoked["n"] += 1
        return impl_result()

    eng.provider._handler = handler
    touch_impl(root, "an operator committed mid-run\n")
    commit_all(root, "operator commit")

    outcome = eng.step()
    assert outcome.next_phase == "BLOCKED"
    assert invoked["n"] == 0, "the agent must not run against a moved anchor"
    assert "HEAD moved" in eng.state.block_reason


def test_the_git_anchor_is_reported_but_kept_out_of_the_fingerprint(tmp_path):
    """HEAD and branch are bound, and bound *separately* from the tree bytes.

    The fingerprint answers exactly one question -- "which bytes are in the
    working tree?" -- so a plain `git commit`, which moves HEAD and changes no
    byte of the tree, must not be reported to the operator as "the reviewer
    modified the working tree". The anchor is enforced by its own check
    (`_git_anchor_drift`), which blocks the run and says HEAD moved.
    """
    root = local_repo(tmp_path)
    ws = LocalWorkspace(workdir=root)
    on_main = ws.snapshot()
    assert on_main.branch
    assert on_main.anchor.endswith(on_main.branch)

    subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", "other"], check=True)
    on_other = ws.snapshot()
    assert on_other.branch == "other"
    assert on_other.anchor != on_main.anchor
    # Same bytes on disk, so the same fingerprint. The branch change is the
    # anchor's business.
    assert on_other.fingerprint == on_main.fingerprint

    subprocess.run(["git", "-C", str(root), "checkout", "-q", "--detach"], check=True)
    detached = ws.snapshot()
    assert detached.branch == ""
    assert "(detached)" in detached.anchor
    assert detached.fingerprint == on_main.fingerprint


# -- the state directory ------------------------------------------------------------
# -- fingerprint robustness -----------------------------------------------------------
def test_a_large_file_is_content_hashed_not_stat_hashed(tmp_path):
    """R1-F9: a same-size, same-mtime rewrite must change the fingerprint.

    The fingerprint used to fall back to `(size, mtime_ns)` above a size
    threshold, which made it metadata-bound for exactly the files where a
    silent swap is easiest to hide: a clean review could then be accepted for
    bytes no reviewer ever saw.
    """
    import os

    root = local_repo(tmp_path)
    ws = LocalWorkspace(workdir=root)
    big = root / "src" / "big.bin"
    size = 40 * 1024 * 1024  # comfortably past the old 32 MB threshold
    big.write_bytes(b"a" * size)
    st = os.stat(big)
    before = ws.snapshot().fingerprint

    # Same length, different bytes, and the timestamps restored exactly.
    big.write_bytes(b"a" * (size - 1) + b"b")
    os.utime(big, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = os.stat(big)
    assert after.st_size == st.st_size and after.st_mtime_ns == st.st_mtime_ns

    assert ws.snapshot().fingerprint != before


def test_workspace_git_reads_do_not_take_the_optional_index_lock(tmp_path):
    """`git status` refreshes the index by default, which takes `.git/index.lock`.

    AutoForge runs it against the operator's own live checkout, where an IDE
    or a concurrent `git` can hold that lock; a fingerprint read must never
    contend for it, and must never write to the index of a repository it is
    only inspecting.
    """
    root = local_repo(tmp_path)
    seen: list[list[str]] = []

    def runner(req):
        seen.append(list(req.command))
        return subprocess_result(req)

    def subprocess_result(req):
        from autoforge.executor import ExecutionResult

        proc = subprocess.run(req.command, cwd=str(req.cwd), capture_output=True)
        return ExecutionResult(
            command=list(req.command),
            cwd=str(req.cwd),
            exit_code=proc.returncode,
            stdout=proc.stdout.decode("utf-8", "replace"),
            stderr=proc.stderr.decode("utf-8", "replace"),
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
        )

    ws = LocalWorkspace(workdir=root, runner=runner)
    ws.snapshot()
    assert seen, "no git command was run"
    for cmd in seen:
        assert cmd[0] == "git"
        assert cmd[1] == "--no-optional-locks", cmd


# -- redaction at the persistence boundary --------------------------------------------
def test_an_agent_message_is_redacted_before_it_reaches_state(tmp_path):
    """R1-F5: `block_reason` is persisted in the clear and printed verbatim."""
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    secret = "ghp_" + "A" * 36
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (
                None,
                lambda e: block(
                    {
                        "phase": "ANALYZE_EXECUTE",
                        "status": "blocked",
                        "message": f"could not authenticate with {secret}",
                    }
                ),
            )
        ],
    )
    eng.step()
    outcome = eng.step()
    assert eng.state.phase == Phase.BLOCKED
    assert secret not in eng.state.block_reason
    assert secret not in outcome.message
    assert "***REDACTED***" in eng.state.block_reason
    assert secret not in eng.paths.state_file.read_text(encoding="utf-8")


def test_findings_and_resolutions_are_redacted_before_they_are_persisted(tmp_path):
    """Agent-authored finding text is persisted and re-rendered into prompts."""
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    secret = "sk-ant-" + "B" * 30

    def leaky_review(e):
        f = finding(1)
        f["required_resolution"] = f"Set ANTHROPIC_API_KEY={secret} in the test fixture."
        return review_result(e.state.workspace_fingerprint, 1, [f])

    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, leaky_review),
        ],
    )
    eng.run(max_steps=3)
    assert eng.state.phase == Phase.FIX
    persisted = eng.paths.state_file.read_text(encoding="utf-8")
    assert secret not in persisted
    assert "***REDACTED***" in json.dumps(eng.state.open_findings)


def test_run_log_metadata_is_redacted(tmp_path):
    """Metadata is caller-supplied text written to request.json and events.jsonl."""
    from autoforge.runlog import ExecutionRecord, RunLogger

    secret = "ghp_" + "C" * 36
    logger = RunLogger(tmp_path / "logs", "run-1")
    step_dir = logger.log_execution(
        ExecutionRecord(
            run_id="run-1",
            seq=0,
            phase="FIX",
            metadata={
                "validation_command": ["deploy", f"--token={secret}"],
                "nested": {"GITHUB_TOKEN": secret},
                f"key-{secret}": "value",
                "count": 3,
                "flag": True,
                "none": None,
            },
        )
    )
    request = (step_dir / "request.json").read_text(encoding="utf-8")
    events = (logger.events_path).read_text(encoding="utf-8")
    assert secret not in request and secret not in events
    assert "***REDACTED***" in request
    # Non-string scalars survive intact rather than being stringified.
    parsed = json.loads(request)["metadata"]
    assert parsed["count"] == 3 and parsed["flag"] is True and parsed["none"] is None


# -- PR #44 second review regressions (R1-F1..R1-F7) ---------------------------
def _failed_git(req, stderr: str, exit_code: int = 128):
    """An ExecutionResult shaped like a `git` invocation that failed."""
    from autoforge.executor import ExecutionResult

    return ExecutionResult(
        command=list(req.command),
        cwd=req.cwd,
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
    )


def test_local_init_refuses_a_feature_directory_reached_through_a_symlink(tmp_path):
    """R1-F2, now closed by construction rather than by a boundary check.

    The old code compared a partly lexical path against the repository root,
    and `realpath` only applied to the target's parent when that exact
    directory already existed -- so `features/new` under a `features` symlink
    looked in-repository and `mkdir(parents=True)` wrote outside the checkout.

    The rewrite does not compare pathnames at all. Every component is opened
    `O_DIRECTORY | O_NOFOLLOW` relative to the repository descriptor, so a
    symbolic link anywhere on the way down cannot be traversed, whether or not
    the rest of the path exists and whatever it points at.
    """
    root = git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "features").symlink_to(outside)
    ws = LocalWorkspace(workdir=root)

    # The nested, not-yet-existing parent is the case that used to slip through.
    with pytest.raises(UnsafePathError, match="symbolic link"):
        init_feature_file(ws, "spec", feature_dir="features/new")
    # The direct case is refused by the same walk, at the same component.
    with pytest.raises(UnsafePathError, match="symbolic link"):
        init_feature_file(ws, "spec", feature_dir="features")
    assert list(outside.iterdir()) == [], "nothing may be written outside the repository"


def test_local_init_never_writes_through_a_symlink_at_the_target(tmp_path):
    """O_NOFOLLOW on the final component: `--force` must not follow a link."""
    root = git_repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere.md"
    outside.write_text("operator content\n", encoding="utf-8")
    (root / "features").mkdir()
    (root / "features" / "spec.md").symlink_to(outside)
    ws = LocalWorkspace(workdir=root)

    with pytest.raises(ConfigurationError, match="already exists"):
        init_feature_file(ws, "spec")
    with pytest.raises(ConfigurationError, match="symbolic link, not a regular file"):
        init_feature_file(ws, "spec", overwrite=True)
    assert outside.read_text(encoding="utf-8") == "operator content\n"


def test_cli_local_init_refuses_while_another_controller_holds_the_lock(
    tmp_path, monkeypatch, capsys
):
    """R1-F3: `local init` is a controller write, so it takes the repository lock.

    A run freezes the specification's SHA-256 and re-checks it around every
    phase; `local init --force` racing an active run could rewrite the
    acceptance criteria while the controller was hashing or rendering them.
    """
    from autoforge.locking import ControllerLock, repository_lock_path

    root = git_repo(tmp_path)
    monkeypatch.chdir(root)
    other = ControllerLock(repository_lock_path(root)).acquire()
    try:
        assert main(["local", "init", "add-filter"]) == 2
    finally:
        other.release()
    err = capsys.readouterr().err
    assert "controller.lock" in err
    assert not (root / "features").exists(), "nothing may be created while the lock is held"

    # Released: the same command now succeeds.
    assert main(["local", "init", "add-filter"]) == 0
    assert (root / "features" / "add-filter.md").is_file()


def test_a_failed_fix_validation_does_not_charge_a_fix_round(tmp_path):
    """R1-F4: the fix budget counted attempts the controller never accepted.

    `local_fix_rounds` was incremented before the validation commands ran, so
    a FIX whose tests failed spent a round on work no controller verified —
    with `max_fix_rounds: 1` the retry was then refused for exhausting a
    budget it had never actually used. Re-entry stays bounded by the separate
    `local_pending_attempts` checkpoint, which counts invocations.
    """
    root = local_repo(tmp_path)
    cfg = default_config()
    # Passes while the marker is absent: ANALYZE_EXECUTE validates, the first
    # FIX attempt (which creates it) does not.
    marker = tmp_path / "FAIL_VALIDATION"
    cfg.local.validation_commands = [["test", "!", "-e", str(marker)]]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint, 1, [finding(1)])),
            (
                lambda r: (touch_impl(r, "v2\n"), marker.write_text("x", encoding="utf-8")),
                lambda e: fix_result(["R1-F1"]),
            ),
        ],
    )
    eng.step()  # INITIALIZING -> ANALYZE_EXECUTE
    eng.step()  # implementation
    eng.step()  # REVIEW -> FIX
    assert eng.state.phase == Phase.FIX
    with pytest.raises(VerificationError, match="validation command"):
        eng.step()

    assert eng.state.phase == Phase.FIX, "an unverified fix must stay resumable"
    assert eng.state.local_fix_rounds == 0, "an unverified attempt must not spend a fix round"
    assert eng.state.local_pending_phase == "FIX"
    assert eng.state.local_pending_attempts == 1
    reloaded = load_state(eng.paths.state_file)
    assert reloaded.local_fix_rounds == 0 and reloaded.local_pending_attempts == 1

    # The retry is accepted, and *that* is the round that gets charged.
    marker.unlink()
    eng.provider._handler = scripted(
        eng, root, [(lambda r: touch_impl(r, "v3\n"), lambda e: fix_result(["R1-F1"]))]
    )
    outcome = eng.step()
    assert outcome.next_phase == "REVIEW"
    assert eng.state.local_fix_rounds == 1
    assert "local fix round 1 verified" in outcome.message


def test_an_unreadable_entry_cannot_be_bound_and_fails_closed(tmp_path):
    """R1-F5: `unreadable:PermissionError` was read as a digest and compared equal.

    A stable marker for a path that could not be hashed said "I could not
    look", but behaved like "nothing changed": the bytes behind it could be
    swapped freely with the fingerprint unmoved, and the review still counted
    as bound to the workspace.

    "Unreadable" is now one fact with one answer, wherever the filesystem
    raises it -- a file that cannot be opened and a directory that cannot be
    listed reach the same refusal through the same translation point, so the
    directory case did not need its own rule.
    """
    import os

    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions, so nothing here is unreadable")
    root = local_repo(tmp_path)
    ws = LocalWorkspace(workdir=root)
    secret = root / "src" / "blob.bin"
    secret.write_bytes(b"v1")
    secret.chmod(0o000)
    try:
        with pytest.raises(VerificationError, match="not readable by the controller") as caught:
            ws.snapshot()
        assert "src/blob.bin" in str(caught.value)
        assert "local.exclude" in str(caught.value)
        # The swap the old marker hid: still refused, never "unchanged".
        secret.chmod(0o600)
        secret.write_bytes(b"v2")
        secret.chmod(0o000)
        with pytest.raises(VerificationError, match="not readable by the controller"):
            ws.snapshot()
        # Declared unreviewed, it binds -- and the declaration is disclosed.
        declared = LocalWorkspace(workdir=root, exclude=["src/blob.bin"]).snapshot()
        assert "exclude:src/blob.bin" in declared.describe_exclusions()
    finally:
        secret.chmod(0o600)
    assert ws.snapshot().fingerprint  # readable again: a normal content hash

    # A directory the controller cannot list is the same refusal, not a
    # traversal that silently reports fewer entries.
    closed = root / "src" / "closed"
    closed.mkdir()
    (closed / "inner.txt").write_text("v1\n", encoding="utf-8")
    closed.chmod(0o000)
    try:
        with pytest.raises(VerificationError, match="not readable by the controller") as caught:
            ws.snapshot()
        assert "src/closed" in str(caught.value)
    finally:
        closed.chmod(0o700)


def test_a_second_working_tree_is_refused_whether_or_not_it_is_dirty(tmp_path):
    """R1-F5, generalised: a submodule is not a dirty-tree problem.

    The old code bound a submodule as the `"dir"` marker `git status` gave it,
    so every change inside it -- and inside any untracked nested repository --
    was invisible to the fingerprint. Making the *dirty* case an error would
    have been another special case, and it would still have depended on git
    noticing.

    The rule now is structural and checked by the controller's own walk: a
    `.git` entry below the root means a second working tree, whose contents
    this snapshot cannot own. That is refused at bind time, clean or dirty,
    tracked or untracked, unless the operator declares it unreviewed.
    """
    root = local_repo(tmp_path / "repo")
    upstream = tmp_path / "upstream"
    subprocess.run(["git", "init", "-q", str(upstream)], check=True)
    (upstream / "f.txt").write_text("v1\n", encoding="utf-8")
    commit_all(upstream, "upstream")
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(upstream),
            "vendor",
        ],
        check=True,
    )
    commit_all(root, "add submodule")

    ws = LocalWorkspace(workdir=root)
    # Clean by git's account, and still refused: "git says nothing changed" is
    # not the same fact as "the controller can say which bytes are there".
    with pytest.raises(VerificationError, match="nested git repository or submodule") as clean:
        ws.snapshot()
    assert "vendor/.git" in str(clean.value)
    assert "local.exclude" in str(clean.value)

    (root / "vendor" / "f.txt").write_text("v2\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="nested git repository or submodule"):
        ws.snapshot()

    # An untracked nested repository is the same shape, so it is the same rule
    # and the same refusal -- not a second condition somewhere else.
    nested = root / "tool"
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    (nested / "x.txt").write_text("v1\n", encoding="utf-8")
    declared = LocalWorkspace(workdir=root, exclude=["vendor"])
    with pytest.raises(VerificationError, match="nested git repository or submodule") as untracked:
        declared.snapshot()
    assert "tool/.git" in str(untracked.value)

    # Declared unreviewed, both of them: the snapshot binds, and says so.
    ok = LocalWorkspace(workdir=root, exclude=["vendor", "tool"]).snapshot()
    assert ok.fingerprint
    assert "exclude:vendor" in ok.describe_exclusions()
    assert "exclude:tool" in ok.describe_exclusions()
    # And the exclusions are part of identity: the same tree bound without
    # them could never compare equal.
    assert (
        ok.fingerprint
        != LocalWorkspace(workdir=root, exclude=["tool", "vendor", "x"]).snapshot().fingerprint
    )


def test_a_failed_anchor_read_is_not_evidence_that_nothing_moved(tmp_path):
    """R1-F6: a failing `git rev-parse` used to look exactly like an unborn HEAD.

    Both collapsed into `""`, so a damaged repository, a permissions change
    or a missing object store read as "HEAD has not moved" and the run
    continued over a git identity the controller could no longer establish.
    """
    from autoforge.executor import execute

    root = local_repo(tmp_path)

    def breaking(argv: list[str]):
        """Real git, except that the two anchor reads fail with git's own 128."""

        def runner(req):
            if req.command[2:] == argv:
                return _failed_git(
                    req, "fatal: not a git repository (or any of the parent directories)"
                )
            return execute(req)

        return runner

    head_argv = ["rev-parse", "--verify", "--quiet", "HEAD"]
    branch_argv = ["symbolic-ref", "--quiet", "--short", "HEAD"]

    ws = LocalWorkspace(workdir=root, runner=breaking(head_argv))
    with pytest.raises(VerificationError, match="cannot read HEAD"):
        ws.head_sha()
    ws = LocalWorkspace(workdir=root, runner=breaking(branch_argv))
    with pytest.raises(VerificationError, match="cannot read the checked-out branch"):
        ws.branch()

    # Exit 1 remains the observed fact it always was.
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "--detach"], check=True)
    plain = LocalWorkspace(workdir=root)
    assert plain.branch() == ""
    assert plain.head_sha()
    empty = LocalWorkspace(workdir=git_repo(tmp_path / "empty"))
    assert empty.head_sha() == ""


def test_a_failed_anchor_read_blocks_before_the_agent_is_invoked(tmp_path):
    """The controller must not launch a write-capable agent it cannot anchor."""
    from autoforge.executor import execute

    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    eng.step()  # INITIALIZING -> ANALYZE_EXECUTE

    invoked = {"n": 0}

    def handler(req):
        invoked["n"] += 1
        return impl_result()

    eng.provider._handler = handler

    def runner(req):
        if req.command[2:] == ["rev-parse", "--verify", "--quiet", "HEAD"]:
            return _failed_git(req, "fatal: bad object HEAD")
        return execute(req)

    eng.workspace()._runner = runner
    with pytest.raises(VerificationError, match="cannot read HEAD"):
        eng.step()
    assert invoked["n"] == 0, "the agent must not run against an unreadable anchor"
    assert eng.state.phase == Phase.ANALYZE_EXECUTE


def test_a_state_file_with_an_unknown_field_is_corruption(tmp_path):
    """R1-F7: unknown fields were dropped, so a newer state loaded as an older one.

    `from_dict` deleted anything it did not recognise and carried on, which
    turned a state file written by a controller that knows about (say) a
    pending-invocation checkpoint into a valid-looking file without one.
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    path = StatePaths.from_state_dir(root / ".autoforge").state_file
    save_state(eng.state, path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    bad = dict(payload, local_future_checkpoint="something this controller ignores")
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(StateError, match="unknown field"):
        load_state(path)

    # A newer protocol version is reported as such, not as a pile of unknown
    # fields: the protocol check runs first so the message names the cause.
    bad["protocol_version"] = "999.0"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(StateError, match="protocol_version"):
        load_state(path)


def test_a_state_file_with_a_malformed_pending_checkpoint_is_refused(tmp_path):
    """R1-F7: the pending-invocation checkpoint is a bound, so it is validated.

    It decides whether a write-capable agent is re-invoked and against which
    baseline, and `MAX_LOCAL_PHASE_ATTEMPTS` is enforced on the persisted
    counter. A hand-edited or truncated checkpoint must fail loudly rather
    than silently disable the bound.
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    path = StatePaths.from_state_dir(root / ".autoforge").state_file
    save_state(eng.state, path)
    good = json.loads(path.read_text(encoding="utf-8"))
    assert good["mode"] == "LOCAL"

    def refuses(match: str, **fields):
        path.write_text(json.dumps({**good, **fields}), encoding="utf-8")
        with pytest.raises(StateError, match=match):
            load_state(path)

    refuses("non-negative integer", local_fix_rounds=-1)
    refuses("non-negative integer", local_pending_attempts=-1)
    refuses("non-negative integer", review_round=-1)
    refuses("non-negative integer", step_count=-1)
    # A phase that is not a phase, and one whose agent cannot write.
    refuses(
        "local_pending_phase",
        local_pending_phase="NOT_A_PHASE",
        local_pending_fingerprint="a" * 64,
        local_pending_attempts=1,
    )
    refuses(
        "local_pending_phase",
        local_pending_phase="REVIEW",
        local_pending_fingerprint="a" * 64,
        local_pending_attempts=1,
    )
    # Partial checkpoints in both directions.
    refuses("local_pending_fingerprint", local_pending_phase="FIX", local_pending_attempts=1)
    refuses(
        "local_pending_attempts",
        local_pending_phase="FIX",
        local_pending_fingerprint="a" * 64,
        local_pending_attempts=0,
    )
    refuses("local_pending_fingerprint", local_pending_fingerprint="a" * 64)
    refuses("local_pending_attempts", local_pending_attempts=2)
    # A checkpoint on a REMOTE run is not a checkpoint at all.
    remote = {
        **good,
        "mode": "REMOTE",
        "repository": "owner/repo",
        "epic_url": "https://github.com/owner/repo/issues/1",
        "local_pending_phase": "FIX",
        "local_pending_fingerprint": "a" * 64,
        "local_pending_attempts": 1,
    }
    path.write_text(json.dumps(remote), encoding="utf-8")
    with pytest.raises(StateError, match="on a REMOTE run"):
        load_state(path)

    # The well-formed checkpoint still loads.
    path.write_text(
        json.dumps(
            {
                **good,
                "local_pending_phase": "FIX",
                "local_pending_fingerprint": "a" * 64,
                "local_pending_attempts": 2,
            }
        ),
        encoding="utf-8",
    )
    assert load_state(path).local_pending_attempts == 2


# -- PR #44 R4: the fingerprint's edges ---------------------------------------
def test_a_mode_change_is_bound_even_with_core_filemode_disabled(tmp_path):
    """R4-F4: `core.fileMode=false` hides `chmod +x` on a *clean* tracked file.

    Git then reports no porcelain entry at all, so the path is never
    inspected and neither its mode nor its bytes reach the fingerprint: a
    clean review stayed valid across a change to what a validation command
    does with that script. The porcelain read forces the setting on, so which
    paths are bound no longer depends on a repository setting.
    """
    root = local_repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "config", "core.fileMode", "false"], check=True)
    ws = LocalWorkspace(workdir=root)
    clean = ws.snapshot().fingerprint

    script = root / IMPL_FILE  # committed, clean, not executable
    script.chmod(0o755)
    assert ws.snapshot().fingerprint != clean
    assert [e.mode for e in ws.snapshot().entries if e.path == IMPL_FILE] == ["0755"]

    script.chmod(0o644)
    assert ws.snapshot().fingerprint == clean


def test_a_symlink_out_of_the_repository_cannot_be_bound(tmp_path):
    """R4-F5: only the link text was bound, so the bytes could be swapped freely."""
    root = local_repo(tmp_path / "repo")
    outside = tmp_path / "external.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    ws = LocalWorkspace(workdir=root)

    (root / "src" / "linked.py").symlink_to(outside)
    with pytest.raises(VerificationError, match="outside the working tree"):
        ws.snapshot()

    # A link *into* the working tree is fine: its target is a path of this
    # tree, so changing it moves that path's own entry in the fingerprint.
    (root / "src" / "linked.py").unlink()
    (root / "src" / "linked.py").symlink_to(root / IMPL_FILE)
    linked = ws.snapshot().fingerprint
    touch_impl(root, "def main():\n    return 2\n")
    assert ws.snapshot().fingerprint != linked


def test_a_local_state_cannot_hold_a_github_only_phase(tmp_path):
    """R4-F6: `Phase(...)` proves the value exists, not that this run can be in it.

    A persisted LOCAL run in READY_FOR_MERGE loaded cleanly and `resume` then
    read it as the remote merge hold, for a run with no repository and no PR.
    """
    path = tmp_path / "state.json"
    good = {
        "run_id": "af-x",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "mode": "LOCAL",
        "feature_spec_path": "docs/feature.md",
        "feature_spec_sha256": "a" * 64,
        "phase": "REVIEW",
    }
    assert load_state_from(path, good).phase == Phase.REVIEW
    for phase in ("READY_FOR_MERGE", "MERGE", "UPDATE_EPIC", "REPLAN_REEXECUTE"):
        with pytest.raises(StateError, match="belongs to the GitHub workflow"):
            load_state_from(path, {**good, "phase": phase})
    # The same phases remain perfectly valid for a REMOTE run.
    remote = {
        **good,
        "mode": "REMOTE",
        "repository": "owner/repo",
        "epic_url": "https://github.com/owner/repo/issues/1",
        "phase": "READY_FOR_MERGE",
    }
    del remote["feature_spec_path"], remote["feature_spec_sha256"]
    assert load_state_from(path, remote).phase == Phase.READY_FOR_MERGE


def load_state_from(path: Path, payload: dict) -> AutoForgeState:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_state(path)


def test_a_successful_local_fix_cannot_also_report_a_blocker():
    """R4-F7: `blocked_reason` was parsed on a successful FIX and then dropped.

    A fix could report a run-level obstacle, pass validation, be reviewed
    clean and reach DONE with nothing having carried the blocker anywhere.
    The protocol already has one channel for it — status "blocked" — and the
    engine routes that to BLOCKED, so a result claiming both is refused
    rather than silently keeping the half the controller acts on.
    """
    from autoforge.errors import ControlResultValidationError
    from autoforge.result_parser import parse_control_result

    payload = {
        "phase": "FIX",
        "status": "success",
        "changed_workspace": True,
        "resolutions": [{"finding_id": "R1-F1", "resolution": "fixed"}],
        "blocked_reason": "the test database is unreachable",
    }
    with pytest.raises(ControlResultValidationError, match="blocked_reason"):
        parse_control_result(block(payload), Phase.FIX, WorkflowMode.LOCAL)

    # An empty (or absent) field is not a claim and stays accepted.
    parse_control_result(block({**payload, "blocked_reason": ""}), Phase.FIX, WorkflowMode.LOCAL)
    parse_control_result(
        block({k: v for k, v in payload.items() if k != "blocked_reason"}),
        Phase.FIX,
        WorkflowMode.LOCAL,
    )
    # ... and the blocker still has a home of its own.
    blocked = parse_control_result(
        block({"phase": "FIX", "status": "blocked", "message": "db unreachable"}),
        Phase.FIX,
        WorkflowMode.LOCAL,
    )
    assert blocked["status"] == "blocked"


def test_a_local_fix_that_reports_a_blocker_ends_the_run_in_blocked(tmp_path):
    """The engine's side of the same result: status 'blocked' is terminal."""
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint, 1, [finding(1)])),
            (
                None,
                lambda e: block(
                    {"phase": "FIX", "status": "blocked", "message": "toolchain is unavailable"}
                ),
            ),
        ],
    )
    eng.run(max_steps=8)
    assert eng.state.phase == Phase.BLOCKED
    assert "toolchain is unavailable" in (eng.state.block_reason or "")
