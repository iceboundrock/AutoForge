"""Run logging: layout, redaction, and the paths it refuses to write through.

The log tree lives inside the operator's checkout, where the agents the
controller launches also write, so nothing under it is trusted by name.
"""

import os

import pytest

from autoforge.errors import StateError
from autoforge.runlog import ExecutionRecord, RunLogger, validate_run_id


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


# -- R3-F1: the log tree is never followed anywhere ---------------------------
def test_a_logs_symlink_never_redirects_controller_writes(tmp_path):
    """A `logs` symlink put every artifact of the run outside the checkout.

    `RunLogger` created the run directory with `mkdir(parents=True)` and then
    wrote through whatever that resolved to. A LOCAL run promises to touch
    nothing outside the working tree, so the link has to fail the write
    rather than silently relocate it.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "logs").symlink_to(outside)

    with pytest.raises(StateError, match="symbolic link"):
        RunLogger(state_dir / "logs", "run-1")
    assert list(outside.iterdir()) == []


def test_a_fifo_event_log_fails_instead_of_hanging_the_controller(tmp_path):
    """`open()` on a FIFO with no writer blocks forever; the controller must not.

    `_existing_event_count` opened `events.jsonl` to resume the sequence
    counter, so a FIFO left at that path stalled every invocation before the
    first agent ran, with no error and no timeout.
    """
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    os.mkfifo(run_dir / "events.jsonl")
    with pytest.raises(StateError, match="FIFO"):
        RunLogger(tmp_path / "logs", "run-1")


def test_a_pre_existing_symlinked_step_artifact_is_refused(tmp_path):
    """Each artifact is opened with O_NOFOLLOW, not just the directories above it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    step = tmp_path / "logs" / "run-1" / "001-review-1"
    step.mkdir(parents=True)
    (step / "stdout.log").symlink_to(outside / "leak.log")

    log = RunLogger(tmp_path / "logs", "run-1")
    with pytest.raises(StateError, match="symbolic link"):
        log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"), stdout="out")
    assert not (outside / "leak.log").exists()


def test_a_directory_where_an_artifact_belongs_is_refused(tmp_path):
    log = RunLogger(tmp_path / "logs", "run-1")
    (tmp_path / "logs" / "run-1" / "001-review-1").mkdir(parents=True)
    (tmp_path / "logs" / "run-1" / "001-review-1" / "prompt.md").mkdir()
    with pytest.raises(StateError, match="not a regular file"):
        log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"), prompt="p")


# -- R3-F2: a run id is a file name, not a path -------------------------------
@pytest.mark.parametrize(
    "run_id",
    ["../escape", "..", ".", "a/b", "/absolute", "sub\\dir", "", ".hidden", "nul\x00byte"],
)
def test_run_id_must_be_a_single_safe_path_component(run_id, tmp_path):
    """`logs/<run_id>` is a write path: "../escape" moved the whole run's logs."""
    with pytest.raises(StateError, match="invalid run_id"):
        validate_run_id(run_id)
    with pytest.raises(StateError, match="invalid run_id"):
        RunLogger(tmp_path / "logs", run_id)
    assert not (tmp_path / "escape").exists()


def test_a_generated_run_id_is_accepted(tmp_path):
    from autoforge.engine import generate_run_id

    run_id = generate_run_id()
    assert validate_run_id(run_id) == run_id
    log = RunLogger(tmp_path / "logs", run_id)
    assert log.run_dir.parent == tmp_path / "logs"
