"""CLI: --help, doctor, run/step/resume/status with injected fakes (no real gh/agents)."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autoforge import cli
from autoforge.engine import ControllerEngine
from autoforge.errors import GitHubError, LockError
from autoforge.executor import ExecutionResult, execute
from autoforge.locking import ControllerLock, IssueRunLock, repository_lock_path
from autoforge.providers import ProviderRegistry, ScriptedProvider
from autoforge.run_namespace import EpicRef, RunBinding, RunKey, issue_namespace, write_binding
from autoforge.scheduler_state import Admission, ClaimKind, StateRef, open_claim
from autoforge.state import AutoForgeState, load_state, quarantine_state_file, save_state
from autoforge.transitions import Phase, WorkflowMode
from tests.conftest import (
    BRANCH,
    EPIC,
    ISSUE,
    PR,
    SHA_A,
    FakeGitHub,
    Origin,
    acceptance,
    block,
    ci_check,
    comment_url,
    connect_origin,
    fixed,
    fixer,
    git_repo,
    implement,
    is_shell_probe,
    make_engine,
    publish_pr_head,
    review_result,
    sample_contract,
)


@pytest.fixture(autouse=True)
def _tmp_path_is_a_repository(tmp_path):
    """The CLI locks the repository containing its cwd; tests chdir into tmp_path."""
    git_repo(tmp_path)


@pytest.fixture
def fakes(monkeypatch, tmp_path, tmp_path_factory):
    """Route every engine the CLI builds through FakeGitHub + a scripted agent.

    The CLI's engine works in its cwd, tmp_path (the tests chdir there). One
    bare origin, outside that checkout and seeded with its HEAD as ``main``,
    is the git remote of every engine the CLI builds and the FakeGitHub's
    git data (#161): an ANALYZE_EXECUTE entry fetches from it and the
    controller publishes the agent's commit to it.
    """
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Feature")
    origin = Origin.seeded(tmp_path_factory.mktemp("cli-origin"), git_repo(tmp_path))
    gh.attach_origin(origin)
    holder = {"gh": gh, "origin": origin, "handler": lambda req: ""}

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
        connect_origin(self, origin)

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


def test_a_run_prints_each_outcome_as_its_step_ends_and_progress_on_stderr(
    tmp_path, capsys, monkeypatch, fakes
):
    """#192: the operator sees a step's outcome when it ends, not after the
    loop, and the agent's progress on stderr while it runs; stdout keeps the
    outcomes alone."""
    monkeypatch.chdir(tmp_path)
    while_the_agent_ran = []

    def agent(req):
        while_the_agent_ran.append(capsys.readouterr())
        return implement(req)

    fakes["handler"] = agent
    sd = str(tmp_path / ".autoforge")
    rc = cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "2"])
    assert rc == 0
    (before,) = while_the_agent_ran
    assert "INITIALIZING -> ANALYZE_EXECUTE" in before.out
    assert "] launching analyze_execute (claude, model fable, effort high)" in before.err
    assert before.err.startswith("[00:00:00 ANALYZE_EXECUTE #2] launching ")
    after = capsys.readouterr()
    assert "ANALYZE_EXECUTE -> REVIEW" in after.out
    assert "INITIALIZING -> ANALYZE_EXECUTE" not in after.out  # printed once, as it ended
    assert "ANALYZE_EXECUTE #2] agent exited 0, 0 progress events" in after.err
    assert "launching" not in before.out + after.out
    assert "progress events" not in before.out + after.out


def test_a_step_shows_progress_and_a_dry_run_shows_none(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    fakes["handler"] = implement
    sd = str(tmp_path / ".autoforge")
    assert cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--dry-run"]) == 0
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "step", "--dry-run"]) == 0
    dry = capsys.readouterr()
    assert "launching" not in dry.err and "progress events" not in dry.err
    assert fakes["provider"].calls == []
    assert cli.main(["--state-dir", sd, "step"]) == 0
    real = capsys.readouterr()
    assert "ANALYZE_EXECUTE #2] launching analyze_execute" in real.err
    assert "ANALYZE_EXECUTE -> REVIEW" in real.out and "launching" not in real.out


@pytest.mark.parametrize(
    ("flags", "resume"),
    [
        ([], "autoforge resume"),
        (
            ["--config", "autoforge.json", "--state-dir", "issue state"],
            "autoforge --config autoforge.json --state-dir 'issue state' resume",
        ),
    ],
)
def test_an_agent_failure_prints_a_resume_command_with_the_flags_given(
    tmp_path, capsys, monkeypatch, fakes, flags, resume
):
    """#189: the CLI has no configuration discovery, so the `resume` an agent
    failure suggests repeats the `--config` / `--state-dir` this process was
    started with, as given and quoted, and nothing else; run as printed, it
    continues the same run."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "autoforge.json").write_text('{"version": 1}', encoding="utf-8")
    fakes["provider"].exit_code = 3
    assert cli.main([*flags, "run", "--epic", EPIC, "--issue", ISSUE]) == 1
    err = capsys.readouterr().err
    failed = "autoforge: error: agent 'analyze_execute' exited 3. stderr tail:\nState unchanged.\n"
    assert failed in err
    assert "\n  worktree:  " in err and "(0 uncommitted paths)\n  logs:      " in err
    assert f"\n  resume:    {resume}\n" in err
    fakes["provider"].exit_code = 0
    fakes["handler"] = implement
    assert cli.main([*shlex.split(resume)[1:], "--max-steps", "1"]) == 0
    assert "ANALYZE_EXECUTE -> REVIEW" in capsys.readouterr().out


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


def test_status_describes_a_corrupt_replan_transaction_instead_of_crashing(
    tmp_path, capsys, monkeypatch, fakes
):
    """#35 F6: an unreadable journal field is reported, not raised as a traceback."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    data["phase"] = "REPLAN_REEXECUTE"
    data["replan_transaction"] = {
        "transaction_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "stage": "supersede_intent",
        "source_pr_url": PR,
        "pr_number_watermark": "12",
        "escalation": ["hard_review_round_threshold"],
    }
    state_file.write_text(json.dumps(data))
    assert cli.main(["--state-dir", sd, "status"]) == 0
    out = capsys.readouterr().out
    assert "Replan transaction: a1b2c3d4e5f60718293a4b5c6d7e8f90" in out
    assert "stage:       rejected" in out
    assert "journal:     CORRUPT (" in out
    assert "pr_number_watermark must be an integer" in out
    assert "escalation must be an object" in out
    assert "recorded stage 'supersede_intent'" in out
    # --json prints the journal as persisted, corrupt values included, and
    # says beside it (#67) what status said above: unreadable, rejected, why.
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["replan_transaction"] == data["replan_transaction"]
    assert printed["replan_journal"] == {
        "readable": False,
        "stage": "rejected",
        "defects": [
            "pr_number_watermark must be an integer >= 0, got '12'",
            "escalation must be an object, got list",
        ],
    }
    for defect in printed["replan_journal"]["defects"]:
        assert defect in out


def test_status_json_derives_the_replan_journal_beside_the_raw_one(
    tmp_path, capsys, monkeypatch, fakes
):
    """#67: `replan_journal` says whether the persisted journal is usable, at
    which stage, without a consumer re-implementing the validation; it is
    `null` for a run without a journal and never merged into the raw one."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["replan_transaction"] == {} and printed["replan_journal"] is None

    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    data["phase"] = "REPLAN_REEXECUTE"
    data["current_pr_url"] = PR
    data["replan_transaction"] = {
        "stage": "pending",
        "issue_url": ISSUE,
        "decision_pr_url": PR,
        "decision_head_sha": SHA_A,
        "decision_branch": BRANCH,
        "decision_base_ref": "main",
        "decision_merge_base_sha": "d" * 40,
        "escalation": {"trigger": "hard_review_round_threshold"},
    }
    state_file.write_text(json.dumps(data))
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["replan_transaction"] == data["replan_transaction"]
    assert "replan_journal" not in printed["replan_transaction"]
    assert printed["replan_journal"] == {"readable": True, "stage": "pending", "defects": []}
    assert cli.main(["--state-dir", sd, "status"]) == 0
    out = capsys.readouterr().out
    assert "stage:       pending" in out and "CORRUPT" not in out


def test_status_json_refuses_a_non_object_replan_journal_like_status_does(
    tmp_path, capsys, monkeypatch, fakes
):
    """#67: a journal that is not an object is refused at the state-load
    boundary (`AutoForgeState.from_dict`), the one place `status` and
    `status --json` share before either prints, so --json never emits a
    partial document around it."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    data["replan_transaction"] = ["not", "an", "object"]
    state_file.write_text(json.dumps(data))
    for argv in (["status"], ["status", "--json"]):
        assert cli.main(["--state-dir", sd, *argv]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "replan_transaction" in captured.err


FAKE_SECRET = "ghp_FakeSecretForStatusTest0123456789"


def test_status_redacts_agent_controlled_state_in_every_output(
    tmp_path, capsys, monkeypatch, fakes
):
    """#6 (PR #1 O5): `block_reason` echoes agent text and a hand-edited
    journal can carry anything, so what status prints -- human, --json and
    the terminal-phase line of resume -- crosses the same redaction boundary
    as the run log. The state file itself is left as written."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    data["phase"] = "BLOCKED"
    data["block_reason"] = f"agent said: GITHUB_TOKEN={FAKE_SECRET} leaked"
    # A journal defect quotes the value it could not read.
    data["replan_transaction"] = {"stage": "pending", "transaction_id": FAKE_SECRET}
    raw = json.dumps(data)
    state_file.write_text(raw)

    assert cli.main(["--state-dir", sd, "status"]) == 0
    out = capsys.readouterr().out
    assert FAKE_SECRET not in out
    assert "Reason:     agent said: GITHUB_TOKEN=***REDACTED*** leaked" in out
    assert "journal:     CORRUPT (" in out and "***REDACTED***" in out

    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    printed_text = capsys.readouterr().out
    assert FAKE_SECRET not in printed_text
    printed = json.loads(printed_text)
    assert printed["block_reason"] == "agent said: GITHUB_TOKEN=***REDACTED*** leaked"
    assert printed["replan_transaction"]["transaction_id"] == "***REDACTED***"
    assert printed["replan_journal"]["readable"] is False
    assert FAKE_SECRET not in json.dumps(printed["replan_journal"])

    assert cli.main(["--state-dir", sd, "resume"]) == 1
    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out + captured.err
    assert "terminal phase BLOCKED" in captured.out
    # Output-only: the persisted evidence is not rewritten.
    assert state_file.read_text() == raw


def test_existing_run_refusal_redacts_the_corrupt_state_error(tmp_path, capsys, monkeypatch, fakes):
    """#6: the refusal to start over an unreadable state file quotes the
    `StateError`, and a state error quotes the field it could not read, so a
    hand-edited file can put a credential-shaped value in the message. The
    refusal is printed before `main`'s own handler, so it redacts the line
    itself; the file is neither rewritten nor quarantined."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    data["phase"] = FAKE_SECRET
    raw = json.dumps(data)
    state_file.write_text(raw)

    rc = cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
    captured = capsys.readouterr()
    assert rc == 2 and captured.out == ""
    assert FAKE_SECRET not in captured.err
    assert "unknown phase '***REDACTED***'" in captured.err
    assert "refusing to start a new run over an unreadable state file" in captured.err
    assert "'run --force'" in captured.err
    assert state_file.read_text() == raw
    assert sorted(p.name for p in (tmp_path / ".autoforge").iterdir()) == ["state.json"]


def test_run_terminal_line_and_state_file_both_redact_a_block_reason(
    tmp_path, capsys, monkeypatch, fakes
):
    """#6 / #98: the terminal line of `run` / `resume` is the CLI's own
    boundary and stays in place, but it is no longer the only one: a `_block`
    reason that quotes `gh` output is also redacted by the engine before it
    reaches `state.json`."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    data["current_pr_url"] = PR
    state_file.write_text(json.dumps(data))
    fakes["gh"].get_pr_error = GitHubError(
        f"HTTP 401: Bad credentials (Authorization: Bearer {FAKE_SECRET})"
    )
    assert cli.main(["--state-dir", sd, "resume"]) == 1
    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out + captured.err
    assert "is BLOCKED: state references PR" in captured.out
    assert "Authorization: Bearer ***REDACTED***" in captured.out
    # The engine redacted the reason before persisting it (#98); the CLI's
    # output-side pass is defense in depth on top of that, not the only guard.
    persisted = json.loads(state_file.read_text())
    assert persisted["phase"] == "BLOCKED"
    assert FAKE_SECRET not in persisted["block_reason"]
    assert "Authorization: Bearer ***REDACTED***" in persisted["block_reason"]
    assert FAKE_SECRET not in state_file.read_text()


def test_ready_banner_redacts_state_derived_fields(tmp_path, capsys, monkeypatch, fakes):
    """#6: the READY_FOR_MERGE banner prints state too, so it crosses the same
    boundary as `status`; a hand-edited URL field cannot echo a secret."""
    monkeypatch.chdir(tmp_path)
    _drive_to_ready(fakes)
    sd = str(tmp_path / ".autoforge")
    assert cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE]) == 0
    capsys.readouterr()
    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    assert data["phase"] == "READY_FOR_MERGE"
    data["last_review_comment_url"] = f"{comment_url(PR, 100)}?token={FAKE_SECRET}"
    state_file.write_text(json.dumps(data))
    assert cli.main(["--state-dir", sd, "resume"]) == 0
    out = capsys.readouterr().out
    assert "AutoForge workflow reached READY_FOR_MERGE." in out
    assert FAKE_SECRET not in out and "?token=***REDACTED***" in out


