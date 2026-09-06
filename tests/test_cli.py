"""CLI: --help, doctor, run/step/resume/status with injected fakes (no real gh/agents)."""

import json
import os

import pytest

from autoforge import cli
from autoforge.engine import ControllerEngine
from autoforge.executor import ExecutionResult
from autoforge.github import CheckInfo
from autoforge.providers import ProviderRegistry, ScriptedProvider
from autoforge.state import AutoForgeState, load_state, quarantine_state_file, save_state
from autoforge.transitions import Phase
from tests.conftest import (
    BRANCH,
    EPIC,
    ISSUE,
    PR,
    SHA_A,
    FakeGitHub,
    block,
    comment_url,
    review_comment_body,
)


@pytest.fixture
def fakes(monkeypatch):
    """Route every engine the CLI builds through FakeGitHub + a scripted agent."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Feature")
    holder = {"gh": gh, "handler": lambda req: ""}

    def handler(req):
        return holder["handler"](req)

    provider = ScriptedProvider(handler)
    holder["provider"] = provider
    real = ControllerEngine.__init__

    def patched(self, *a, **k):
        k.setdefault("github", gh)
        k.setdefault(
            "providers", ProviderRegistry(overrides={"claude": provider, "opencode": provider})
        )
        real(self, *a, **k)

    monkeypatch.setattr(ControllerEngine, "__init__", patched)
    return holder


def test_help(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for sub in ("doctor", "run", "step", "resume", "status"):
        assert sub in out


def test_status_without_state_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["--state-dir", str(tmp_path / ".autoforge"), "status"])
    assert rc == 2
    assert "no state" in capsys.readouterr().err.lower()


def test_run_dry_run_writes_nothing(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    rc = cli.main(["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "INITIALIZING" in out and "ANALYZE_EXECUTE" in out
    assert "claude" in out and "fable" in out and "analyze_execute.md" in out
    assert "Expected next" in out and "Command" in out
    assert not sd.exists()
    assert fakes["gh"].calls == [] and fakes["provider"].calls == []


def test_run_bad_url_rejected(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(
        [
            "--state-dir",
            str(tmp_path / ".autoforge"),
            "run",
            "--epic",
            "not-a-url",
            "--issue",
            ISSUE,
            "--dry-run",
        ]
    )
    assert rc == 2


def test_run_issue_repo_mismatch_rejected(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(
        [
            "--state-dir",
            str(tmp_path / ".autoforge"),
            "run",
            "--epic",
            EPIC,
            "--issue",
            "https://github.com/other/repo/issues/2",
            "--dry-run",
        ]
    )
    assert rc == 2


def test_run_then_status_json(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    rc = cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
    assert rc == 0
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["phase"] == "ANALYZE_EXECUTE"
    assert data["repository"] == "owner/repo" and data["step_count"] == 1
    assert cli.main(["--state-dir", sd, "status"]) == 0
    assert "ANALYZE_EXECUTE" in capsys.readouterr().out


def test_run_refuses_to_overwrite_active_state(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    rc = cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
    assert rc == 2 and "--force" in capsys.readouterr().err
    assert (
        cli.main(
            [
                "--state-dir",
                sd,
                "run",
                "--epic",
                EPIC,
                "--issue",
                ISSUE,
                "--max-steps",
                "1",
                "--force",
            ]
        )
        == 0
    )


def test_full_run_prints_ready_banner(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    gh = fakes["gh"]

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
            return block(
                {
                    "phase": "ANALYZE_EXECUTE",
                    "status": "success",
                    "issue_url": ISSUE,
                    "pr_url": PR,
                    "head_sha": SHA_A,
                    "branch": BRANCH,
                }
            )
        gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
        return block(
            {
                "phase": "REVIEW",
                "status": "success",
                "round": 1,
                "reviewed_head_sha": SHA_A,
                "review_comment_url": comment_url(PR, 100),
                "needs_fix_round": False,
                "findings": [],
            }
        )

    fakes["handler"] = agent
    sd = str(tmp_path / ".autoforge")
    rc = cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE])
    out = capsys.readouterr().out
    assert rc == 0
    assert "AutoForge workflow reached READY_FOR_MERGE." in out
    assert "Automatic merge is disabled in this milestone." in out
    assert PR in out and ISSUE in out and SHA_A in out and "Review round:  1" in out
    # resume on a held state re-prints the banner and does nothing else
    n_calls = len(fakes["provider"].calls)
    assert cli.main(["--state-dir", sd, "resume"]) == 0
    assert "READY_FOR_MERGE" in capsys.readouterr().out
    assert len(fakes["provider"].calls) == n_calls
    # step on the held state is refused by the gate (rc 1) and never merges
    assert cli.main(["--state-dir", sd, "step"]) == 1
    assert "merge is disabled" in capsys.readouterr().err
    assert cli.main(["--state-dir", sd, "step", "--allow-merge"]) == 1
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "READY_FOR_MERGE"


def _drive_to_ready(fakes):
    """Agent script: ANALYZE_EXECUTE creates the PR, REVIEW is clean, UPDATE_EPIC ends."""
    gh = fakes["gh"]

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
            return block(
                {
                    "phase": "ANALYZE_EXECUTE",
                    "status": "success",
                    "issue_url": ISSUE,
                    "pr_url": PR,
                    "head_sha": SHA_A,
                    "branch": BRANCH,
                }
            )
        if req.phase == "UPDATE_EPIC":
            return block({"phase": "UPDATE_EPIC", "status": "success", "next_issue_url": None})
        gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
        return block(
            {
                "phase": "REVIEW",
                "status": "success",
                "round": 1,
                "reviewed_head_sha": SHA_A,
                "review_comment_url": comment_url(PR, 100),
                "needs_fix_round": False,
                "findings": [],
            }
        )

    fakes["handler"] = agent


def _gate_open_config(tmp_path, attempts: int = 5) -> str:
    cfg = tmp_path / "autoforge.json"
    cfg.write_text(
        json.dumps(
            {
                "safety": {"allow_merge": True},
                "merge": {"max_verification_attempts": attempts},
            }
        )
    )
    return str(cfg)


def test_resume_with_gate_open_rechecks_inconclusive_ready_for_merge_then_blocks(
    tmp_path, capsys, monkeypatch, fakes
):
    """R1-F1: the bounded READY_FOR_MERGE re-check is reachable through `resume`."""
    monkeypatch.chdir(tmp_path)
    _drive_to_ready(fakes)
    gh = fakes["gh"]
    cfg = _gate_open_config(tmp_path, attempts=3)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--config", cfg, "--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE]) == 0
    )
    assert "Automatic merge is disabled" in capsys.readouterr().out  # no --allow-merge: holds
    gh.prs[PR].mergeable = "UNKNOWN"

    # gate closed (flag missing): resume is a no-op banner, GitHub is not read
    n_reads = gh.calls.count(("get_pr", PR))
    assert cli.main(["--config", cfg, "--state-dir", sd, "resume"]) == 0
    assert "A human must review and merge the PR." in capsys.readouterr().out
    assert gh.calls.count(("get_pr", PR)) == n_reads

    # gate open: each resume performs one verification attempt and keeps the phase
    for n in (1, 2):
        assert cli.main(["--config", cfg, "--state-dir", sd, "resume", "--allow-merge"]) == 1
        err = capsys.readouterr().err
        assert "not determined mergeability" in err and f"attempt {n}/3" in err
        assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["phase"] == "READY_FOR_MERGE" and status["attempt"] == n
    assert gh.calls.count(("get_pr", PR)) == n_reads + 2

    # bound reached: BLOCKED, nothing merged
    assert cli.main(["--config", cfg, "--state-dir", sd, "resume", "--allow-merge"]) == 1
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "max_verification_attempts=3" in out
    assert gh.merges == []
    n_calls = len(fakes["provider"].calls)
    assert cli.main(["--config", cfg, "--state-dir", sd, "resume", "--allow-merge"]) == 1
    assert len(fakes["provider"].calls) == n_calls  # terminal: nothing re-run


def test_resume_with_gate_open_merges_via_controller_to_done(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    _drive_to_ready(fakes)
    gh = fakes["gh"]
    cfg = _gate_open_config(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--config", cfg, "--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE]) == 0
    )
    capsys.readouterr()
    gh.prs[PR].checks = [CheckInfo(name="ci", state="IN_PROGRESS")]
    assert cli.main(["--config", cfg, "--state-dir", sd, "resume", "--allow-merge"]) == 1
    assert "still running: ci" in capsys.readouterr().err
    gh.prs[PR].checks = [CheckInfo(name="ci", state="COMPLETED", conclusion="SUCCESS")]

    # --max-steps 1 exhausts the budget in MERGE: banner is not the "disabled" one
    assert (
        cli.main(
            ["--config", cfg, "--state-dir", sd, "resume", "--allow-merge", "--max-steps", "1"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "READY_FOR_MERGE -> MERGE" in out or "MERGE" in out
    assert "Automatic merge is disabled" not in out
    assert gh.merges == []

    assert cli.main(["--config", cfg, "--state-dir", sd, "resume", "--allow-merge"]) == 0
    out = capsys.readouterr().out
    assert gh.merges == [(PR, "squash", SHA_A, False)]
    assert [c.phase for c in fakes["provider"].calls] == [
        "ANALYZE_EXECUTE",
        "REVIEW",
        "UPDATE_EPIC",
    ]
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "DONE"


def test_ready_banner_with_gate_open_and_exhausted_budget(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    _drive_to_ready(fakes)
    cfg = _gate_open_config(tmp_path)
    sd = str(tmp_path / ".autoforge")
    # budget ends exactly at READY_FOR_MERGE with the gate open
    rc = cli.main(
        [
            "--config",
            cfg,
            "--state-dir",
            sd,
            "run",
            "--allow-merge",
            "--max-steps",
            "3",
            "--epic",
            EPIC,
            "--issue",
            ISSUE,
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0 and "AutoForge workflow reached READY_FOR_MERGE." in out
    assert "step budget (--max-steps) ran out before MERGE" in out
    assert "Automatic merge is disabled" not in out
    assert fakes["gh"].merges == []


def test_blocked_run_returns_1(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    fakes["handler"] = lambda req: block(
        {"phase": "ANALYZE_EXECUTE", "status": "blocked", "message": "spec unclear"}
    )
    sd = str(tmp_path / ".autoforge")
    assert cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE]) == 1
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "spec unclear" in out


def test_resume_without_state_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--state-dir", str(tmp_path / ".autoforge"), "resume"]) == 2


def test_step_dry_run_after_run(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "step", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "ANALYZE_EXECUTE" in out and "analyze_execute.md" in out
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["step_count"] == 1  # dry-run did not count


def test_dry_run_redacts_secrets(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "analyze_execute": {
                        "extra_args": ["--token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"]
                    }
                },
            }
        )
    )
    rc = cli.main(
        [
            "--config",
            str(cfg),
            "--state-dir",
            str(tmp_path / ".autoforge"),
            "run",
            "--epic",
            EPIC,
            "--issue",
            ISSUE,
            "--dry-run",
            "--full-prompt",
        ]
    )
    assert rc == 0
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in capsys.readouterr().out


def test_doctor_with_fake_runner(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def runner(req):
        argv = req.command
        if argv[:2] == ["gh", "auth"]:
            return ExecutionResult(argv, req.cwd, 1, "", "not logged in", "t", "t")
        if argv[:3] == ["git", "remote", "get-url"]:
            return ExecutionResult(argv, req.cwd, 0, "git@github.com:o/r.git\n", "", "t", "t")
        return ExecutionResult(argv, req.cwd, 0, "ok 1.0\n", "", "t", "t")

    monkeypatch.setattr(cli, "_doctor_runner", lambda: runner)
    rc = cli.main(["--state-dir", str(tmp_path / ".autoforge"), "doctor"])
    out = capsys.readouterr().out
    assert rc == 1 and "gh authenticated" in out and "FAIL" in out
    assert cli.main(["--state-dir", str(tmp_path / ".autoforge"), "doctor", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert any(c["name"] == "GitHub remote" and c["ok"] for c in data["checks"])


def test_resume_never_resets_the_step_budget(tmp_path, capsys, monkeypatch, fakes):
    """workflow.max_total_steps is enforced on the persisted step_count across resume."""
    monkeypatch.chdir(tmp_path)
    gh = fakes["gh"]
    rounds = {"n": 0}

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
            return block(
                {
                    "phase": "ANALYZE_EXECUTE",
                    "status": "success",
                    "issue_url": ISSUE,
                    "pr_url": PR,
                    "head_sha": SHA_A,
                    "branch": BRANCH,
                }
            )
        if req.phase == "REVIEW":
            rounds["n"] += 1
            rnd = rounds["n"]
            sha = gh.prs[PR].head_sha
            gh.add_comment(PR, 100 + rnd, review_comment_body(rnd, sha, True, [f"R{rnd}-F1"]))
            return block(
                {
                    "phase": "REVIEW",
                    "status": "success",
                    "round": rnd,
                    "reviewed_head_sha": sha,
                    "review_comment_url": comment_url(PR, 100 + rnd),
                    "needs_fix_round": True,
                    "findings": [
                        {
                            "id": f"R{rnd}-F1",
                            "classification": "nit",
                            "title": "t",
                            "location": "src/x.py:1",
                            "required_resolution": f"different text {rnd}",
                        }
                    ],
                }
            )
        prev = gh.prs[PR].head_sha
        new = f"{rounds['n']:040x}"
        gh.set_head(new)
        return block(
            {
                "phase": "FIX",
                "status": "success",
                "previous_head_sha": prev,
                "new_head_sha": new,
                "resolutions": [{"finding_id": f"R{rounds['n']}-F1", "resolution": "fixed"}],
            }
        )

    fakes["handler"] = agent
    cfg = tmp_path / "autoforge.json"
    cfg.write_text(
        json.dumps(
            {
                "workflow": {
                    "max_total_steps": 4,
                    "stagnation_identical_rounds": 0,
                    "stagnation_unchanged_count_rounds": 0,
                }
            }
        )
    )
    sd = str(tmp_path / ".autoforge")
    base = ["--config", str(cfg), "--state-dir", sd]
    rc = cli.main([*base, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "3"])
    assert rc == 0
    capsys.readouterr()
    assert cli.main([*base, "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["step_count"] == 3
    # resume with a fresh --max-steps 50 gets exactly the one remaining step, then BLOCKED
    rc = cli.main([*base, "resume", "--max-steps", "50"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "is BLOCKED" in out and "workflow.max_total_steps=4" in out
    assert cli.main([*base, "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["phase"] == "BLOCKED" and data["step_count"] == 4
    assert len(fakes["provider"].calls) == 3  # ANALYZE_EXECUTE, REVIEW, FIX


def test_run_refuses_corrupt_state_without_force(tmp_path, capsys, monkeypatch, fakes):
    """Issue #11: a corrupted state.json must be fatal for 'run' (exit 2), file untouched."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    raw = '{"phase": "REVIEW", "run_id": '
    (sd / "state.json").write_text(raw, encoding="utf-8")
    rc = cli.main(
        ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "corrupt" in err.lower() and "--force" in err
    assert (sd / "state.json").read_text(encoding="utf-8") == raw
    assert sorted(p.name for p in sd.iterdir()) == ["controller.lock", "state.json"]


def test_run_refuses_invalid_utf8_state_without_force(tmp_path, capsys, monkeypatch, fakes):
    """A state.json holding invalid UTF-8 is corrupt: exit 2, bytes untouched (R1-F1)."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    raw = b'\xff\xfe{"phase": "REVIEW", "run_id": "x"}'
    (sd / "state.json").write_bytes(raw)
    rc = cli.main(
        ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "corrupt" in err.lower() and "utf-8" in err.lower() and "--force" in err
    assert (sd / "state.json").read_bytes() == raw
    assert sorted(p.name for p in sd.iterdir()) == ["controller.lock", "state.json"]


def test_run_refuses_foreign_protocol_state_without_force(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    raw = json.dumps({"phase": "TELEPORT", "run_id": "x"})
    (sd / "state.json").write_text(raw, encoding="utf-8")
    rc = cli.main(
        ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    )
    err = capsys.readouterr().err
    assert rc == 2 and "unknown phase" in err.lower() and "--force" in err
    assert (sd / "state.json").read_text(encoding="utf-8") == raw
    assert sorted(p.name for p in sd.iterdir()) == ["controller.lock", "state.json"]


def test_run_force_moves_corrupt_state_aside(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    raw = '{"phase": "REVIEW", "run_id": '
    (sd / "state.json").write_text(raw, encoding="utf-8")
    rc = cli.main(
        [
            "--state-dir",
            str(sd),
            "run",
            "--epic",
            EPIC,
            "--issue",
            ISSUE,
            "--max-steps",
            "1",
            "--force",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    quarantined = [p for p in sd.iterdir() if p.name.startswith("state.json.corrupt-")]
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == raw
    assert str(quarantined[0]) in err
    assert cli.main(["--state-dir", str(sd), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "ANALYZE_EXECUTE"


def test_run_force_moves_invalid_utf8_state_aside(tmp_path, capsys, monkeypatch, fakes):
    """run --force quarantines an invalid-UTF-8 state.json and keeps the original bytes (R1-F1)."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    raw = b'\xff\xfe{"phase": "REVIEW", "run_id": "x"}'
    (sd / "state.json").write_bytes(raw)
    rc = cli.main(
        [
            "--state-dir",
            str(sd),
            "run",
            "--epic",
            EPIC,
            "--issue",
            ISSUE,
            "--max-steps",
            "1",
            "--force",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    quarantined = [p for p in sd.iterdir() if p.name.startswith("state.json.corrupt-")]
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == raw
    assert str(quarantined[0]) in err
    assert cli.main(["--state-dir", str(sd), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "ANALYZE_EXECUTE"


def test_run_refuses_dangling_symlink_state_without_force(tmp_path, capsys, monkeypatch, fakes):
    """R4-F2: a dangling state.json symlink is an existing entry: exit 2, link untouched."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    (sd / "state.json").symlink_to("missing-state.json")
    rc = cli.main(
        ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "symbolic link" in err.lower() and "--force" in err
    assert (sd / "state.json").is_symlink()
    assert os.readlink(sd / "state.json") == "missing-state.json"
    assert sorted(p.name for p in sd.iterdir()) == ["controller.lock", "state.json"]


def test_run_force_moves_dangling_symlink_state_aside(tmp_path, capsys, monkeypatch, fakes):
    """R4-F2: run --force archives the link itself (not its target) and starts a regular file."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    (sd / "state.json").symlink_to("missing-state.json")
    rc = cli.main(
        [
            "--state-dir",
            str(sd),
            "run",
            "--epic",
            EPIC,
            "--issue",
            ISSUE,
            "--max-steps",
            "1",
            "--force",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    quarantined = [p for p in sd.iterdir() if p.name.startswith("state.json.corrupt-")]
    assert len(quarantined) == 1
    assert quarantined[0].is_symlink() and os.readlink(quarantined[0]) == "missing-state.json"
    assert str(quarantined[0]) in err
    assert not (sd / "state.json").is_symlink()
    assert cli.main(["--state-dir", str(sd), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "ANALYZE_EXECUTE"


def _other_controller_state(run_id: str) -> AutoForgeState:
    return AutoForgeState(
        run_id=run_id,
        repository="owner/repo",
        epic_url=EPIC,
        current_issue_url=ISSUE,
        phase=Phase.ANALYZE_EXECUTE,
        created_at="2026-09-06T00:00:00+00:00",
        updated_at="2026-09-06T00:00:00+00:00",
    )


def _interleave_before_first_lock(monkeypatch, action):
    """Run ``action()`` once, right when the CLI first takes the controller lock.

    Simulates another controller finishing its work in the window between
    the caller's pre-lock view of the state directory and its lock acquisition.
    """
    real_acquire = cli.ControllerLock.acquire
    fired = []

    def racing_acquire(self):
        lock = real_acquire(self)
        if not fired:
            fired.append(True)
            action()
        return lock

    monkeypatch.setattr(cli.ControllerLock, "acquire", racing_acquire)
    return fired


def test_run_force_never_quarantines_state_saved_by_a_concurrent_controller(
    tmp_path, capsys, monkeypatch, fakes
):
    """R4-F1: the 'corrupt' verdict is taken under the lock, never from a stale pre-lock view.

    Two 'run --force' see the same corrupt file. The first quarantines it and
    saves a fresh run before the second gets the lock. The second must not
    quarantine that fresh, valid state as though it were the corrupt file it
    saw earlier; it only discards it as a normal --force over a live run.
    """
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    sf = sd / "state.json"
    raw = '{"phase": "REVIEW", "run_id": '
    sf.write_text(raw, encoding="utf-8")

    def first_controller_finishes():
        quarantine_state_file(sf)
        save_state(_other_controller_state("af-other"), sf)

    fired = _interleave_before_first_lock(monkeypatch, first_controller_finishes)
    rc = cli.main(
        [
            "--state-dir",
            str(sd),
            "run",
            "--epic",
            EPIC,
            "--issue",
            ISSUE,
            "--max-steps",
            "1",
            "--force",
        ]
    )
    assert rc == 0 and fired
    quarantined = [p for p in sd.iterdir() if p.name.startswith("state.json.corrupt-")]
    assert len(quarantined) == 1, "the fresh valid state must not be quarantined"
    assert quarantined[0].read_text(encoding="utf-8") == raw
    assert "moved unreadable state file aside" not in capsys.readouterr().err
    assert cli.main(["--state-dir", str(sd), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] != "af-other"


def test_run_refuses_state_created_by_a_concurrent_controller_before_lock(
    tmp_path, capsys, monkeypatch, fakes
):
    """R4-F1: a state entry appearing after the pre-lock check is seen under the lock."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    sf = sd / "state.json"

    fired = _interleave_before_first_lock(
        monkeypatch, lambda: save_state(_other_controller_state("af-other"), sf)
    )
    rc = cli.main(
        ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    )
    err = capsys.readouterr().err
    assert rc == 2 and fired
    assert "af-other" in err and "--force" in err
    assert load_state(sf).run_id == "af-other"
    assert sorted(p.name for p in sd.iterdir()) == ["controller.lock", "state.json"]
