"""CLI: --help, doctor, run/step/resume/status with injected fakes (no real gh/agents)."""

import json

import pytest

from autoforge import cli
from autoforge.engine import ControllerEngine
from autoforge.executor import ExecutionResult
from autoforge.github import CheckInfo
from autoforge.providers import ProviderRegistry, ScriptedProvider
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
