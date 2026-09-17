"""Run logging: layout, redaction, and the paths it refuses to write through.

The log tree lives inside the operator's checkout, where the agents the
controller launches also write, so nothing under it is trusted by name.
"""

import json
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


def test_runlog_recovers_sequence_from_record_and_step_names(tmp_path):
    log_dir = tmp_path / "logs" / "run-1"
    log_dir.mkdir(parents=True)
    (log_dir / "events.jsonl").write_text('{"seq": 7}\n', encoding="utf-8")
    (log_dir / "011-review-1").mkdir()

    log = RunLogger(tmp_path / "logs", "run-1")
    step = log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX"))
    assert step.name == "012-fix-1"


def test_runlog_rejects_a_corrupt_event_journal(tmp_path):
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text("not-json\n", encoding="utf-8")
    with pytest.raises(StateError, match="corrupted event journal"):
        RunLogger(tmp_path / "logs", "run-1")


# -- R11-F2: recovery reads the journal bounded, never wholesale ---------------
#
# The journal lives where the agents write (same OS user), and recovery
# needs it only for the highest seq it holds. Like ``state.json`` (R10-F3)
# it is read through ``SafeRoot.read_bytes(limit=)``: an oversized or sparse
# file is refused as corrupt after at most one byte past the budget.


def _counted_reads(monkeypatch) -> list[int]:
    """Record every ``read(n)`` the bounded reader asks a file object for."""
    import autoforge.safefs as safefs

    asked: list[int] = []
    real_fdopen = safefs.os.fdopen

    def fdopen_with_counted_reads(fd, *args, **kwargs):
        fh = real_fdopen(fd, *args, **kwargs)
        real_read = fh.read

        def read(n=-1):
            asked.append(n)
            return real_read(n)

        fh.read = read  # type: ignore[method-assign]
        return fh

    monkeypatch.setattr(safefs.os, "fdopen", fdopen_with_counted_reads)
    return asked


def test_an_oversized_event_journal_is_refused_without_being_read_wholesale(tmp_path, monkeypatch):
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    journal = run_dir / "events.jsonl"
    journal.write_text('{"seq": 1}\n', encoding="utf-8")
    # A sparse file: costs nothing to create, would cost the whole apparent
    # size to read() -- and a same-user agent can leave one at this name.
    os.truncate(journal, 16 * MAX_EVENT_JOURNAL_BYTES)
    asked = _counted_reads(monkeypatch)
    with pytest.raises(StateError, match="corrupted event journal.*larger than .* bytes"):
        RunLogger(tmp_path / "logs", "run-1")
    assert asked and max(asked) == MAX_EVENT_JOURNAL_BYTES + 1, asked
    assert journal.stat().st_size == 16 * MAX_EVENT_JOURNAL_BYTES, "recovery never writes"


def test_a_journal_at_the_byte_budget_still_recovers_the_sequence(tmp_path):
    """The bound is a ceiling on what is read, not on what is valid."""
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    body = b'{"seq": 7}\n'
    (run_dir / "events.jsonl").write_bytes(body + b" " * (MAX_EVENT_JOURNAL_BYTES - len(body)))
    log = RunLogger(tmp_path / "logs", "run-1")
    assert log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX")).name == (
        "008-fix-1"
    )


def test_a_journal_with_too_many_records_is_refused_before_any_line_is_parsed(
    tmp_path, monkeypatch
):
    """Records are counted on the bytes: a journal of a million empty lines
    is refused before it is split into a million objects."""
    import autoforge.runlog as runlog

    monkeypatch.setattr(runlog, "MAX_EVENT_JOURNAL_RECORDS", 3)
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    # Four records, the last one unterminated and not JSON: the count is what
    # refuses it, so the parser never gets to complain about the last line.
    (run_dir / "events.jsonl").write_bytes(b'{"seq": 1}\n\n\nnot-json')
    with pytest.raises(StateError, match="corrupted event journal.*more than 3 records"):
        RunLogger(tmp_path / "logs", "run-1")
    # Exactly the budget, unterminated last line included, loads.
    (run_dir / "events.jsonl").write_bytes(b'{"seq": 1}\n\n{"seq": 5}')
    log = RunLogger(tmp_path / "logs", "run-1")
    assert log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX")).name == (
        "006-fix-1"
    )