def test_state_derived_run_id_is_redacted_on_every_terminal_line(
    tmp_path, capsys, monkeypatch, fakes
):
    """#6 (PR #97 review): `run_id` names the log directory, so its validation
    checks path safety, not secret shapes -- a credential-shaped value is a
    valid run id. Every line that prints it from state (the step outcome, the
    existing-run refusal, the terminal-phase line of `run` / `step` /
    `resume`) is redacted whole, not field by field."""
    monkeypatch.chdir(tmp_path)
    _drive_to_ready(fakes)
    sd = str(tmp_path / ".autoforge")
    run_argv = ["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    assert cli.main(run_argv) == 0
    capsys.readouterr()
    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    assert data["phase"] not in ("BLOCKED", "FAILED", "DONE")
    data["run_id"] = FAKE_SECRET  # a valid single path component
    state_file.write_text(json.dumps(data))

    # `run` over a live run refuses on stderr, naming the run.
    assert cli.main(run_argv) == 2
    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out + captured.err
    assert "existing run ***REDACTED*** in phase" in captured.err

    # `step` prints the outcome line under the run id.
    assert cli.main(["--state-dir", sd, "step"]) == 0
    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out + captured.err
    assert captured.out.startswith("[***REDACTED***] ") and " -> " in captured.out

    data = json.loads(state_file.read_text())
    data["phase"] = "BLOCKED"
    data["block_reason"] = "stopped"
    state_file.write_text(json.dumps(data))
    assert cli.main(["--state-dir", sd, "resume"]) == 1
    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out + captured.err
    assert "run ***REDACTED*** is in terminal phase BLOCKED: stopped" in captured.out

    data["phase"] = "DONE"
    state_file.write_text(json.dumps(data))
    assert cli.main(["--state-dir", sd, "resume"]) == 0
    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out + captured.err
    assert "workflow already DONE (run ***REDACTED***)" in captured.out


def test_status_json_keeps_journal_entries_whose_redacted_keys_collide(
    tmp_path, capsys, monkeypatch, fakes
):
    """#67 (PR #97 review): `status --json` redacts the whole document, and a
    loadable journal's `escalation` is a free-form mapping, so two keys that
    redact to the same text must both survive -- the raw journal is kept
    beside its verdict as forensic evidence, and an entry silently dropped
    from the rendering would contradict that while `state.json` still holds
    it. The human summary renders the same mapping as text and keeps both."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    second_secret = "ghp_AnotherFakeSecretForStatusTest9876543210"
    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    data["phase"] = "REPLAN_REEXECUTE"
    data["current_pr_url"] = PR
    data["replan_transaction"] = {
        "stage": "pending",
        "issue_url": ISSUE,
        "decision_pr_url": PR,
        "decision_head_sha": SHA_A,
        "decision_branch": BRANCH,
        "decision_base_ref": "main",
        "decision_merge_base_sha": "d" * 40,
        "escalation": {
            "trigger": "hard_review_round_threshold",
            f"GITHUB_TOKEN={FAKE_SECRET}": "first",
            f"GITHUB_TOKEN={second_secret}": "second",
        },
    }
    raw = json.dumps(data)
    state_file.write_text(raw)

    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    printed_text = capsys.readouterr().out
    assert FAKE_SECRET not in printed_text and second_secret not in printed_text
    printed = json.loads(printed_text)
    assert printed["replan_journal"] == {"readable": True, "stage": "pending", "defects": []}
    assert printed["replan_transaction"]["escalation"] == {
        "trigger": "hard_review_round_threshold",
        "GITHUB_TOKEN=***REDACTED***": "first",
        "GITHUB_TOKEN=***REDACTED***#2": "second",
    }

    assert cli.main(["--state-dir", sd, "status"]) == 0
    out = capsys.readouterr().out
    assert FAKE_SECRET not in out and second_secret not in out
    assert '"first"' in out and '"second"' in out
    assert state_file.read_text() == raw


def test_status_and_resume_refuse_an_in_flight_protocol_1_replan(
    tmp_path, capsys, monkeypatch, fakes
):
    """#66 R7-F1: a protocol-1 state with a replan in the close window is a
    protocol incompatibility reported at the boundary -- not a corrupt journal,
    not a resumed close -- and `run` will not replace it without --force."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    data["protocol_version"] = "1"
    data["phase"] = "REPLAN_REEXECUTE"
    data["current_pr_url"] = PR
    data["replan_transaction"] = {
        "transaction_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "stage": "supersede_intent",
        "issue_url": ISSUE,
        "source_pr_url": PR,
        "replacement_pr_url": "https://github.com/owner/repo/pull/43",
        "close_intent_at": "2026-01-01T00:00:00+00:00",
    }
    raw = json.dumps(data)
    state_file.write_text(raw)
    for argv in (["status"], ["resume"]):
        assert cli.main(["--state-dir", sd, *argv]) == 2
        err = capsys.readouterr().err
        assert "protocol_version '1'" in err and "stage 'supersede_intent'" in err
        assert PR in err and "pull/43" in err
        assert "may have done so" in err and "autoforge-replan-close" in err
        assert "controller that wrote it" in err
        assert "corrupt" not in err.lower()
        assert state_file.read_text() == raw
    rc = cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
    err = capsys.readouterr().err
    assert rc == 2 and "--force" in err and "protocol_version '1'" in err
    assert state_file.read_text() == raw
    assert fakes["gh"].closed_prs == [] and fakes["gh"].reopened_prs == []


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
            return implement(req)
        head = gh.prs[PR].head_sha  # the controller-published implementation commit
        return block(review_result(1, head))

    fakes["handler"] = agent
    sd = str(tmp_path / ".autoforge")
    rc = cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE])
    out = capsys.readouterr().out
    assert rc == 0
    assert "AutoForge workflow reached READY_FOR_MERGE." in out
    assert "Automatic merge is disabled in this milestone." in out
    head = fakes["origin"].head("autoforge/2")  # the commit the controller published
    assert head is not None
    assert load_state(tmp_path / ".autoforge" / "state.json").current_head_sha == head
    assert PR in out and ISSUE in out and head in out and "Review round:  1" in out
    assert "Reviewed base: main" in out
    merge_base = fakes["origin"].head("main")  # the implementation commit's parent
    assert f"Reviewed merge base: {merge_base}" in out
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
    status = json.loads(capsys.readouterr().out)
    assert status["phase"] == "READY_FOR_MERGE"
    assert (status["reviewed_pr_url"], status["reviewed_base_ref"]) == (PR, "main")
    assert status["reviewed_merge_base_sha"] == merge_base
    assert status["current_merge_base_sha"] == merge_base
    assert cli.main(["--state-dir", sd, "status"]) == 0
    out = capsys.readouterr().out
    assert f"Reviewed PR:   {PR}" in out and "Reviewed base: main" in out
    assert "Current base:  main" in out
    assert f"Current merge base:  {merge_base}" in out
    assert f"Reviewed merge base: {merge_base}" in out


