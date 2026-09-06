from autoforge.runlog import ExecutionRecord, RunLogger


def test_runlog_layout_and_redaction(tmp_path):
    log = RunLogger(tmp_path / "logs", "run-1")
    rec = ExecutionRecord(
        run_id="run-1",
        seq=0,
        phase="REVIEW",
        attempt=1,
        provider="opencode",
        model="openai/gpt-5.6-luna",
        effort="high",
        prompt_version="v1",
        command=["opencode", "run", "--token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "p"],
        timeout_seconds=10,
        exit_code=0,
        parsed_result={"phase": "REVIEW", "status": "success"},
    )
    d = log.log_execution(
        rec,
        prompt="secret GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        stdout="out",
        stderr="err",
    )
    assert d.name == "001-review-1"
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in (d / "prompt.md").read_text()
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in (d / "request.json").read_text()
    assert (d / "control-result.json").exists()
    # second logger instance continues the sequence
    log2 = RunLogger(tmp_path / "logs", "run-1")
    d2 = log2.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX", attempt=2))
    assert d2.name == "002-fix-2"