def test_the_journal_refusal_names_the_manual_step(tmp_path, monkeypatch):
    """Refusing recovery must not strand the run: the message says what to
    move aside, and the step directories keep the sequence monotonic."""
    import autoforge.runlog as runlog

    monkeypatch.setattr(runlog, "MAX_EVENT_JOURNAL_RECORDS", 1)
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_bytes(b'{"seq": 1}\n{"seq": 2}\n')
    (run_dir / "002-review-1").mkdir()
    with pytest.raises(StateError, match="move logs/run-1/events.jsonl aside"):
        RunLogger(tmp_path / "logs", "run-1")
    (run_dir / "events.jsonl").rename(run_dir / "events.jsonl.aside")
    log = RunLogger(tmp_path / "logs", "run-1")
    assert log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX")).name == (
        "003-fix-1"
    )


def test_a_journal_that_is_not_utf8_is_corrupt_not_silently_repaired(tmp_path):
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_bytes(b'{"seq": 1}\n\xff\xfe\n')
    with pytest.raises(StateError, match="corrupted event journal.*line 2"):
        RunLogger(tmp_path / "logs", "run-1")


# -- #55: the post-agent append is the other read of the journal -----------------
#
# R11-F2 bounded the read at recovery and moved it before the launch. The
# append that records the invocation reads the journal again *after* the
# agent returned, which is exactly when an agent has had the chance to
# enlarge it. That read is bounded the same way and refused the same way.


def test_a_journal_enlarged_after_the_logger_opened_refuses_the_append_not_the_machine(
    tmp_path, monkeypatch
):
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    log = RunLogger(tmp_path / "logs", "run-1")
    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"))
    # The agent runs here, and leaves the journal sparse and twice the budget.
    os.truncate(log.events_path, 2 * MAX_EVENT_JOURNAL_BYTES)
    before = log.events_path.stat()
    asked = _counted_reads(monkeypatch)

    with pytest.raises(StateError, match="corrupted event journal.*larger than .* bytes") as exc:
        log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX"), stdout="work")
    assert max(asked) == MAX_EVENT_JOURNAL_BYTES + 1, asked
    # Refused before anything was written: the oversized file is neither
    # materialised nor carried forward into a fresh inode ...
    after = log.events_path.stat()
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)
    # ... and the invocation is not lost: its artifacts were published
    # first, the message says where they are, and the sequence continues
    # from the directory name once the journal is moved aside.
    step = log.run_dir / "002-fix-1"
    assert (step / "stdout.log").read_text(encoding="utf-8") == "work"
    assert "logs/run-1/002-fix-1/" in str(exc.value)
    assert "move logs/run-1/events.jsonl aside" in str(exc.value)
    log.events_path.rename(log.events_path.with_suffix(".aside"))
    assert (
        RunLogger(tmp_path / "logs", "run-1")
        .log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"))
        .name
        == "003-review-1"
    )


def test_a_journal_at_exactly_the_byte_budget_is_still_appended_to(tmp_path):
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    log = RunLogger(tmp_path / "logs", "run-1")
    body = b'{"seq": 1}\n'
    log.events_path.write_bytes(body + b" " * (MAX_EVENT_JOURNAL_BYTES - len(body)))
    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX"))
    journal = log.events_path.read_bytes()
    assert journal.startswith(body)
    assert len(journal) > MAX_EVENT_JOURNAL_BYTES
    assert json.loads(journal.splitlines()[-1])["seq"] == 1


# -- #56: the crash guard lists the run's own directory, bounded ------------------
#
# The step-directory scan used to walk ``logs/`` and skip every sibling run,
# which still listed and stat'ed all of them first. It now opens the run's
# directory as a sub-root and lists that alone, with a budget.


def _recorded_listings(monkeypatch) -> list[str]:
    """Every directory entry name any ``scandir`` in ``safefs`` produces."""
    import autoforge.safefs as safefs

    seen: list[str] = []
    real_scandir = safefs.os.scandir

    class Recording:
        def __init__(self, it):
            self._it = it

        def __enter__(self):
            self._it.__enter__()
            return self

        def __exit__(self, *exc):
            return self._it.__exit__(*exc)

        def __iter__(self):
            for entry in self._it:
                seen.append(entry.name)
                yield entry

    monkeypatch.setattr(safefs.os, "scandir", lambda *a, **k: Recording(real_scandir(*a, **k)))
    return seen