def _drive_to_ready(fakes):
    """Agent script: ANALYZE_EXECUTE commits (the controller opens the PR), REVIEW is
    clean, UPDATE_EPIC ends."""
    gh = fakes["gh"]

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
        head = gh.prs[PR].head_sha  # the controller-published implementation commit
        return block(review_result(1, head))

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
    gh.prs[PR].checks = [ci_check(state="IN_PROGRESS", conclusion="")]
    assert cli.main(["--config", cfg, "--state-dir", sd, "resume", "--allow-merge"]) == 1
    assert "still running: ci" in capsys.readouterr().err
    gh.prs[PR].checks = [ci_check()]

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
    implemented = fakes["origin"].head("autoforge/2")
    assert implemented is not None
    assert gh.merges == [(PR, "squash", implemented, False)]
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
    # `gh --version` answers "ok 1.0" here: an unreadable version is a FAIL, not a pass.
    assert "[FAIL] gh available: cannot read the gh version" in out
    assert "merge gate: CLOSED: safety.allow_merge=false (built-in default)" in out
    assert cli.main(["--state-dir", str(tmp_path / ".autoforge"), "doctor", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert any(c["name"] == "GitHub remote" and c["ok"] for c in data["checks"])
    gate = next(c for c in data["checks"] if c["name"] == "merge gate")
    assert gate["ok"] and not gate["required"] and gate["detail"].startswith("CLOSED")
    assert not gate["skipped"]
    # `gh api` answers "ok 1.0" here, not JSON: a conclusive read failure is a FAIL.
    rules = next(c for c in data["checks"] if c["name"] == "default branch requires checks")
    assert not rules["ok"] and rules["required"] and not rules["skipped"]


def test_doctor_reports_an_unreadable_branch_rule_as_skipped(tmp_path, capsys, monkeypatch):
    """Issue #41: a token that cannot read rulesets is SKIP, and doctor still exits 0."""
    monkeypatch.chdir(tmp_path)

    def runner(req):
        if is_shell_probe(req):  # the launch preflight's shell startup probe: real shells
            return execute(req)
        argv = req.command
        if argv[:3] == ["git", "remote", "get-url"]:
            return ExecutionResult(argv, req.cwd, 0, "git@github.com:o/r.git\n", "", "t", "t")
        if argv[:3] == ["gh", "repo", "view"]:
            out = '{"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}'
            return ExecutionResult(argv, req.cwd, 0, out, "", "t", "t")
        if argv[:2] == ["gh", "api"]:
            return ExecutionResult(argv, req.cwd, 1, "", "gh: Not Found (HTTP 404)", "t", "t")
        if argv == ["gh", "--version"]:
            return ExecutionResult(
                argv, req.cwd, 0, "gh version 2.48.0 (2024-04-09)\n", "", "t", "t"
            )
        if argv == ["opencode", "--version"]:
            return ExecutionResult(argv, req.cwd, 0, "opencode v2.0.23\n", "", "t", "t")
        if argv == ["claude", "--help"]:  # the flags that keep integrations off (#165)
            out = "  --safe-mode  ...\n  --setting-sources  ...\n  --strict-mcp-config  ...\n"
            return ExecutionResult(argv, req.cwd, 0, out, "", "t", "t")
        if "--show-scope" in argv:  # the launch preflight's config listing: nothing configured
            return ExecutionResult(argv, req.cwd, 0, "", "", "t", "t")
        return ExecutionResult(argv, req.cwd, 0, "ok 1.0\n", "", "t", "t")

    monkeypatch.setattr(cli, "_doctor_runner", lambda: runner)
    assert cli.main(["--state-dir", str(tmp_path / ".autoforge"), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "[SKIP] default branch requires checks: this token cannot read the branch rules" in out
    assert cli.main(["--state-dir", str(tmp_path / ".autoforge"), "doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"]
    rules = next(c for c in data["checks"] if c["name"] == "default branch requires checks")
    assert rules["skipped"] and not rules["ok"] and not rules["required"]


def test_resume_never_resets_the_step_budget(tmp_path, capsys, monkeypatch, fakes):
    """workflow.max_total_steps is enforced on the persisted step_count across resume."""
    monkeypatch.chdir(tmp_path)
    gh = fakes["gh"]
    rounds = {"n": 0}

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW":
            rounds["n"] += 1
            rnd = rounds["n"]
            finding = {
                "id": f"R{rnd}-F1",
                "classification": "nit",
                "title": "t",
                "location": "src/x.py:1",
                "required_resolution": f"different text {rnd}",
            }
            return block(review_result(rnd, gh.prs[PR].head_sha, [finding]))
        return fixer(fixed(f"R{rounds['n']}-F1"))(req)  # the controller pushes it (#163)

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
    assert sorted(p.name for p in sd.iterdir()) == ["state.json"]


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
    assert sorted(p.name for p in sd.iterdir()) == ["state.json"]


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
    assert sorted(p.name for p in sd.iterdir()) == ["state.json"]


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


def test_run_force_moves_corrupt_state_aside_where_it_cannot_be_hard_linked(
    tmp_path, capsys, monkeypatch, fakes
):
    """#30: where link(2) refuses the state file (a filesystem without hard
    links, or protected_hardlinks on a file the operator cannot write) the
    recovery path still works: the archive is a copy under an exclusively
    created name, the original is removed, and the run starts fresh."""
    import errno

    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    raw = '{"phase": "REVIEW", "run_id": '
    (sd / "state.json").write_text(raw, encoding="utf-8")
    real_link = os.link

    def link(src, dst, *args, **kwargs):
        if os.fspath(src) == "state.json":
            raise PermissionError(errno.EPERM, "Operation not permitted", os.fspath(dst))
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", link)
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
    assert quarantined[0].lstat().st_nlink == 1
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
    assert sorted(p.name for p in sd.iterdir()) == ["state.json"]


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


def _other_controller_state(
    run_id: str, phase: Phase = Phase.ANALYZE_EXECUTE, block_reason: str = ""
) -> AutoForgeState:
    return AutoForgeState(
        run_id=run_id,
        repository="owner/repo",
        epic_url=EPIC,
        current_issue_url=ISSUE,
        phase=phase,
        block_reason=block_reason,
        created_at="2026-09-06T00:00:00+00:00",
        updated_at="2026-09-06T00:00:00+00:00",
    )


def _interleave_before_first_lock(monkeypatch, action):
    """Run ``action()`` once, right when the CLI first takes the controller lock.

    Simulates another controller finishing its work in the window between
    the caller's pre-lock view of the state directory and its lock acquisition.
    """
    real_acquire = ControllerLock.acquire
    fired = []

    def racing_acquire(self):
        lock = real_acquire(self)
        if not fired:
            fired.append(True)
            action()
        return lock

    monkeypatch.setattr(ControllerLock, "acquire", racing_acquire)
    return fired


def _second_controller_can_lock(lock_file) -> bool:
    """Whether a second controller could take the repository lock right now."""
    try:
        ControllerLock(lock_file).acquire().release()
    except LockError:
        return False
    return True


def _count_lock_acquisitions(monkeypatch) -> list:
    """Record every *successful* lock acquisition from now on."""
    real_acquire = ControllerLock.acquire
    acquired = []

    def counting_acquire(self):
        lock = real_acquire(self)
        acquired.append(self.lock_path)
        return lock

    monkeypatch.setattr(ControllerLock, "acquire", counting_acquire)
    return acquired


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
    assert sorted(p.name for p in sd.iterdir()) == ["state.json"]


# -- R5-F1: one continuous lock per command ----------------------------------
@pytest.mark.parametrize("command", ["run", "step", "resume"])
def test_command_holds_one_lock_from_state_load_through_agent_execution(
    tmp_path, capsys, monkeypatch, fakes, command
):
    """R5-F1: no lock gap between reading/creating state.json and executing it.

    A second controller must be unable to take the repository lock while the
    agent runs, and the command takes the lock exactly once (no release and
    re-acquire between the first save / load and the engine loop).
    """
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    lock_file = repository_lock_path(tmp_path)
    if command != "run":
        sd.mkdir()
        save_state(_other_controller_state("af-live"), sd / "state.json")
    seen: list[bool] = []

    def agent(req):
        seen.append(_second_controller_can_lock(lock_file))
        return implement(req)

    fakes["handler"] = agent
    acquired = _count_lock_acquisitions(monkeypatch)
    argv = ["--state-dir", str(sd), command]
    if command == "run":
        argv += ["--epic", EPIC, "--issue", ISSUE, "--max-steps", "2"]
    elif command == "resume":
        argv += ["--max-steps", "1"]
    assert cli.main(argv) == 0
    assert seen == [False], "a second controller took the lock while the agent ran"
    assert acquired == [lock_file]
    assert _second_controller_can_lock(lock_file), "lock must be released afterwards"
    assert load_state(sd / "state.json").phase == Phase.REVIEW


def test_step_executes_the_state_seen_under_the_lock_not_a_pre_lock_snapshot(
    tmp_path, capsys, monkeypatch, fakes
):
    """R5-F1: `step` loads state.json under the lock, never before it.

    Another controller finishes the run (DONE) right before this `step` gets
    the lock. The stale ANALYZE_EXECUTE snapshot must not be executed: no
    agent runs and the other controller's state is left as it is.
    """
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    sf = sd / "state.json"
    save_state(_other_controller_state("af-stale"), sf)
    fakes["handler"] = implement
    fired = _interleave_before_first_lock(
        monkeypatch, lambda: save_state(_other_controller_state("af-other", Phase.DONE), sf)
    )
    rc = cli.main(["--state-dir", str(sd), "step"])
    out = capsys.readouterr().out
    assert rc == 0 and fired
    assert "af-other" in out and "already DONE" in out
    assert fakes["provider"].calls == []
    on_disk = load_state(sf)
    assert on_disk.run_id == "af-other" and on_disk.phase == Phase.DONE


def test_resume_executes_the_state_seen_under_the_lock_not_a_pre_lock_snapshot(
    tmp_path, capsys, monkeypatch, fakes
):
    """R5-F1: `resume` loads and decides under the lock, never on a pre-lock snapshot."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    sf = sd / "state.json"
    save_state(_other_controller_state("af-stale"), sf)
    fakes["handler"] = implement
    fired = _interleave_before_first_lock(
        monkeypatch,
        lambda: save_state(_other_controller_state("af-other", Phase.BLOCKED, "human needed"), sf),
    )
    rc = cli.main(["--state-dir", str(sd), "resume", "--max-steps", "5"])
    out = capsys.readouterr().out
    assert rc == 1 and fired
    assert "af-other" in out and "BLOCKED" in out and "human needed" in out
    assert fakes["provider"].calls == []
    on_disk = load_state(sf)
    assert on_disk.run_id == "af-other" and on_disk.phase == Phase.BLOCKED


def test_step_and_resume_dry_run_take_no_lock(tmp_path, capsys, monkeypatch, fakes):
    """Dry-run stays read-only: it must work while another controller holds the lock."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    save_state(_other_controller_state("af-live"), sd / "state.json")
    with ControllerLock(repository_lock_path(tmp_path)):
        assert cli.main(["--state-dir", str(sd), "step", "--dry-run"]) == 0
        assert cli.main(["--state-dir", str(sd), "resume", "--dry-run"]) == 0
    assert "analyze_execute.md" in capsys.readouterr().out
    assert fakes["provider"].calls == []
    assert load_state(sd / "state.json").step_count == 0


# -- non-regular state entries (R6-F2) -----------------------------------------
def _run_with_timeout(fn, seconds: float = 10.0):
    """Run a CLI call in a daemon thread; fail instead of hanging on a FIFO."""
    import threading

    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"CLI call blocked for more than {seconds}s"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_run_refuses_fifo_state_entry_without_force_and_does_not_hang(
    tmp_path, capsys, monkeypatch, fakes
):
    """R6-F2: a FIFO state.json with no writer: exit 2 promptly, entry untouched."""
    import stat

    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    os.mkfifo(sd / "state.json")
    rc = _run_with_timeout(
        lambda: cli.main(
            ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
        )
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "a fifo, not a regular file" in err.lower() and "--force" in err
    assert stat.S_ISFIFO(os.lstat(sd / "state.json").st_mode)
    assert sorted(p.name for p in sd.iterdir()) == ["state.json"]


def test_run_force_moves_fifo_state_entry_aside(tmp_path, capsys, monkeypatch, fakes):
    """R6-F2: run --force archives the FIFO entry itself (never opened) and starts a run."""
    import stat

    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    os.mkfifo(sd / "state.json")
    rc = _run_with_timeout(
        lambda: cli.main(
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
    )
    assert rc == 0
    err = capsys.readouterr().err
    quarantined = [p for p in sd.iterdir() if p.name.startswith("state.json.corrupt-")]
    assert len(quarantined) == 1
    assert stat.S_ISFIFO(os.lstat(quarantined[0]).st_mode)
    assert str(quarantined[0]) in err
    assert stat.S_ISREG(os.lstat(sd / "state.json").st_mode)
    assert cli.main(["--state-dir", str(sd), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "ANALYZE_EXECUTE"


def test_run_force_refuses_directory_state_entry_and_writes_nothing(
    tmp_path, capsys, monkeypatch, fakes
):
    """A directory cannot be archived: even --force fails closed, nothing is written."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    (sd / "state.json").mkdir()
    (sd / "state.json" / "keep").write_text("x", encoding="utf-8")
    argv = ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "a directory, not a regular file" in err and "by hand" in err
    assert cli.main([*argv, "--force"]) == 2
    err = capsys.readouterr().err
    assert "it is a directory" in err and "by hand" in err
    assert (sd / "state.json").is_dir()
    assert (sd / "state.json" / "keep").read_text(encoding="utf-8") == "x"
    assert sorted(p.name for p in sd.iterdir()) == ["state.json"]
    assert fakes["provider"].calls == []


# -- --max-steps validation (R6-F3) --------------------------------------------
@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_run_rejects_non_positive_max_steps_before_writing_state(
    tmp_path, capsys, monkeypatch, fakes, value
):
    """R6-F3: 'run --max-steps 0' is a usage error (exit 2), no traceback, no state file."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    with pytest.raises(SystemExit) as info:
        cli.main(
            ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", value]
        )
    assert info.value.code == 2
    err = capsys.readouterr().err
    assert "--max-steps" in err and "Traceback" not in err
    assert not sd.exists()
    assert fakes["provider"].calls == []


@pytest.mark.parametrize("command", ["run", "resume"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_max_steps_zero_is_rejected_for_run_and_resume_including_dry_run(
    tmp_path, capsys, monkeypatch, fakes, command, dry_run
):
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    argv = ["--state-dir", str(sd), command]
    if command == "run":
        argv += ["--epic", EPIC, "--issue", ISSUE]
    argv += ["--max-steps", "0"]
    if dry_run:
        argv.append("--dry-run")
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2
    assert "must be >= 1" in capsys.readouterr().err
    assert not sd.exists()


# -- lock entry validation (PFR-F1) -------------------------------------------
def _lock_entry(tmp_path):
    """Path of the repository lock entry, its directory created, entry absent."""
    path = repository_lock_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_run_refuses_symlink_lock_entry_and_leaves_target_untouched(
    tmp_path, capsys, monkeypatch, fakes
):
    """A controller.lock symlink is refused with exit 2; its target keeps its bytes."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    target = tmp_path / "unrelated.txt"
    target.write_text("keep\n", encoding="utf-8")
    entry = _lock_entry(tmp_path)
    entry.symlink_to(target)
    rc = cli.main(
        ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "autoforge: error:" in err and "symbolic link" in err
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert entry.is_symlink()
    assert not sd.exists()  # no state.json
    assert fakes["provider"].calls == []


def test_run_refuses_hard_linked_lock_entry_and_leaves_target_untouched(
    tmp_path, capsys, monkeypatch, fakes
):
    """PFR-F2: a hard-linked controller.lock is refused (exit 2); the other name keeps its bytes."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    target = tmp_path / "unrelated.txt"
    target.write_text("preserve\n", encoding="utf-8")
    entry = _lock_entry(tmp_path)
    os.link(target, entry)
    rc = cli.main(
        ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "autoforge: error:" in err and "hard links" in err
    assert target.read_text(encoding="utf-8") == "preserve\n"
    assert os.lstat(entry).st_nlink == 2
    assert not sd.exists()  # no state.json
    assert fakes["provider"].calls == []


def test_run_refuses_fifo_lock_entry_without_traceback_or_hang(
    tmp_path, capsys, monkeypatch, fakes
):
    """A FIFO controller.lock: clean exit 2 (LockError), never io.UnsupportedOperation."""
    import stat

    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    entry = _lock_entry(tmp_path)
    os.mkfifo(entry)
    rc = _run_with_timeout(
        lambda: cli.main(
            ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
        )
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "autoforge: error:" in err and "a fifo, not a regular file" in err.lower()
    assert stat.S_ISFIFO(os.lstat(entry).st_mode)
    assert not sd.exists()
    assert fakes["provider"].calls == []


def test_run_refuses_symlinked_lock_directory_and_leaves_target_untouched(
    tmp_path, capsys, monkeypatch, fakes
):
    """R8-F1: <git dir>/autoforge as a symlink is refused (exit 2); nothing lands in its target."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "note.txt").write_text("keep\n", encoding="utf-8")
    lock_dir = repository_lock_path(tmp_path).parent
    assert not lock_dir.exists()
    lock_dir.symlink_to(elsewhere)
    rc = cli.main(
        ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "autoforge: error:" in err and "is a symbolic link" in err
    assert lock_dir.is_symlink() and os.readlink(lock_dir) == str(elsewhere)
    assert sorted(p.name for p in elsewhere.iterdir()) == ["note.txt"]
    assert (elsewhere / "note.txt").read_text(encoding="utf-8") == "keep\n"
    assert not sd.exists()  # no state.json
    assert fakes["provider"].calls == []


# -- repository-scoped lock (R6-F1) --------------------------------------------
def _run_argv(state_dir=None, *extra) -> list[str]:
    argv = ["--state-dir", str(state_dir)] if state_dir is not None else []
    # Two steps: INITIALIZING, then ANALYZE_EXECUTE (the agent call).
    return [*argv, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "2", *extra]


def test_lock_lives_in_the_git_dir_not_in_the_state_dir(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / "elsewhere" / "state"
    fakes["handler"] = implement
    assert cli.main(_run_argv(sd)) == 0
    lock = repository_lock_path(tmp_path)
    assert lock == (tmp_path / ".git" / "autoforge" / "controller.lock").resolve()
    assert lock.is_file() and lock.read_text(encoding="utf-8") == f"{os.getpid()}\n"
    # No controller.lock; read-context/ holds what each launch was handed (#165).
    assert sorted(p.name for p in sd.iterdir()) == ["logs", "read-context", "state.json"]


def test_second_controller_with_another_state_dir_is_refused_while_the_first_runs(
    tmp_path, capsys, monkeypatch, fakes
):
    """R6-F1: distinct --state-dir values on one checkout contend for one lock.

    While the first controller's agent is running, a second `run` against the
    same repository with a different state directory must exit 2 with
    LockError: no agent, no state.json, nothing written for the second run.
    """
    monkeypatch.chdir(tmp_path)
    first_sd = tmp_path / "state-a"
    second_sd = tmp_path / "state-b"
    outcome: dict = {}

    def agent(req):
        outcome["rc"] = cli.main(_run_argv(second_sd))
        outcome["calls"] = len(fakes["provider"].calls)
        return implement(req)

    fakes["handler"] = agent
    assert cli.main(_run_argv(first_sd)) == 0
    err = capsys.readouterr().err
    assert outcome["rc"] == 2
    assert "another AutoForge controller holds" in err and "same repository" in err
    assert outcome["calls"] == 1, "the second controller must not invoke an agent"
    assert not second_sd.exists()
    assert load_state(first_sd / "state.json").phase == Phase.REVIEW


def test_second_controller_from_a_subdirectory_with_default_state_dir_is_refused(
    tmp_path, capsys, monkeypatch, fakes
):
    """R6-F1: the default relative `.autoforge` from a subdirectory selects the same lock."""
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "pkg" / "deep"
    sub.mkdir(parents=True)
    outcome: dict = {}

    def agent(req):
        monkeypatch.chdir(sub)
        try:
            outcome["rc"] = cli.main(_run_argv())  # no --state-dir: relative .autoforge
        finally:
            monkeypatch.chdir(tmp_path)
        return implement(req)

    fakes["handler"] = agent
    assert cli.main(_run_argv()) == 0
    err = capsys.readouterr().err
    assert outcome["rc"] == 2
    assert "another AutoForge controller holds" in err
    assert not (sub / ".autoforge").exists()
    assert len(fakes["provider"].calls) == 1
    assert load_state(tmp_path / ".autoforge" / "state.json").phase == Phase.REVIEW


def test_second_controller_in_a_linked_worktree_is_refused(tmp_path, capsys, monkeypatch, fakes):
    """R6-F1: a linked `git worktree` shares the repository lock of the main checkout."""
    import subprocess

    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    worktree = tmp_path.parent / f"{tmp_path.name}-wt"
    subprocess.run(["git", "-C", str(tmp_path), "worktree", "add", "-q", str(worktree)], check=True)
    assert repository_lock_path(worktree) == repository_lock_path(tmp_path)
    monkeypatch.chdir(tmp_path)
    outcome: dict = {}

    def agent(req):
        monkeypatch.chdir(worktree)
        try:
            outcome["rc"] = cli.main(_run_argv())
        finally:
            monkeypatch.chdir(tmp_path)
        return implement(req)

    fakes["handler"] = agent
    assert cli.main(_run_argv()) == 0
    assert outcome["rc"] == 2
    assert "another AutoForge controller holds" in capsys.readouterr().err
    assert not (worktree / ".autoforge").exists()
    assert len(fakes["provider"].calls) == 1


@pytest.mark.parametrize("command", ["run", "step", "resume"])
def test_commands_refuse_to_run_outside_a_git_repository(
    tmp_path, capsys, monkeypatch, fakes, command
):
    """No repository identity -> no lock -> exit 2 before any state is written or agent runs."""
    outside = tmp_path.parent / f"{tmp_path.name}-not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    sd = outside / ".autoforge"
    if command != "run":
        sd.mkdir()
        save_state(_other_controller_state("af-live"), sd / "state.json")
    argv = ["--state-dir", str(sd), command]
    if command == "run":
        argv += ["--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    rc = cli.main(argv)
    err = capsys.readouterr().err
    assert rc == 2
    assert "autoforge: error:" in err and "not inside a git repository" in err
    assert "Traceback" not in err
    assert fakes["provider"].calls == []
    if command == "run":
        assert not sd.exists()
    else:
        assert load_state(sd / "state.json").step_count == 0
        assert sorted(p.name for p in sd.iterdir()) == ["state.json"]


def test_dry_run_needs_no_repository_and_spawns_no_git(tmp_path, capsys, monkeypatch, fakes):
    """Dry-run takes no lock, so it neither resolves the repository nor runs git."""
    import autoforge.engine as engine_mod

    outside = tmp_path.parent / f"{tmp_path.name}-not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)

    def never(*a, **k):
        raise AssertionError("dry-run must not resolve the repository lock")

    monkeypatch.setattr(engine_mod, "repository_lock_path", never)
    sd = outside / ".autoforge"
    assert cli.main(_run_argv(sd, "--dry-run")) == 0
    assert "analyze_execute.md" in capsys.readouterr().out
    assert not sd.exists()
    sd.mkdir()
    save_state(_other_controller_state("af-live"), sd / "state.json")
    assert cli.main(["--state-dir", str(sd), "step", "--dry-run"]) == 0
    assert cli.main(["--state-dir", str(sd), "resume", "--dry-run"]) == 0
    assert fakes["provider"].calls == []


# -- a state dir inside the scheduler root (#178, ADR 0008 D17.3) ------------------
def _namespace_state_dir(tmp_path, n: int = 1):
    """``<common>/autoforge/scheduler/issues/<n>/state`` of the tmp_path repository."""
    return tmp_path / ".git" / "autoforge" / "scheduler" / "issues" / str(n) / "state"


def _assert_refused(capsys, fakes, command: str) -> None:
    err = capsys.readouterr().err
    assert f"autoforge: error: '{command}' refuses state directory" in err
    assert "inside the parallel EPIC scheduler's root" in err
    assert "'autoforge epic resume'" in err and "'autoforge epic archive" in err
    assert "Traceback" not in err
    assert fakes["provider"].calls == [] and fakes["gh"].calls == []


@pytest.mark.parametrize("command", ["run", "step", "resume"])
def test_standalone_commands_refuse_a_state_dir_inside_the_scheduler_root(
    tmp_path, capsys, monkeypatch, fakes, command
):
    """Refused before the lock is taken or anything is read or written."""
    monkeypatch.chdir(tmp_path)
    sd = _namespace_state_dir(tmp_path)
    if command != "run":
        sd.mkdir(parents=True)
        save_state(_other_controller_state("af-namespace"), sd / "state.json")
    before = _files(sd) if sd.exists() else None
    argv = ["--state-dir", str(sd), command]
    if command == "run":
        argv += ["--epic", EPIC, "--issue", ISSUE]
    assert cli.main(argv) == 2
    _assert_refused(capsys, fakes, command)
    assert not (tmp_path / ".git" / "autoforge" / "controller.lock").exists()
    if command == "run":
        assert not (tmp_path / ".git" / "autoforge" / "scheduler").exists()
    else:
        assert _files(sd) == before


def test_the_refusal_is_decided_on_the_resolved_path(tmp_path, capsys, monkeypatch, fakes):
    """A relative path, a symbolic link and config.state_dir all reach the namespace."""
    monkeypatch.chdir(tmp_path)
    scheduler = tmp_path / ".git" / "autoforge" / "scheduler"
    scheduler.mkdir(parents=True)
    link = tmp_path.parent / f"{tmp_path.name}-link"
    link.symlink_to(scheduler, target_is_directory=True)
    run = ["run", "--epic", EPIC, "--issue", ISSUE]
    for sd in (".git/autoforge/scheduler/issues/1/state", str(link / "issues" / "1" / "state")):
        assert cli.main(["--state-dir", sd, *run]) == 2
        _assert_refused(capsys, fakes, "run")
    cfg = _config(tmp_path / "autoforge.json", state_dir=".git/autoforge/scheduler/issues/7/state")
    assert cli.main(["--config", cfg, *run]) == 2
    _assert_refused(capsys, fakes, "run")
    assert sorted(p.name for p in scheduler.iterdir()) == []


@pytest.mark.parametrize("command", ["step", "resume"])
def test_a_configured_scheduler_state_dir_is_refused_before_the_run_is_looked_for(
    tmp_path, capsys, monkeypatch, fakes, command
):
    """R1-F1: `config.state_dir` in the scheduler root, a LOCAL run's state in
    LOCAL's location. Looking for the run first would find no `state.json` in
    the namespace, bind the LOCAL one and continue it; the configured directory
    is refused before either is read, whether it is missing, empty or holds a
    state (no "two runs exist" either)."""
    monkeypatch.chdir(tmp_path)
    local = tmp_path / ".git" / "autoforge" / "state"
    local.mkdir(parents=True)
    save_state(
        AutoForgeState(
            run_id="af-local",
            mode=WorkflowMode.LOCAL,
            phase=Phase.REVIEW,
            feature_spec_path="features/add-filter.md",
            feature_spec_sha256="a" * 64,
            local_run_contract=sample_contract(
                repository_root=str(tmp_path), state_root=str(local)
            ),
            workspace_fingerprint="b" * 64,
            base_head_sha="c" * 40,
            base_branch="main",
            created_at="2026-09-06T00:00:00+00:00",
            updated_at="2026-09-06T00:00:00+00:00",
        ),
        local / "state.json",
    )
    sd = _namespace_state_dir(tmp_path)
    cfg = _config(tmp_path / "autoforge.json", state_dir=".git/autoforge/scheduler/issues/1/state")
    local_before = _files(local)

    def holds_a_state() -> None:
        save_state(_other_controller_state("af-namespace"), sd / "state.json")

    for prepare in (lambda: None, lambda: sd.mkdir(parents=True), holds_a_state):
        prepare()
        namespace_before = _files(sd) if sd.exists() else None
        assert cli.main(["--config", cfg, command]) == 2
        _assert_refused(capsys, fakes, command)
        assert _files(local) == local_before
        assert (_files(sd) if sd.exists() else None) == namespace_before
        assert not (tmp_path / ".git" / "autoforge" / "controller.lock").exists()
    assert load_state(local / "state.json").phase == Phase.REVIEW


def test_local_run_refuses_a_state_dir_inside_the_scheduler_root(
    tmp_path, capsys, monkeypatch, fakes
):
    monkeypatch.chdir(tmp_path)
    feature = tmp_path / "features" / "add-filter.md"
    feature.parent.mkdir()
    feature.write_text("# Feature: Add a filter\n", encoding="utf-8")
    sd = _namespace_state_dir(tmp_path)
    assert cli.main(["--state-dir", str(sd), "local", "run", str(feature)]) == 2
    _assert_refused(capsys, fakes, "local run")
    assert not (tmp_path / ".git" / "autoforge" / "scheduler").exists()


def test_status_reads_and_unblock_acts_on_a_namespace_state_dir(
    tmp_path, capsys, monkeypatch, fakes
):
    """`status` reads one; `unblock` takes the lock and edits it, then names the
    scheduler's command to continue, since a standalone `resume` refuses it."""
    monkeypatch.chdir(tmp_path)
    standalone = str(tmp_path / ".autoforge")
    _block_run(tmp_path, capsys, fakes, standalone)
    sd = _namespace_state_dir(tmp_path, 2)
    sd.mkdir(parents=True)
    shutil.copy2(os.path.join(standalone, "state.json"), sd / "state.json")
    provider_calls = len(fakes["provider"].calls)

    assert cli.main(["--state-dir", str(sd), "status"]) == 0
    assert "BLOCKED" in capsys.readouterr().out
    assert cli.main(["--state-dir", str(sd), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "BLOCKED"

    assert cli.main(["--state-dir", str(sd), "unblock", "--reason", "clarified"]) == 0
    out = capsys.readouterr().out
    assert "BLOCKED -> ANALYZE_EXECUTE" in out
    assert "continue with the scheduler: 'autoforge epic resume'" in out
    assert "continue with: autoforge" not in out
    assert load_state(sd / "state.json").phase == Phase.ANALYZE_EXECUTE
    assert load_state(os.path.join(standalone, "state.json")).phase == Phase.BLOCKED

    assert cli.main(["--state-dir", str(sd), "resume"]) == 2
    assert "'resume' refuses state directory" in capsys.readouterr().err
    assert len(fakes["provider"].calls) == provider_calls


def test_a_dry_run_is_not_refused_and_writes_nothing(tmp_path, capsys, monkeypatch, fakes):
    """A dry run takes no lock and runs no git, so it has no coordination root
    to compare with; it writes nothing either way."""
    monkeypatch.chdir(tmp_path)
    sd = _namespace_state_dir(tmp_path)
    assert cli.main(["--state-dir", str(sd), *_run_argv(None, "--dry-run")]) == 0
    assert "analyze_execute.md" in capsys.readouterr().out
    assert not (tmp_path / ".git" / "autoforge").exists()


def test_a_state_dir_beside_the_scheduler_root_is_not_refused(tmp_path, capsys, monkeypatch, fakes):
    """Only the scheduler root: LOCAL's default `<git dir>/autoforge/state` and a
    sibling whose name merely starts with `scheduler` are standalone state dirs."""
    monkeypatch.chdir(tmp_path)
    for sd in (tmp_path / ".git" / "autoforge" / "schedulers", tmp_path / ".autoforge"):
        argv = ["--state-dir", str(sd), "run", "--epic", EPIC, "--issue", ISSUE]
        assert cli.main([*argv, "--max-steps", "1"]) == 0
        capsys.readouterr()
        assert load_state(sd / "state.json").step_count == 1


# -- the forward guard: an issue a scheduler namespace owns (#180, ADR 0008 D17.2) -----
# Holds L0 the way a scheduler does (shared, close-only) until its stdin ends.
_HOLD_L0 = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from autoforge.locking import ControllerLock

with ControllerLock(Path(sys.argv[2]) / "autoforge" / "controller.lock") as lock:
    lock.share()
    print("held", flush=True)
    sys.stdin.read()
"""


def _own_issue_by_state(tmp_path, n: int = 2) -> Path:
    """Scheduler namespace ``n`` holds a state for issue ``n`` (ISSUE is #2)."""
    sd = _namespace_state_dir(tmp_path, n)
    sd.mkdir(parents=True)
    save_state(_other_controller_state(f"af-namespace-{n}"), sd / "state.json")
    return sd


def _assert_owned(capsys, fakes, command: str, reason: str, provider_calls: int = 0) -> None:
    err = capsys.readouterr().err
    assert f"autoforge: error: '{command}' refuses issue {ISSUE}: " in err
    assert reason in err
    assert "'autoforge epic archive <epic> 2' releases it" in err
    assert "Nothing was written." in err and "Traceback" not in err
    assert len(fakes["provider"].calls) == provider_calls


def _own_issue_by_open_claim(namespace) -> None:
    """Namespace #2 holds an open claim (#181) and no state: admitted, not yet started."""
    write_binding(
        namespace,
        RunBinding(
            run_key=RunKey(700_001, 2),
            issue_id=9_002,
            repository_owner="owner",
            repository_name="repo",
            epic=EpicRef(number=1, issue_id=8_001),
            worktree_path="/work/autoforge/worktrees/2",
            created_at="2026-10-18T00:00:00+00:00",
        ),
    )
    with IssueRunLock(namespace) as lock:
        open_claim(
            namespace,
            lock,
            kind=ClaimKind.START,
            invocation_id="0" * 32,
            admission=Admission("a" * 64, "b" * 64, "2026-10-18T00:00:00+00:00"),
            state_ref=StateRef.absent(),
        )


@pytest.mark.parametrize(
    "owned_by", ["state", "corrupt state", "open claim", "live run lock", "unreadable namespace"]
)
def test_run_refuses_an_issue_a_scheduler_namespace_owns(
    tmp_path, capsys, monkeypatch, fakes, owned_by
):
    """Under L0, before a state, a quarantine or a run log is written; fail-closed."""
    monkeypatch.chdir(tmp_path)
    common = Path(os.path.realpath(tmp_path / ".git"))
    namespace = issue_namespace(common, 2)
    lock = None
    if owned_by == "state":
        _own_issue_by_state(tmp_path)
        reason = "holds a state (phase ANALYZE_EXECUTE)"
    elif owned_by == "corrupt state":
        namespace.state_dir.mkdir(parents=True)
        namespace.state_file.write_text("{ not json")
        reason = "holds a state that cannot be read"
    elif owned_by == "open claim":
        _own_issue_by_open_claim(namespace)
        reason = "holds an open scheduler claim (generation 1, a start admission)"
    elif owned_by == "live run lock":
        lock = IssueRunLock(namespace).acquire()
        reason = "a live scheduler worker holds its run lock"
    else:
        namespace.directory.mkdir(parents=True)
        namespace.binding_file.write_text("{ not json")
        reason = "cannot be read"
    before = _files(namespace.directory)
    sd = tmp_path / ".autoforge"
    try:
        assert cli.main(_run_argv(sd)) == 2
    finally:
        if lock is not None:
            lock.release()
    _assert_owned(capsys, fakes, "run", reason)
    assert fakes["gh"].calls == []
    assert not (sd / "state.json").exists() and not (sd / "logs").exists()
    assert _files(namespace.directory) == before
    ControllerLock(repository_lock_path(tmp_path)).acquire().release()  # released


def test_run_force_refuses_an_owned_issue_before_quarantining(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    _own_issue_by_state(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    (sd / "state.json").write_text("{ corrupt")
    before = _files(sd)
    assert cli.main([*_run_argv(sd), "--force"]) == 2
    _assert_owned(capsys, fakes, "run", "holds a state")
    assert _files(sd) == before


@pytest.mark.parametrize("command", ["step", "resume", "unblock"])
def test_continuing_a_standalone_run_on_an_owned_issue_is_refused(
    tmp_path, capsys, monkeypatch, fakes, command
):
    """The standalone state names an issue a namespace has owned since."""
    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    sd.mkdir()
    blocked = command == "unblock"
    state = _other_controller_state(
        "af-standalone",
        Phase.BLOCKED if blocked else Phase.ANALYZE_EXECUTE,
        block_reason="spec unclear" if blocked else "",
    )
    save_state(state, sd / "state.json")
    _own_issue_by_state(tmp_path)
    before = _files(sd)
    argv = ["--state-dir", str(sd), command, *(["--reason", "clarified"] if blocked else [])]
    assert cli.main(argv) == 2
    _assert_owned(capsys, fakes, command, "holds a state")
    assert fakes["gh"].calls == []
    assert _files(sd) == before


def test_unblock_of_a_namespace_state_dir_is_exempt_for_its_own_issue_only(
    tmp_path, capsys, monkeypatch, fakes
):
    monkeypatch.chdir(tmp_path)
    standalone = str(tmp_path / ".autoforge")
    _block_run(tmp_path, capsys, fakes, standalone)
    calls = len(fakes["provider"].calls)
    own = _namespace_state_dir(tmp_path, 2)
    other = _namespace_state_dir(tmp_path, 3)
    for sd in (own, other):
        sd.mkdir(parents=True)
        shutil.copy2(os.path.join(standalone, "state.json"), sd / "state.json")
    unblock = ["unblock", "--reason", "clarified"]

    # Namespace 2 owns issue 2: neither the standalone state nor namespace 3's
    # copy may unblock it.
    for sd in (standalone, str(other)):
        assert cli.main(["--state-dir", sd, *unblock]) == 2
        _assert_owned(capsys, fakes, "unblock", "holds a state (phase BLOCKED)", calls)
        assert load_state(os.path.join(sd, "state.json")).phase == Phase.BLOCKED

    # Its own state directory: exempt.
    assert cli.main(["--state-dir", str(own), *unblock]) == 0
    assert "BLOCKED -> ANALYZE_EXECUTE" in capsys.readouterr().out
    assert load_state(own / "state.json").phase == Phase.ANALYZE_EXECUTE


def test_the_cli_hands_a_standalone_engine_the_ownership_predicate(
    tmp_path, capsys, monkeypatch, fakes
):
    """What `_verify_issue_selectable` applies to a sequential run's next issue."""
    monkeypatch.chdir(tmp_path)
    _own_issue_by_state(tmp_path, 3)
    guarded = []
    real = cli._guard_scheduler_ownership

    def spy(engine, state, command):
        real(engine, state, command)
        guarded.append(engine)

    monkeypatch.setattr(cli, "_guard_scheduler_ownership", spy)
    fakes["handler"] = implement
    assert cli.main(_run_argv(tmp_path / ".autoforge")) == 0
    (engine,) = guarded
    assert engine.issue_ownership is not None
    assert engine.issue_ownership(2) == ""
    assert "holds a state" in engine.issue_ownership(3)


@pytest.mark.parametrize("command", ["run", "unblock"])
def test_a_scheduler_holding_the_controller_lock_excludes_a_standalone_command(
    tmp_path, capsys, monkeypatch, fakes, command
):
    """L0 excludes them in time: a scheduler (or its surviving workers) holds it."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    if command == "unblock":
        _block_run(tmp_path, capsys, fakes, sd)
    before = _files(sd) if os.path.exists(sd) else None
    calls = len(fakes["provider"].calls)
    src = Path(__file__).resolve().parents[1] / "src"
    common = os.path.realpath(tmp_path / ".git")
    scheduler = subprocess.Popen(
        [sys.executable, "-c", _HOLD_L0, str(src), common],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert scheduler.stdout is not None and scheduler.stdout.readline() == "held\n"
        argv = _run_argv(sd) if command == "run" else ["--state-dir", sd, "unblock"]
        assert cli.main(argv + (["--reason", "clarified"] if command == "unblock" else [])) == 2
    finally:
        scheduler.communicate("", timeout=60)
    err = capsys.readouterr().err
    assert "an AutoForge scheduler or its surviving workers may hold it" in err
    assert len(fakes["provider"].calls) == calls
    assert (_files(sd) if os.path.exists(sd) else None) == before


# -- unblock (issue #5) -------------------------------------------------------------
def _block_run(tmp_path, capsys, fakes, sd: str) -> None:
    """A run BLOCKED by ANALYZE_EXECUTE's agent, with nothing on GitHub yet."""
    fakes["handler"] = lambda req: block(
        {"phase": "ANALYZE_EXECUTE", "status": "blocked", "message": "spec unclear"}
    )
    assert cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE]) == 1
    capsys.readouterr()
    fakes["handler"] = lambda req: ""


def test_help_lists_unblock(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "unblock" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["unblock", "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "--reason" in out and "--dry-run" in out and "BLOCKED" in out


def test_unblock_requires_a_reason_argument(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        cli.main(["--state-dir", str(tmp_path / ".autoforge"), "unblock"])
    assert e.value.code == 2
    assert "--reason" in capsys.readouterr().err


def test_unblock_without_state_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["--state-dir", str(tmp_path / ".autoforge"), "unblock", "--reason", "x"])
    assert rc == 2
    assert "no state" in capsys.readouterr().err.lower()


def test_unblock_reenters_a_phase_and_resume_continues(tmp_path, capsys, monkeypatch, fakes):
    """The operator clarified the spec: unblock re-inspects GitHub (issue open,
    no PR yet), re-enters ANALYZE_EXECUTE, and only `resume` runs an agent."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    _block_run(tmp_path, capsys, fakes, sd)
    n_calls = len(fakes["provider"].calls)
    assert cli.main(["--state-dir", sd, "resume"]) == 1
    assert "autoforge unblock --reason" in capsys.readouterr().out
    rc = cli.main(["--state-dir", sd, "unblock", "--reason", "spec clarified in issue #2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BLOCKED -> ANALYZE_EXECUTE" in out
    # With the flags `unblock` was given, so it continues this run (#189).
    assert f"continue with: {shlex.join(['autoforge', '--state-dir', sd, 'resume'])}\n" in out
    assert len(fakes["provider"].calls) == n_calls  # no agent ran
    state = load_state(tmp_path / ".autoforge" / "state.json")
    assert state.phase == Phase.ANALYZE_EXECUTE and state.block_reason == ""
    assert state.unblock_history[-1]["reason"] == "spec clarified in issue #2"
    assert cli.main(["--state-dir", sd, "status"]) == 0
    status = capsys.readouterr().out
    assert "Unblocked:  1 time(s)" in status and "-> ANALYZE_EXECUTE: spec clarified" in status
    assert "Reason:" not in status
    # And the run continues from the re-entered phase.
    fakes["handler"] = lambda req: block(
        {"phase": "ANALYZE_EXECUTE", "status": "blocked", "message": "still unclear"}
    )
    assert cli.main(["--state-dir", sd, "resume"]) == 1
    assert len(fakes["provider"].calls) == n_calls + 1
    assert "still unclear" in capsys.readouterr().out


def test_unblock_refusal_leaves_the_run_blocked_and_exits_1(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    _block_run(tmp_path, capsys, fakes, sd)
    fakes["gh"].add_issue(ISSUE, "Feature", state="CLOSED")
    state_file = tmp_path / ".autoforge" / "state.json"
    raw = state_file.read_text()
    rc = cli.main(["--state-dir", sd, "unblock", "--reason", "trying anyway"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "stays BLOCKED" in out and "CLOSED" in out
    assert "run 'autoforge resume'" not in out
    assert state_file.read_text() == raw


def test_unblock_of_a_run_that_is_not_blocked_exits_2(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "unblock", "--reason", "x"]) == 2
    err = capsys.readouterr().err
    assert "not BLOCKED" in err


def test_unblock_dry_run_previews_and_writes_nothing(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    _block_run(tmp_path, capsys, fakes, sd)
    state_file = tmp_path / ".autoforge" / "state.json"
    raw = state_file.read_text()
    logs = sorted(p.name for p in (tmp_path / ".autoforge" / "logs").rglob("*"))
    rc = cli.main(["--state-dir", sd, "unblock", "--reason", "preview", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run] would re-enter ANALYZE_EXECUTE" in out
    assert state_file.read_text() == raw
    assert sorted(p.name for p in (tmp_path / ".autoforge" / "logs").rglob("*")) == logs
    assert ("get_issue", ISSUE) in fakes["gh"].calls


def test_unblock_output_is_redacted(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    _block_run(tmp_path, capsys, fakes, sd)
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    rc = cli.main(["--state-dir", sd, "unblock", "--reason", f"used {secret}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert secret not in out
    assert secret not in (tmp_path / ".autoforge" / "state.json").read_text()
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    assert secret not in capsys.readouterr().out


def test_unblock_is_refused_while_the_repository_is_locked(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    _block_run(tmp_path, capsys, fakes, sd)
    with ControllerLock(repository_lock_path(tmp_path)):
        rc = cli.main(["--state-dir", sd, "unblock", "--reason", "x"])
    assert rc == 2
    assert "lock" in capsys.readouterr().err.lower()
    assert load_state(tmp_path / ".autoforge" / "state.json").phase == Phase.BLOCKED


def test_dry_run_with_pi_profiles_prints_the_pi_argv_and_spawns_nothing(
    tmp_path, tmp_path_factory, capsys, monkeypatch, fakes
):
    """#133: `run --dry-run` and `step --dry-run` with every profile on Pi
    print the Pi argv of the routed profile and never start `pi`: the fake
    logs every start (RPC child or auth preflight) to a marker file, and the
    real PiProvider is the one routed here (only Claude Code and OpenCode
    are scripted by `fakes`)."""
    from tests.pi_fake import PI_PROFILES, PiFake

    fake = PiFake(tmp_path_factory.mktemp("pi"))
    cfg = tmp_path_factory.mktemp("cfg") / "cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    name: {
                        "provider": "pi",
                        "model": model,
                        "effort": effort,
                        "command": str(fake.command),
                        "extra_args": [],
                        "options": {},
                    }
                    for name, (model, effort) in PI_PROFILES.items()
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    base = ["--config", str(cfg), "--state-dir", sd]
    pi_argv = f"{fake.command} --mode rpc --no-session"
    analyze = "--model openai/pi-analyze --thinking high"

    assert cli.main([*base, "run", "--epic", EPIC, "--issue", ISSUE, "--dry-run"]) == 0
    out = capsys.readouterr().out
    (command,) = [line for line in out.splitlines() if line.startswith("Command:")]
    # Pi takes the prompt on stdin: no argument is the prompt, so none is
    # replaced by the placeholder and the thinking level is shown.
    assert pi_argv in command and command.endswith(analyze)
    assert not os.path.exists(sd)

    assert cli.main([*base, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]) == 0
    capsys.readouterr()
    assert cli.main([*base, "step", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert pi_argv in out and analyze in out
    assert not fake.spawned()
    assert fakes["provider"].calls == []


def test_dry_run_shows_the_prompt_argument_as_a_placeholder(tmp_path, capsys, monkeypatch, fakes):
    """A provider that passes the prompt as an argument (here the scripted
    stand-in for Claude Code) shows that argument as `<prompt>`; the prompt
    itself is previewed below the command."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "step", "--dry-run"]) == 0
    out = capsys.readouterr().out
    (command,) = [line for line in out.splitlines() if line.startswith("Command:")]
    assert command == "Command:  scripted-agent --profile=analyze_execute -- <prompt>"


def test_dry_run_shows_both_agent_limits(tmp_path, capsys, monkeypatch, fakes):
    """#193: the plan names the idle timeout and the maximum runtime the
    agent would run under, the ceiling as unset when it is, and how it is
    watched for a loop (#194)."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert (
        cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"])
        == 0
    )
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "step", "--dry-run"]) == 0
    out = capsys.readouterr().out
    (limits,) = [line for line in out.splitlines() if line.startswith("Limits:")]
    assert limits == "Limits:   idle timeout 900s, max runtime unset"
    assert not any(line.startswith("Timeout:") for line in out.splitlines())
    (loops,) = [line for line in out.splitlines() if line.startswith("Loops:")]
    assert loops == (
        "Loops:    loop detection kill (cycles of up to 4 actions x8, no new action for 1800s, "
        "repeated lines x200)"
    )


# -- acceptance actions (#211) ---------------------------------------------------------
def _blocked_on_acceptance(tmp_path, monkeypatch, fakes) -> tuple[str, str, str]:
    """A run whose FIX plan's acceptance action awaits the operator: (state dir, id, body)."""
    from autoforge.effects import EffectRecord

    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    gh = fakes["gh"]
    eng = make_engine(sd, fixer(acceptance("R1-F1")), github=gh)
    eng.origin = fakes["origin"]
    head = publish_pr_head(eng)
    gh.add_pr(head_sha=head)
    eng.state.phase = Phase.FIX
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = eng.state.reviewed_head_sha = head
    eng.state.review_round = 1
    eng.state.last_review_comment_url = comment_url(PR, 100)
    eng.state.open_findings = [
        {
            "id": "R1-F1",
            "classification": "nit",
            "title": "post the check results",
            "location": "src/x.py:1",
            "required_resolution": "post the results on the issue",
        }
    ]
    assert eng.step().next_phase == "BLOCKED"
    record = EffectRecord.from_dict(load_state(sd / "state.json").effect_records[0])
    eng.close()
    return str(sd), record.action_id, record.body


def test_status_shows_the_acceptance_action_awaiting_approval(tmp_path, capsys, monkeypatch, fakes):
    sd, action, body = _blocked_on_acceptance(tmp_path, monkeypatch, fakes)
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "status"]) == 0
    out = capsys.readouterr().out
    assert f"Acceptance action {action} (FIX, review round 1, finding R1-F1)" in out
    assert f"target:        {ISSUE}" in out
    assert "finding:       R1-F1 post the check results" in out
    assert "authorization: operator approval required (safety.acceptance_actions: approve)" in out
    assert "state:         awaiting approval" in out
    assert f"payload:       {len(body)} chars, sha256 " in out
    assert f"--approve-action {action}" in out and f"--decline-action {action}" in out
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    [entry] = json.loads(capsys.readouterr().out)["acceptance_actions"]
    assert entry["action_id"] == action and entry["state"] == "awaiting_approval"
    assert entry["target"] == ISSUE and entry["pr"] == PR and entry["finding_id"] == "R1-F1"
    assert entry["authorization"] == "operator" and entry["payload_chars"] == len(body)
    assert entry["comment_url"] == ""


def test_status_action_prints_the_exact_payload_bytes(tmp_path, capsys, monkeypatch, fakes):
    """What the operator may post instead of approving: the body, nothing else."""
    sd, action, body = _blocked_on_acceptance(tmp_path, monkeypatch, fakes)
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "status", "--action", action]) == 0
    assert capsys.readouterr().out == body
    assert cli.main(["--state-dir", sd, "status", "--action", "A-" + "0" * 16]) == 1
    assert capsys.readouterr().out == ""
    with pytest.raises(SystemExit) as e:
        cli.main(["--state-dir", sd, "status", "--json", "--action", action])
    assert e.value.code == 2


def test_unblock_approve_action_publishes_on_resume(tmp_path, capsys, monkeypatch, fakes):
    """`unblock --approve-action` records the approval and sends nothing;
    `resume` publishes the comment, pushes, and status shows it verified."""
    sd, action, body = _blocked_on_acceptance(tmp_path, monkeypatch, fakes)
    gh = fakes["gh"]
    capsys.readouterr()
    rc = cli.main(["--state-dir", sd, "unblock", "--approve-action", action, "--reason", "ok"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "BLOCKED -> FIX" in out and gh.effect_writes == []
    assert cli.main(["--state-dir", sd, "status"]) == 0
    assert f"-> FIX [approved {action}]: ok" in capsys.readouterr().out
    assert cli.main(["--state-dir", sd, "resume", "--max-steps", "1"]) == 0
    capsys.readouterr()
    assert [w[:2] for w in gh.effect_writes] == [("create_issue_comment", ISSUE)]
    [comment] = gh.comments[ISSUE]
    assert comment.body == body
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    [entry] = json.loads(capsys.readouterr().out)["acceptance_actions"]
    assert entry["state"] == "verified" and entry["comment_url"] == comment.url


@pytest.mark.parametrize(
    ("argv", "code", "stream", "needle"),
    [
        (["--approve-action", "A-123"], 2, "err", "A-<16 hex digits>"),
        (["--approve-action", "A-" + "0" * 16], 1, "out", "names no acceptance action"),
        (["--decline-action", "A-" + "0" * 16], 1, "out", "names no acceptance action"),
    ],
)
def test_unblock_acceptance_flags_exit_codes(
    tmp_path, capsys, monkeypatch, fakes, argv, code, stream, needle
):
    sd, _, _ = _blocked_on_acceptance(tmp_path, monkeypatch, fakes)
    raw = (tmp_path / ".autoforge" / "state.json").read_text()
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "unblock", *argv, "--reason", "x"]) == code
    assert needle in getattr(capsys.readouterr(), stream)
    assert (tmp_path / ".autoforge" / "state.json").read_text() == raw
    assert fakes["gh"].effect_writes == []


def test_unblock_takes_one_acceptance_flag(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    flags = ["--approve-action", "A-" + "0" * 16, "--decline-action", "A-" + "1" * 16]
    with pytest.raises(SystemExit) as e:
        cli.main(["--state-dir", str(tmp_path / ".autoforge"), "unblock", *flags, "--reason", "x"])
    assert e.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


# -- operator actions (#243) ------------------------------------------------------------
T16 = "https://github.com/owner/repo/issues/16"
T158 = "https://github.com/owner/repo/issues/158"
NOTES = {16: "Coordination note for #16.", 158: "Coordination note for #158."}


def _blocked_on_operator(tmp_path, monkeypatch, fakes) -> tuple[str, dict[str, str]]:
    """A run whose FIX plan awaits two operator actions: (state dir, {target: action id})."""
    from autoforge.effects import EffectRecord

    monkeypatch.chdir(tmp_path)
    sd = tmp_path / ".autoforge"
    gh = fakes["gh"]
    gh.add_issue(ISSUE, "Ship the feature", body="Notes on #16 and #158 are required.")
    gh.add_issue(T16, "Scheduler", state="CLOSED")
    gh.add_issue(T158, "Worker")
    resolution = {
        "finding_id": "R1-F1",
        "resolution": "operator_acceptance",
        "rationale": "The issue requires coordination notes on #16 and #158 posted by the "
        "operator.",
        "operator_acceptance": {
            "actions": [
                {"kind": "related_issue_comment", "issue": n, "body": text}
                for n, text in NOTES.items()
            ]
        },
    }
    eng = make_engine(sd, fixer(resolution), github=gh)
    eng.origin = fakes["origin"]
    head = publish_pr_head(eng)
    gh.add_pr(head_sha=head)
    eng.state.phase = Phase.FIX
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = eng.state.reviewed_head_sha = head
    eng.state.review_round = 1
    eng.state.last_review_comment_url = comment_url(PR, 100)
    eng.state.open_findings = [
        {
            "id": "R1-F1",
            "classification": "bug",
            "title": "notes missing",
            "location": "docs/adr.md:1",
            "required_resolution": "publish the coordination notes",
        }
    ]
    assert eng.step().next_phase == "BLOCKED"
    records = [EffectRecord.from_dict(r) for r in load_state(sd / "state.json").effect_records]
    eng.close()
    return str(sd), {r.target["issue_url"]: r.action_id for r in records[:2]}


def test_status_shows_the_operator_actions_and_their_commands(tmp_path, capsys, monkeypatch, fakes):
    sd, ids = _blocked_on_operator(tmp_path, monkeypatch, fakes)
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "status"]) == 0
    out = capsys.readouterr().out
    assert "Operator actions (FIX, review round 1, finding R1-F1 notes missing)" in out
    for target, action in ids.items():
        assert f"{action}  comment on {target}  pending (not yet verified)" in out
    assert "allowed:   referenced by the run's issue body" in out
    assert f"status --action {ids[T16]} | gh issue comment {T16} --body-file -" in out
    assert f"--bind-evidence {ids[T16]} <comment URL>" in out
    assert f"--decline-action {ids[T16]} --decline-action {ids[T158]}" in out
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)["operator_actions"]
    assert [(e["action_id"], e["state"], e["performer"], e["target"]) for e in entries] == [
        (ids[T16], "pending", "operator", T16),
        (ids[T158], "pending", "operator", T158),
    ]
    assert entries[0]["allowed_by"] == ["issue_body"] and entries[0]["binding"] is None


def test_status_action_prints_an_operator_payload_and_a_bound_state(
    tmp_path, capsys, monkeypatch, fakes
):
    sd, ids = _blocked_on_operator(tmp_path, monkeypatch, fakes)
    gh = fakes["gh"]
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "status", "--action", ids[T158]]) == 0
    body = capsys.readouterr().out
    assert NOTES[158] in body and "ai-operator-comment" in body
    comment = gh.add_comment(T16, 5016, "Existing notes.")
    argv = ["--state-dir", sd, "unblock", "--bind-evidence", ids[T16], comment.url]
    assert cli.main([*argv, "--reason", "bound the existing note"]) == 0
    out = capsys.readouterr().out
    assert f"bound {ids[T16]} {comment.url}" in out
    assert cli.main(["--state-dir", sd, "status", "--json"]) == 0
    [first, _] = json.loads(capsys.readouterr().out)["operator_actions"]
    assert first["state"] == "evidence_bound" and first["binding"]["comment_id"] == 5016


def test_unblock_declines_several_operator_actions_at_once(tmp_path, capsys, monkeypatch, fakes):
    sd, ids = _blocked_on_operator(tmp_path, monkeypatch, fakes)
    capsys.readouterr()
    argv = ["--state-dir", sd, "unblock", "--decline-action", ids[T16]]
    rc = cli.main([*argv, "--decline-action", ids[T158], "--reason", "not needed"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"declined {ids[T16]}, {ids[T158]}" in out
    state = load_state(Path(sd) / "state.json")
    assert [o["outcome"] for o in state.operator_comment_outcomes] == ["declined", "declined"]


@pytest.mark.parametrize(
    ("argv", "code", "stream", "needle"),
    [
        (["--approve-action", "X-" + "0" * 16], 2, "err", "operator-performed: nothing to approve"),
        (["--decline-action", "X-" + "0" * 16], 1, "out", "no operator action of the saved"),
        (
            ["--decline-action", "A-" + "0" * 16, "--decline-action", "X-" + "0" * 16],
            2,
            "err",
            "may repeat for operator action ids only",
        ),
        (["--bind-evidence", "X-123", T16 + "#issuecomment-1"], 2, "err", "X-<16 hex digits>"),
    ],
)
def test_unblock_operator_flags_exit_codes(
    tmp_path, capsys, monkeypatch, fakes, argv, code, stream, needle
):
    sd, _ = _blocked_on_operator(tmp_path, monkeypatch, fakes)
    raw = (tmp_path / ".autoforge" / "state.json").read_text()
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "unblock", *argv, "--reason", "x"]) == code
    assert needle in getattr(capsys.readouterr(), stream)
    assert (tmp_path / ".autoforge" / "state.json").read_text() == raw


def test_status_of_a_namespace_names_the_scheduler_to_continue(
    tmp_path, capsys, monkeypatch, fakes
):
    """A scheduler namespace's run is continued by 'epic resume', which its
    status names in the operator and acceptance hints: a standalone 'resume'
    refuses that state directory (ADR 0008 D17.3)."""
    standalone, ids = _blocked_on_operator(tmp_path, monkeypatch, fakes)
    sd = _namespace_state_dir(tmp_path, 2)
    sd.mkdir(parents=True)
    shutil.copy2(os.path.join(standalone, "state.json"), sd / "state.json")
    capsys.readouterr()
    assert cli.main(["--state-dir", str(sd), "status"]) == 0
    out = capsys.readouterr().out
    assert f"&& autoforge epic resume {EPIC}" in out
    assert f"--state-dir {sd} unblock --decline-action {ids[T16]}" in out
    assert f"--state-dir {sd} resume" not in out


@pytest.mark.parametrize("phase", ["BLOCKED", "FAILED"])
def test_run_refuses_to_replace_a_run_holding_operator_actions(
    tmp_path, capsys, monkeypatch, fakes, phase
):
    """BLOCKED and FAILED keep the FIX plan; a fresh `run` over one holding
    operator actions is refused without --force, naming each action."""
    sd, ids = _blocked_on_operator(tmp_path, monkeypatch, fakes)
    state_file = tmp_path / ".autoforge" / "state.json"
    data = json.loads(state_file.read_text())
    data["phase"] = phase
    state_file.write_text(json.dumps(data))
    raw = state_file.read_text()
    capsys.readouterr()
    run_argv = ["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    assert cli.main(run_argv) == 2
    err = capsys.readouterr().err
    assert "holds operator actions" in err and f"{ids[T16]} on {T16}" in err
    assert "comments stay on GitHub" in err
    assert state_file.read_text() == raw


# -- the recorded configuration (#219) ------------------------------------------------
def _config(path, **sections) -> str:
    """Write a JSON config file at ``path``; its absolute path, as the run records it."""
    path.write_text(json.dumps({"version": 1, **sections}), encoding="utf-8")
    return os.path.abspath(path)


def _files(sd) -> dict[str, bytes]:
    """Every file under the state directory, with its bytes."""
    root = os.path.abspath(sd)
    out = {}
    for base, _, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            with open(path, "rb") as handle:
                out[os.path.relpath(path, root)] = handle.read()
    return out


def _events(sd) -> list[dict]:
    state = load_state(os.path.join(sd, "state.json"))
    path = os.path.join(sd, "logs", state.run_id, "events.jsonl")
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def _default_worktree_root(tmp_path) -> str:
    return os.path.join(os.path.realpath(tmp_path / ".git"), "autoforge", "worktrees")


def test_resume_without_the_config_a_run_started_with_refuses_before_anything_runs(
    tmp_path, tmp_path_factory, capsys, monkeypatch, fakes
):
    """#219: the CLI has no configuration discovery, so a `resume` without the
    `--config` a run was started with would run another model in another,
    empty worktree. The run recorded its configuration when it was created;
    the resume names each change and the command that restores it, and
    refuses before any agent launch, GitHub read or write, or state write."""
    monkeypatch.chdir(tmp_path)
    worktrees = tmp_path_factory.mktemp("worktrees")
    cfg = _config(
        tmp_path / "autoforge.json",
        profiles={"analyze_execute": {"model": "opus"}},
        execution={"worktree_dir": str(worktrees)},
    )
    sd = str(tmp_path / ".autoforge")
    run = ["--config", "autoforge.json", "--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE]
    assert cli.main([*run, "--max-steps", "1"]) == 0
    recorded = load_state(os.path.join(sd, "state.json")).config_fingerprint
    assert recorded["source"] == "created" and recorded["config_file"] == cfg
    assert recorded["worktree_root"] == os.path.realpath(worktrees)
    assert recorded["profiles"]["analyze_execute"]["model"] == "opus"
    capsys.readouterr()
    before, gh_calls = _files(sd), len(fakes["gh"].calls)

    assert cli.main(["--state-dir", sd, "resume"]) == 2
    err = capsys.readouterr().err
    assert '\n  profiles.analyze_execute.model: "opus" -> "fable"\n' in err
    root = _default_worktree_root(tmp_path)
    assert (
        f'\n  agent worktree root (execution.worktree_dir): "{os.path.realpath(worktrees)}" '
        f'-> "{root}"\n'
    ) in err
    assert "'resume' refuses before any phase executes; nothing was written" in err
    restore = shlex.join(["autoforge", "--config", cfg, "--state-dir", sd, "resume"])
    assert f"To continue under the recorded configuration: {restore}\n" in err
    assert "repeat it with --accept-config-change" in err
    assert _files(sd) == before
    assert fakes["provider"].calls == [] and len(fakes["gh"].calls) == gh_calls
    assert fakes["gh"].effect_writes == []

    # Run as printed, the command continues the run on its configuration.
    fakes["handler"] = implement
    assert cli.main([*shlex.split(restore)[1:], "--max-steps", "1"]) == 0
    assert "ANALYZE_EXECUTE -> REVIEW" in capsys.readouterr().out
    [call] = fakes["provider"].calls
    assert call.profile.model == "opus"
    assert os.path.dirname(call.cwd) == os.path.realpath(worktrees)


def test_a_changed_model_refuses_step_naming_the_key_and_the_file_that_restores_it(
    tmp_path, capsys, monkeypatch, fakes
):
    monkeypatch.chdir(tmp_path)
    a = _config(tmp_path / "a.json")
    b = _config(tmp_path / "b.json", profiles={"review_round_1": {"model": "openai/gpt-5.6-sol"}})
    sd = str(tmp_path / ".autoforge")
    assert cli.main(["--config", a, "--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE,
                     "--max-steps", "1"]) == 0  # fmt: skip
    capsys.readouterr()
    before = _files(sd)
    assert cli.main(["--config", b, "--state-dir", sd, "step"]) == 2
    err = capsys.readouterr().err
    changed = [line for line in err.splitlines() if line.startswith("  ")]
    assert changed == [
        '  profiles.review_round_1.model: "openai/gpt-5.6-luna" -> "openai/gpt-5.6-sol"'
    ]
    assert f"was recorded under --config {a} (created " in err
    restore = shlex.join(["autoforge", "--config", a, "--state-dir", sd, "step"])
    assert f"To continue under the recorded configuration: {restore}\n" in err
    # The file the run was recorded from, edited since: no other file to name.
    (tmp_path / "a.json").write_text((tmp_path / "b.json").read_text(), encoding="utf-8")
    assert cli.main(["--config", a, "--state-dir", sd, "step"]) == 2
    err = capsys.readouterr().err
    assert f"This command loads the same file, {a}: it was edited since" in err
    assert _files(sd) == before and fakes["provider"].calls == []


def test_a_changed_worktree_root_refuses_and_a_run_on_the_defaults_restores_without_config(
    tmp_path, tmp_path_factory, capsys, monkeypatch, fakes
):
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE,
                     "--max-steps", "1"]) == 0  # fmt: skip
    capsys.readouterr()
    worktrees = tmp_path_factory.mktemp("worktrees")
    cfg = _config(tmp_path / "autoforge.json", execution={"worktree_dir": str(worktrees)})
    before = _files(sd)
    assert cli.main(["--config", cfg, "--state-dir", sd, "resume"]) == 2
    err = capsys.readouterr().err
    changed = [line for line in err.splitlines() if line.startswith("  ")]
    assert changed == [
        f'  agent worktree root (execution.worktree_dir): "{_default_worktree_root(tmp_path)}" '
        f'-> "{os.path.realpath(worktrees)}"'
    ]
    assert "was recorded under the built-in defaults (created " in err
    restore = shlex.join(["autoforge", "--state-dir", sd, "resume"])
    assert f"To continue under the recorded configuration: {restore}\n" in err
    assert _files(sd) == before and fakes["provider"].calls == []


def test_the_restore_command_names_a_state_dir_this_command_took_from_its_configuration(
    tmp_path, capsys, monkeypatch, fakes
):
    """A command that found the run through its configuration's `state_dir`,
    not a flag, gets a restore command that still names that directory,
    absolute: the recorded configuration (here the built-in defaults) would
    look in `.autoforge`, where there is no run."""
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--state-dir", "custom-state", "run", "--epic", EPIC, "--issue", ISSUE,
                     "--max-steps", "1"]) == 0  # fmt: skip
    cfg = _config(
        tmp_path / "autoforge.json",
        state_dir="custom-state",
        profiles={"analyze_execute": {"model": "opus"}},
    )
    sd = os.path.abspath("custom-state")
    capsys.readouterr()
    before = _files(sd)
    assert cli.main(["--config", cfg, "resume"]) == 2
    err = capsys.readouterr().err
    assert '\n  profiles.analyze_execute.model: "fable" -> "opus"\n' in err
    assert "was recorded under the built-in defaults (created " in err
    restore = shlex.join(["autoforge", "--state-dir", sd, "resume"])
    assert f"To continue under the recorded configuration: {restore}\n" in err
    assert _files(sd) == before and fakes["provider"].calls == []
    # Without the directory, the defaults find no run.
    assert cli.main(["resume"]) == 2
    assert "no state" in capsys.readouterr().err.lower()
    assert not (tmp_path / ".autoforge").exists()

    # Run as printed, the command continues the original run on its configuration.
    fakes["handler"] = implement
    assert cli.main([*shlex.split(restore)[1:], "--max-steps", "1"]) == 0
    assert "ANALYZE_EXECUTE -> REVIEW" in capsys.readouterr().out
    [call] = fakes["provider"].calls
    assert call.profile.model == "fable"
    assert load_state(os.path.join(sd, "state.json")).phase == Phase.REVIEW
    assert not (tmp_path / ".autoforge").exists()


def test_accept_config_change_records_the_change_in_the_run_log_and_continues(
    tmp_path, capsys, monkeypatch, fakes
):
    """Raising a limit after a timeout is a normal reason to resume on a changed
    configuration: the opt-in lets it through, and it is recorded."""
    monkeypatch.chdir(tmp_path)
    a = _config(tmp_path / "a.json")
    raised = _config(
        tmp_path / "raised.json", profiles={"analyze_execute": {"idle_timeout_seconds": 3600}}
    )
    sd = str(tmp_path / ".autoforge")
    assert cli.main(["--config", a, "--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE,
                     "--max-steps", "1"]) == 0  # fmt: skip
    capsys.readouterr()
    assert cli.main(["--config", raised, "--state-dir", sd, "resume"]) == 2
    assert "profiles.analyze_execute.idle_timeout_seconds: 900 -> 3600" in capsys.readouterr().err

    fakes["handler"] = implement
    argv = ["--config", raised, "--state-dir", sd, "resume", "--accept-config-change"]
    assert cli.main([*argv, "--max-steps", "1"]) == 0
    out = capsys.readouterr()
    assert "autoforge: accepted the configuration change of run " in out.err
    assert f"--config {raised} is the run's configuration from now on:" in out.err
    assert "\n  profiles.analyze_execute.idle_timeout_seconds: 900 -> 3600" in out.err
    assert "ANALYZE_EXECUTE -> REVIEW" in out.out
    [call] = fakes["provider"].calls
    assert call.idle_timeout_seconds == 3600
    recorded = load_state(os.path.join(sd, "state.json")).config_fingerprint
    assert recorded["source"] == "accepted" and recorded["config_file"] == raised
    assert recorded["profiles"]["analyze_execute"]["idle_timeout_seconds"] == 3600
    [change] = [e for e in _events(sd) if e["phase"].endswith("-config-change")]
    assert change["phase"] == "ANALYZE_EXECUTE-config-change"
    assert change["metadata"]["command"] == "resume"
    assert change["metadata"]["changes"] == [
        {
            "key": "profiles.analyze_execute.idle_timeout_seconds",
            "recorded": "900",
            "current": "3600",
        }
    ]
    assert change["metadata"]["recorded_config_file"] == a
    # From now on the run's configuration is the accepted one.
    assert cli.main(["--config", raised, "--state-dir", sd, "resume", "--dry-run"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", a, "--state-dir", sd, "resume", "--dry-run"]) == 2
    assert "profiles.analyze_execute.idle_timeout_seconds: 3600 -> 900" in capsys.readouterr().err


def test_resume_dry_run_reports_a_mismatch_and_writes_nothing(tmp_path, capsys, monkeypatch, fakes):
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path / "autoforge.json", profiles={"fix": {"effort": "medium"}})
    sd = str(tmp_path / ".autoforge")
    assert cli.main(["--config", cfg, "--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE,
                     "--max-steps", "1"]) == 0  # fmt: skip
    capsys.readouterr()
    before = _files(sd)
    for command in ("resume", "step"):
        assert cli.main(["--state-dir", sd, command, "--dry-run"]) == 2
        err = capsys.readouterr().err
        assert '\n  profiles.fix.effort: "medium" -> "high"\n' in err
        assert f"'{command}' refuses before any phase executes" in err
        assert cli.main(["--state-dir", sd, command, "--dry-run", "--accept-config-change"]) == 0
        out = capsys.readouterr()
        assert "autoforge: [dry-run] would accept the configuration change of run " in out.err
        assert "ANALYZE_EXECUTE" in out.out and "analyze_execute.md" in out.out
    assert _files(sd) == before
    assert fakes["provider"].calls == [] and fakes["gh"].effect_writes == []


def test_an_old_state_file_without_a_recorded_configuration_loads_and_resumes(
    tmp_path, capsys, monkeypatch, fakes
):
    """A protocol-8 file has no recorded configuration: it is not checked, and
    the resume records the one it runs on, which later commands are held to."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    assert cli.main(["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE,
                     "--max-steps", "1"]) == 0  # fmt: skip
    state_file = os.path.join(sd, "state.json")
    with open(state_file, encoding="utf-8") as handle:
        data = json.load(handle)
    del data["config_fingerprint"]
    for key in (
        "validation_records",
        "validation_attempt",
        "review_evidence",
        "review_certificate",
        "producer_validation_failure",
        "validation_override_seq",
    ):
        del data[key]  # protocol 10's own fields (#172)
    del data["operator_comment_outcomes"]  # protocol 11's own field (#243)
    data["protocol_version"] = "8"
    with open(state_file, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    cfg = _config(tmp_path / "autoforge.json", profiles={"analyze_execute": {"model": "opus"}})
    capsys.readouterr()
    assert cli.main(["--state-dir", sd, "status"]) == 0
    assert "Config:     not recorded (state from before #219; the next step" in (
        capsys.readouterr().out
    )

    fakes["handler"] = implement
    assert cli.main(["--config", cfg, "--state-dir", sd, "resume", "--max-steps", "1"]) == 0
    out = capsys.readouterr()
    assert "had no recorded configuration (its state file predates #219)" in out.err
    assert f"--config {cfg} is recorded as the run's" in out.err
    assert "ANALYZE_EXECUTE -> REVIEW" in out.out
    saved = load_state(state_file)
    assert saved.protocol_version == "11"
    assert saved.config_fingerprint["source"] == "upgraded"
    assert saved.config_fingerprint["profiles"]["analyze_execute"]["model"] == "opus"
    assert cli.main(["--state-dir", sd, "status"]) == 0
    assert f"Config:     --config {cfg} (upgraded " in capsys.readouterr().out
    assert cli.main(["--state-dir", sd, "resume"]) == 2
    assert 'profiles.analyze_execute.model: "opus" -> "fable"' in capsys.readouterr().err


def test_unblock_refuses_another_configuration_and_records_an_accepted_one(
    tmp_path, capsys, monkeypatch, fakes
):
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path / "autoforge.json", profiles={"fix": {"model": "opus"}})
    sd = str(tmp_path / ".autoforge")
    fakes["handler"] = lambda req: block(
        {"phase": "ANALYZE_EXECUTE", "status": "blocked", "message": "spec unclear"}
    )
    assert (
        cli.main(["--config", cfg, "--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE]) == 1
    )
    fakes["handler"] = lambda req: ""
    capsys.readouterr()
    before = _files(sd)
    for dry in ([], ["--dry-run"]):
        assert cli.main(["--state-dir", sd, "unblock", "--reason", "clarified", *dry]) == 2
        err = capsys.readouterr().err
        assert '\n  profiles.fix.model: "opus" -> "fable"\n' in err
        restore = shlex.join(
            ["autoforge", "--config", cfg, "--state-dir", sd, "unblock", "--reason", "<why>"]
        )
        assert f"To continue under the recorded configuration: {restore}\n" in err
    assert _files(sd) == before and fakes["gh"].effect_writes == []

    argv = ["--state-dir", sd, "unblock", "--reason", "clarified", "--accept-config-change"]
    assert cli.main(argv) == 0
    out = capsys.readouterr()
    assert "autoforge: accepted the configuration change of run " in out.err
    assert "BLOCKED -> ANALYZE_EXECUTE" in out.out
    assert f"continue with: {shlex.join(['autoforge', '--state-dir', sd, 'resume'])}" in out.out
    state = load_state(os.path.join(sd, "state.json"))
    assert state.phase == Phase.ANALYZE_EXECUTE
    assert state.config_fingerprint["source"] == "accepted"
    assert state.config_fingerprint["config_file"] == ""
    assert len(state.unblock_history) == 1
    phases = [e["phase"] for e in _events(sd)]
    assert phases.index("BLOCKED-config-change") < phases.index("BLOCKED-unblock")
    assert len(fakes["provider"].calls) == 1  # the run's launch; unblock launches nothing
    assert fakes["gh"].effect_writes == []


def test_run_refuses_a_model_its_recorded_configuration_could_not_hold_before_any_state(
    tmp_path, capsys, monkeypatch, fakes
):
    """R1-F1: the configuration bounds no model, the recorded configuration
    of a run's state file does. `run` and its dry run refuse a model over the
    bound before the run is created, so no state file is written that could
    not be loaded again; at the bound the run records it, and loads again."""
    monkeypatch.chdir(tmp_path)
    sd = str(tmp_path / ".autoforge")
    run = ["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE, "--max-steps", "1"]
    over = _config(tmp_path / "over.json", profiles={"fix": {"model": "m" * 513}})
    for dry in ([], ["--dry-run"]):
        assert cli.main(["--config", over, *run, *dry]) == 2
        err = capsys.readouterr().err
        assert (
            "autoforge: error: profiles.fix.model must be a string of at most 512 characters: "
            "a REMOTE run records it in its state file"
        ) in err
        assert "m" * 513 not in err
    assert _files(sd) == {}
    assert fakes["provider"].calls == [] and fakes["gh"].effect_writes == []

    at = _config(tmp_path / "at.json", profiles={"fix": {"model": "m" * 512}})
    assert cli.main(["--config", at, *run]) == 0
    recorded = load_state(os.path.join(sd, "state.json")).config_fingerprint
    assert recorded["profiles"]["fix"]["model"] == "m" * 512
    capsys.readouterr()
    assert cli.main(["--config", at, "--state-dir", sd, "status"]) == 0
    assert f"Config:     --config {at} (created " in capsys.readouterr().out
    assert cli.main(["--config", at, "--state-dir", sd, "resume", "--dry-run"]) == 0


def test_a_host_that_cannot_enforce_a_required_sandbox_refuses_step_with_exit_2(
    tmp_path, capsys, monkeypatch, fakes, sandbox_probe
):
    """#169 (ADR 0005 D12.2): the pre-launch gate's refusal is a configuration
    error at the CLI; it names the capability and the `disabled` alternative,
    and the run's files are unchanged. `status` names the mode without a probe."""
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path / "autoforge.json", sandbox={"mode": "required"})
    sd = str(tmp_path / ".autoforge")
    run = ["--config", cfg, "--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE]
    assert cli.main([*run, "--max-steps", "1"]) == 0  # INITIALIZING launches nothing
    sandbox_probe.unsupported(reason="bwrap: setting up uid map: Permission denied")
    capsys.readouterr()
    before = _files(sd)
    assert cli.main(["--config", cfg, "--state-dir", sd, "step"]) == 2
    err = capsys.readouterr().err
    assert "autoforge: error:" in err and "user namespace" in err
    assert "uid map: Permission denied" in err
    assert "sandbox.mode: disabled" in err and "logical isolation only" in err
    assert _files(sd) == before
    assert fakes["provider"].calls == [] and fakes["gh"].effect_writes == []
    assert cli.main(["--config", cfg, "--state-dir", sd, "status"]) == 0
    out = capsys.readouterr().out
    assert "Outer sandbox:\n  required (sandbox.mode)" in out
    assert sandbox_probe.calls == 1


def test_the_scheduler_worker_entry_point_is_hidden_and_routed(monkeypatch, capsys):
    """``epic _worker`` (#182) is the scheduler's, not an operator's: no parser, no help."""
    import autoforge.worker as worker_module

    seen: list[list[str]] = []

    def run_worker(argv):
        seen.append(list(argv))
        return 7

    monkeypatch.setattr(worker_module, "run_worker", run_worker)
    assert cli.main(["epic", "_worker", "--issue", "2"]) == 7
    assert seen == [["--issue", "2"]]
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "_worker" not in capsys.readouterr().out
