"""CLI: --help, doctor, run/step/resume/status with injected fakes (no real gh/agents)."""

import json

import pytest

from autoforge import cli
from autoforge.engine import ControllerEngine
from autoforge.executor import ExecutionResult
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