def test_opening_the_logger_never_lists_sibling_runs(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    run_dir = logs / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text('{"seq": 3}\n', encoding="utf-8")
    (run_dir / "005-review-1").mkdir()
    (run_dir / "005-review-1" / "stdout.log").write_text("not a step name", encoding="utf-8")
    for i in range(2000):
        (logs / f"sibling-{i:04}").mkdir()
        (logs / f"planted-{i:04}").write_text("x", encoding="utf-8")
    seen = _recorded_listings(monkeypatch)

    log = RunLogger(logs, "run-1")

    assert sorted(seen) == ["005-review-1", "events.jsonl"], "only the run's own top level"
    assert log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX")).name == (
        "006-fix-1"
    )


def test_a_run_directory_with_more_entries_than_a_run_can_produce_is_refused_while_listing(
    tmp_path, monkeypatch
):
    import autoforge.runlog as runlog

    monkeypatch.setattr(runlog, "MAX_RUN_LOG_ENTRIES", 8)
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text('{"seq": 1}\n', encoding="utf-8")
    planted = [run_dir / f"planted-{i:03}" for i in range(100)]
    for path in planted:
        path.mkdir()
    seen = _recorded_listings(monkeypatch)
    with pytest.raises(StateError, match="corrupted run log directory.*more than 8 entries") as exc:
        RunLogger(tmp_path / "logs", "run-1")
    # Refused on the readdir record that passes the budget, before the rest
    # of the directory is listed, sorted or stat'ed.
    assert len(seen) == 9, seen
    assert "move the entries AutoForge did not create out of logs/run-1/" in str(exc.value)
    # The manual step works: within the budget again, the sequence resumes.
    for path in planted[7:]:
        path.rmdir()
    log = RunLogger(tmp_path / "logs", "run-1")
    assert log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX")).name == (
        "002-fix-1"
    )


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


def test_a_symlinked_step_artifact_is_replaced_not_written_through(tmp_path):
    """The invariant is where the bytes land, not which exception is raised.

    A whole-file artifact is created as a fresh temporary in the artifact's
    own directory and renamed over the name, so a symbolic link planted at
    that name is *replaced* -- the link's target is never opened, never
    created and never written. Asserting a refusal here would be asserting an
    implementation detail; asserting that the outside file is untouched is
    asserting the guarantee.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    log = RunLogger(tmp_path / "logs", "run-1")
    step = tmp_path / "logs" / "run-1" / "001-review-1"
    step.mkdir(parents=True)
    (step / "stdout.log").symlink_to(outside / "leak.log")
    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"), stdout="out")

    assert not (outside / "leak.log").exists(), "the link target must never be created"
    assert list(outside.iterdir()) == []
    written = step / "stdout.log"
    assert not written.is_symlink(), "the name now holds the controller's own file"
    assert written.read_text(encoding="utf-8") == "out"


def test_a_symlinked_events_journal_is_refused_rather_than_appended_through(tmp_path):
    """The journal is the one artifact that cannot be written by replacement.

    `events.jsonl` is appended to, so there is no fresh temporary to rename
    over the name: the existing entry must be opened. That open is
    `O_NOFOLLOW`, so a link there is a refusal -- and the refusal, not a
    replacement, is what keeps the target untouched.
    """
    outside = tmp_path / "notes.txt"
    outside.write_text("mine\n", encoding="utf-8")
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").symlink_to(outside)

    # The counter resume reads the journal, so the refusal arrives at
    # construction -- before any agent runs, which is the right moment for it.
    with pytest.raises(StateError, match="symbolic link"):
        RunLogger(tmp_path / "logs", "run-1")
    assert outside.read_text(encoding="utf-8") == "mine\n"


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


# -- PR #44 R4-F3: the journal serialises the whole record ---------------------
def test_the_event_journal_never_carries_an_unredacted_error(tmp_path):
    """`events.jsonl` is `asdict(record)`, so redacting at each write site missed it.

    `execution.json` and `error.txt` were each redacted where they were
    written; the journal line was not, and a controller-side error quotes
    agent output and failing command lines. The record itself is redacted
    now, so every persistence path gets the same text.
    """
    secret = "ghp_" + "a" * 36
    log = RunLogger(tmp_path / "logs", "run-1")
    step = log.log_execution(
        ExecutionRecord(
            run_id="run-1",
            seq=0,
            phase="FIX",
            error=f"agent failed: GITHUB_TOKEN={secret} rejected",
        )
    )
    journal = (log.events_path).read_text(encoding="utf-8")
    assert secret not in journal
    assert "REDACTED" in journal
    for name in ("execution.json", "error.txt"):
        assert secret not in (step / name).read_text(encoding="utf-8")


def test_the_event_journal_and_request_never_carry_an_unredacted_metadata_value(tmp_path):
    """Issue #37 F5: `metadata` is persisted raw into `request.json` and the journal.

    `metadata` is free-form and carries whatever the caller attached -- a
    failing command line, a redirect URL with a token in it -- so it gets
    the same redaction as `error`, and every persistence path (the request
    file, `execution.json`, the events journal) must show the redacted text.
    """
    secret = "ghp_" + "b" * 36
    log = RunLogger(tmp_path / "logs", "run-1")
    step = log.log_execution(
        ExecutionRecord(
            run_id="run-1",
            seq=0,
            phase="MERGE",
            metadata={
                "command": f"gh api -H 'Authorization: Bearer {secret}' /repos/o/r",
                "nested": {"env": [f"GH_TOKEN={secret}"]},
            },
        )
    )
    journal = log.events_path.read_text(encoding="utf-8")
    assert secret not in journal
    assert "REDACTED" in journal
    request = (step / "request.json").read_text(encoding="utf-8")
    assert secret not in request
    assert "REDACTED" in request
    assert secret not in (step / "execution.json").read_text(encoding="utf-8")


def test_a_write_never_lands_on_a_hard_link_and_never_truncates_first(tmp_path):
    """PR #44 R4-F1: a hard link is a regular file by every other test.

    `O_TRUNC` was handed to `os.open`, so the kernel emptied the linked file
    *before* anything could look at the descriptor: the refusal arrived after
    the damage was done. Replacement removes the question -- the target inode
    is never opened at all, so the second name keeps both its content and its
    identity, and the controller still gets its artifact.
    """
    outside = tmp_path / "precious.txt"
    outside.write_text("do not lose me\n", encoding="utf-8")
    before = outside.stat()
    log = RunLogger(tmp_path / "logs", "run-1")
    step_dir = log.run_dir / "001-review-1"
    os.makedirs(step_dir, exist_ok=True)
    os.link(outside, step_dir / "stdout.log")

    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"), stdout="x")

    assert outside.read_text(encoding="utf-8") == "do not lose me\n"
    assert outside.stat().st_ino == before.st_ino
    assert outside.stat().st_size == before.st_size
    artifact = step_dir / "stdout.log"
    assert artifact.read_text(encoding="utf-8") == "x"
    assert artifact.stat().st_ino != before.st_ino, "a new inode, not the linked one"
    assert artifact.stat().st_nlink == 1


def test_a_hard_linked_events_journal_is_replaced_before_it_is_appended_to(tmp_path):
    outside = tmp_path / "notes.txt"
    outside.write_text("mine\n", encoding="utf-8")
    log = RunLogger(tmp_path / "logs", "run-1")
    os.link(outside, log.events_path)
    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"))
    assert outside.read_text(encoding="utf-8") == "mine\n"
    assert log.events_path.stat().st_nlink == 1


def test_a_rewritten_artifact_never_keeps_a_tail_of_the_old_one(tmp_path):
    """Replacing rather than truncating must still leave exactly the new bytes."""
    from autoforge.safefs import SafeRoot

    with SafeRoot.open(tmp_path) as root:
        root.write_text("artifact.log", "a long first line that must not survive\n")
        root.write_text("artifact.log", "short\n")
        assert root.read_text("artifact.log") == "short\n"
