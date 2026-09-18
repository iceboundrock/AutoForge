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


def test_runlog_recovers_sequence_from_step_names_never_from_the_journal(tmp_path):
    """#51: the journal is write-only for the controller. The step sequence
    comes from the directory names, which are published before the journal
    line and so are never behind it; whatever the journal says -- a higher
    seq, no JSON at all -- is neither believed nor looked at."""
    log_dir = tmp_path / "logs" / "run-1"
    log_dir.mkdir(parents=True)
    (log_dir / "events.jsonl").write_text('{"seq": 70}\nnot-json\n', encoding="utf-8")
    (log_dir / "011-review-1").mkdir()

    log = RunLogger(tmp_path / "logs", "run-1")
    step = log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX"))
    assert step.name == "012-fix-1"
    lines = (log_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ['{"seq": 70}', "not-json"], "appended after, never rewritten"
    assert json.loads(lines[2])["seq"] == 12


def test_a_step_directory_published_without_its_journal_line_is_never_reused(tmp_path):
    """A crash between the step-directory publish and the journal append
    leaves a directory the journal does not name; the next invocation must
    not reuse it and overwrite the dead invocation's artifacts."""
    log = RunLogger(tmp_path / "logs", "run-1")
    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"), stdout="one")
    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX"), stdout="two")
    # The crash: the third step directory exists, its journal line does not.
    (log.run_dir / "003-review-1").mkdir()
    (log.run_dir / "003-review-1" / "stdout.log").write_text("dead", encoding="utf-8")
    assert [json.loads(line)["seq"] for line in log.events_path.read_text().splitlines()] == [1, 2]

    again = RunLogger(tmp_path / "logs", "run-1")
    step = again.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"), stdout="x")
    assert step.name == "004-review-1"
    assert (log.run_dir / "003-review-1" / "stdout.log").read_text(encoding="utf-8") == "dead"


# -- PR #91 review, fourth round: the names are untrusted input ----------------
HUGE = "9" * 240  # a numeric prefix the filesystem accepts and `int` believes


def test_only_an_entry_shaped_like_a_step_directory_counts_toward_the_sequence(tmp_path):
    """The step directory names are the sequence's only input, and the run
    directory is where a same-user agent writes too. An entry that merely
    starts with digits used to be parsed for a number, so a planted
    ``999...9-p`` (a regular file was enough) became the sequence and the
    next step's path was too long for the filesystem, after the agent had
    returned. Only the exact shape ``<seq>-<phase>-<attempt>`` is a step;
    everything else -- the journal, the probe, a moved-aside copy, a name
    that starts with digits -- is skipped without being parsed."""
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "002-review-1").mkdir()
    (run_dir / f"{HUGE}-p").write_text("planted", encoding="utf-8")
    (run_dir / "12-notes.txt").mkdir()
    (run_dir / "events.jsonl.1").write_text('{"seq": 90}\n', encoding="utf-8")
    (run_dir / ".af-probe-dead").write_text("", encoding="utf-8")
    before = sorted(p.name for p in run_dir.iterdir())

    log = RunLogger(tmp_path / "logs", "run-1")
    step = log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX"), stdout="work")
    assert step.name == "003-fix-1"
    assert (step / "stdout.log").read_text(encoding="utf-8") == "work"
    assert sorted(p.name for p in run_dir.iterdir()) == sorted(
        [*before, "003-fix-1", "events.jsonl"]
    )
    assert (run_dir / f"{HUGE}-p").read_text(encoding="utf-8") == "planted"


@pytest.mark.parametrize("kind", ["regular file", "symbolic link", "FIFO"])
def test_a_step_shaped_entry_that_is_not_a_directory_is_refused_before_the_launch(tmp_path, kind):
    """A name of exactly a step's shape that is not a directory is not one
    the controller published, and it cannot be skipped either: the next
    ``mkdir`` of that name would collide with it after the agent had
    returned. It is refused as a corrupt run log at the logger open, with
    the manual step, and the sequence resumes once it is moved aside."""
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "002-review-1").mkdir()
    planted = run_dir / "003-fix-1"
    if kind == "regular file":
        planted.write_text("x", encoding="utf-8")
    elif kind == "symbolic link":
        planted.symlink_to("002-review-1")
    else:
        os.mkfifo(planted)

    with pytest.raises(StateError, match="corrupted run log directory") as exc:
        RunLogger(tmp_path / "logs", "run-1")
    assert f"003-fix-1 is named like a step directory but is a {kind}" in str(exc.value)
    assert "move the entries AutoForge did not create out of logs/run-1/" in str(exc.value)
    assert sorted(p.name for p in run_dir.iterdir()) == ["002-review-1", "003-fix-1"]

    planted.unlink()
    log = RunLogger(tmp_path / "logs", "run-1")
    assert log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX")).name == (
        "003-fix-1"
    )


def test_a_step_numbered_past_what_a_run_can_make_is_refused_before_the_launch(
    tmp_path, monkeypatch
):
    """The number in a step-shaped name decides the width of the next step's
    path, so it is bounded before it is believed: a step past
    ``MAX_STEP_SEQ`` (more invocations than a run can make) is a corrupt
    run log, refused at the open with the manual step. Like the listing
    budget, the bound is on what is believed, not on what is published: a
    logger opened at the bound still records its step, and the next open
    refuses the result."""
    import autoforge.runlog as runlog

    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    # The real bound, against a name the filesystem accepts.
    (run_dir / f"{HUGE}-review-1").mkdir()
    with pytest.raises(StateError, match="corrupted run log directory") as exc:
        RunLogger(tmp_path / "logs", "run-1")
    assert f"{HUGE}-review-1 numbers a step past {runlog.MAX_STEP_SEQ}" in str(exc.value)
    assert "move the entries AutoForge did not create out of logs/run-1/" in str(exc.value)
    (run_dir / f"{HUGE}-review-1").rmdir()

    # The edge, with the bound lowered so the numbers stay readable.
    monkeypatch.setattr(runlog, "MAX_STEP_SEQ", 8)
    (run_dir / "009-review-1").mkdir()
    with pytest.raises(StateError, match="009-review-1 numbers a step past 8"):
        RunLogger(tmp_path / "logs", "run-1")
    (run_dir / "009-review-1").rmdir()
    (run_dir / "008-review-1").mkdir()
    log = RunLogger(tmp_path / "logs", "run-1")
    assert log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX")).name == (
        "009-fix-1"
    )
    with pytest.raises(StateError, match="009-fix-1 numbers a step past 8"):
        RunLogger(tmp_path / "logs", "run-1")


def test_every_step_name_the_logger_publishes_is_one_it_recovers(tmp_path):
    """The shape the recovery accepts (``STEP_DIR_RE``) and the shape the
    logger publishes (``_step_name``) are two places that must agree, or a
    real step would be skipped and its number reused. Every phase the
    controller has, and the sanitised forms of names it does not, round-trip
    through a reopen."""
    from autoforge.runlog import STEP_DIR_RE
    from autoforge.transitions import Phase

    phases = [phase.value for phase in Phase] + ["Weird Phase!", "", "trailing-"]
    log = RunLogger(tmp_path / "logs", "run-1")
    for attempt, phase in enumerate(phases, start=1):
        step = log.log_execution(
            ExecutionRecord(run_id="run-1", seq=0, phase=phase, attempt=attempt)
        )
        assert STEP_DIR_RE.match(step.name), step.name
    assert RunLogger(tmp_path / "logs", "run-1")._seq == len(phases)


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


def test_opening_and_appending_never_read_the_journal(tmp_path, monkeypatch):
    """The cost of opening the logger and of recording an invocation must not
    grow with the journal (#51): a long run's journal is opened, sized and
    appended to, and no byte of it is ever read back."""
    log = RunLogger(tmp_path / "logs", "run-1")
    for _ in range(3):
        log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"))
    asked = _counted_reads(monkeypatch)
    again = RunLogger(tmp_path / "logs", "run-1")
    assert again.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX")).name == (
        "004-fix-1"
    )
    assert asked == [], asked


def test_an_append_costs_the_line_not_the_journal(tmp_path, monkeypatch):
    """#51: recording an invocation appends its line in place. Across a run
    the journal keeps one inode, grows by exactly the lines written, and is
    never replaced by a temporary: the read-then-rewrite that made every
    append cost the whole journal is gone."""
    import autoforge.safefs as safefs

    log = RunLogger(tmp_path / "logs", "run-1")
    replaced: list[str] = []
    real_replace = safefs.os.replace

    def counted_replace(src, dst, **kwargs):
        replaced.append(dst)
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(safefs.os, "replace", counted_replace)
    asked = _counted_reads(monkeypatch)
    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"), stdout="x")
    first = log.events_path.stat()
    for i in range(1, 6):
        log.log_execution(
            ExecutionRecord(run_id="run-1", seq=0, phase="FIX", command=["c"] * (1000 * i)),
            stdout="x",
        )
    after = log.events_path.stat()
    lines = log.events_path.read_bytes().splitlines(keepends=True)
    assert after.st_ino == first.st_ino, "the same inode throughout"
    assert after.st_size == sum(len(line) for line in lines)
    assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3, 4, 5, 6]
    assert "events.jsonl" not in replaced, "artifacts are replaced, the journal is appended"
    assert asked == [], "nothing was read"


# -- R11-F2, then #51: the journal is refused on its size, never read -----------
#
# The journal lives where the agents write (same OS user). It used to be
# read, bounded, for the highest seq it held; now it is never read at all,
# and what remains of the bound is a sanity check on the opened descriptor's
# size: a file larger than any journal the controller could have written is
# not a controller journal, and is refused before anything is written to it.


def test_an_oversized_event_journal_is_refused_without_being_read(tmp_path, monkeypatch):
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
    assert asked == [], asked
    assert journal.stat().st_size == 16 * MAX_EVENT_JOURNAL_BYTES, "opening never writes"


def test_a_journal_at_the_byte_budget_is_appended_to_and_the_result_refused_next(tmp_path):
    """The bound is on what the controller is willing to extend, not on the
    file's final size: at exactly the budget the line is still appended, in
    place, and the journal that results is refused by the next open."""
    from autoforge.runlog import MAX_EVENT_JOURNAL_BYTES

    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    body = b'{"seq": 7}\n'
    journal = run_dir / "events.jsonl"
    journal.write_bytes(body + b" " * (MAX_EVENT_JOURNAL_BYTES - len(body)))
    before = journal.stat()
    log = RunLogger(tmp_path / "logs", "run-1")
    assert log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX")).name == (
        "001-fix-1"
    )
    after = journal.stat()
    assert after.st_ino == before.st_ino
    assert after.st_size > MAX_EVENT_JOURNAL_BYTES
    tail = journal.read_bytes()[MAX_EVENT_JOURNAL_BYTES:]
    assert json.loads(tail)["seq"] == 1
    with pytest.raises(StateError, match="corrupted event journal.*larger than"):
        RunLogger(tmp_path / "logs", "run-1")


def test_the_journal_refusal_names_the_manual_step(tmp_path, monkeypatch):
    """Refusing must not strand the run: the message says what to move
    aside, and the step directories keep the sequence monotonic."""
    import autoforge.runlog as runlog

    monkeypatch.setattr(runlog, "MAX_EVENT_JOURNAL_BYTES", 16)
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_bytes(b"x" * 17)
    (run_dir / "002-review-1").mkdir()
    with pytest.raises(StateError, match="move logs/run-1/events.jsonl aside") as exc:
        RunLogger(tmp_path / "logs", "run-1")
    assert "never reads it" in str(exc.value)
    (run_dir / "events.jsonl").rename(run_dir / "events.jsonl.aside")
    log = RunLogger(tmp_path / "logs", "run-1")
    assert log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX")).name == (
        "003-fix-1"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_a_read_only_journal_is_refused_before_the_launch_with_the_manual_step(tmp_path):
    """The in-place append needs a writable *file* where the read-then-rewrite
    of PR #44 needed only a writable directory, so a journal made read-only
    (the controller creates it 0600; this is someone else's chmod) is
    refused at the open before the launch. The refusal keeps its access
    cause and, like the size refusal, says how to resume."""
    from autoforge.safefs import UnreadableEntryError

    log = RunLogger(tmp_path / "logs", "run-1")
    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"))
    before = log.events_path.read_bytes()
    log.events_path.chmod(0o444)
    try:
        with pytest.raises(UnreadableEntryError, match="Permission denied") as exc:
            RunLogger(tmp_path / "logs", "run-1")
        assert exc.value.path.endswith("events.jsonl")
        assert "make logs/run-1/events.jsonl writable, or move it aside" in str(exc.value)
        assert "never reads it" in str(exc.value)
        assert "sequence continues from their names" in str(exc.value)
        assert log.events_path.read_bytes() == before
    finally:
        log.events_path.chmod(0o600)
    # Made writable again, the sequence continues from the step directory.
    step = RunLogger(tmp_path / "logs", "run-1").log_execution(
        ExecutionRecord(run_id="run-1", seq=0, phase="FIX")
    )
    assert step.name == "002-fix-1"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_a_run_directory_that_cannot_take_a_step_directory_is_refused_before_the_launch(
    tmp_path,
):
    """PR #91 review: the pre-launch gate proves the *run directory*, not
    only the journal. An existing ``logs/<run>`` the controller cannot add
    an entry to (``0500``: the controller creates it ``0700``, so this is
    someone else's directory or chmod) passed the gate while the journal was
    absent, and the step directory's ``mkdir`` then failed after the agent
    had returned. The logger open now creates and removes a probe entry in
    the run directory, the way ``doctor`` probes the state directory, so the
    refusal lands before the launch, keeps its access cause, names the
    manual step, and leaves nothing behind."""
    from autoforge.safefs import UnreadableEntryError

    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    run_dir.chmod(0o500)
    try:
        with pytest.raises(UnreadableEntryError, match="Permission denied") as exc:
            RunLogger(tmp_path / "logs", "run-1")
        assert str(exc.value).startswith("cannot publish into logs/run-1/")
        assert "make logs/run-1/ writable to resume" in str(exc.value)
        assert "sequence continues from their names" in str(exc.value)
        assert sorted(p.name for p in run_dir.iterdir()) == [], "the gate left something behind"
    finally:
        run_dir.chmod(0o700)
    # Made writable again, the run starts at its first step, and the probe
    # that proved the directory is not among the entries.
    log = RunLogger(tmp_path / "logs", "run-1")
    assert sorted(p.name for p in run_dir.iterdir()) == []
    step = log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"))
    assert step.name == "001-review-1"
    assert sorted(p.name for p in run_dir.iterdir()) == ["001-review-1", "events.jsonl"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_a_journal_made_read_only_after_the_logger_opened_refuses_the_append(tmp_path):
    """#55 for the access cause: the append after the agent returned refuses,
    the artifacts are published, and the refusal names both the manual step
    and where the invocation's record is."""
    from autoforge.safefs import UnreadableEntryError

    log = RunLogger(tmp_path / "logs", "run-1")
    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"))
    before = log.events_path.read_bytes()
    # The agent runs here, and leaves the journal read-only.
    log.events_path.chmod(0o444)
    try:
        with pytest.raises(UnreadableEntryError, match="Permission denied") as exc:
            log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX"), stdout="work")
        assert log.events_path.read_bytes() == before, "neither appended to nor replaced"
    finally:
        log.events_path.chmod(0o600)
    assert (log.run_dir / "002-fix-1" / "stdout.log").read_text(encoding="utf-8") == "work"
    message = str(exc.value)
    assert "make logs/run-1/events.jsonl writable, or move it aside" in message
    assert "logs/run-1/002-fix-1/" in message
    assert "journal line was not written" in message


# -- #55: the post-agent append is the other open of the journal ----------------
#
# R11-F2 bounded the recovery read and moved it before the launch. The
# append that records the invocation opens the journal again *after* the
# agent returned, which is exactly when an agent has had the chance to
# enlarge or replace it. That open proves the file again and refuses on its
# size the same way, still without reading it (#51).


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
    assert asked == [], asked
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


def test_a_journal_replaced_by_a_hard_link_after_the_logger_opened_refuses_the_append(
    tmp_path,
):
    """A second name for the journal's inode planted between the open and
    the append is refused on the descriptor (the inode has two names when
    it is inspected), the artifacts are published, and the refusal keeps
    its filesystem cause while saying where the record is."""
    from autoforge.safefs import UnsafePathError

    outside = tmp_path / "notes.txt"
    outside.write_text("mine\n", encoding="utf-8")
    log = RunLogger(tmp_path / "logs", "run-1")
    log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="REVIEW"))
    # The agent runs here, and puts the operator's file at the journal's name.
    log.events_path.unlink()
    os.link(outside, log.events_path)

    with pytest.raises(UnsafePathError, match="hard link: 2 directory entries") as exc:
        log.log_execution(ExecutionRecord(run_id="run-1", seq=0, phase="FIX"), stdout="work")
    assert outside.read_text(encoding="utf-8") == "mine\n"
    assert log.events_path.stat().st_nlink == 2, "neither appended to nor replaced"
    assert (log.run_dir / "002-fix-1" / "stdout.log").read_text(encoding="utf-8") == "work"
    assert "logs/run-1/002-fix-1/" in str(exc.value)
    assert "journal line was not written" in str(exc.value)


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
    (run_dir / "001-review-1").mkdir()
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
    for path in planted[6:]:
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

    `_recover_sequence` opens `events.jsonl` to prove it appendable before
    the first agent runs, so a FIFO left at that path would stall every
    invocation before that agent ran, with no error and no timeout.
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


def test_a_hard_linked_events_journal_is_refused_before_any_agent_is_launched(tmp_path):
    """PR #44 R4-F1 replaced a hard-linked journal; #51 refuses it instead,
    at the open that precedes the launch, so the operator's file is neither
    appended to nor displaced and no agent has done work that would go
    unlogged."""
    from autoforge.safefs import UnsafePathError

    outside = tmp_path / "notes.txt"
    outside.write_text("mine\n", encoding="utf-8")
    run_dir = tmp_path / "logs" / "run-1"
    run_dir.mkdir(parents=True)
    os.link(outside, run_dir / "events.jsonl")
    with pytest.raises(UnsafePathError, match="hard link: 2 directory entries"):
        RunLogger(tmp_path / "logs", "run-1")
    assert outside.read_text(encoding="utf-8") == "mine\n"
    assert (run_dir / "events.jsonl").stat().st_nlink == 2


def test_a_rewritten_artifact_never_keeps_a_tail_of_the_old_one(tmp_path):
    """Replacing rather than truncating must still leave exactly the new bytes."""
    from autoforge.safefs import SafeRoot

    with SafeRoot.open(tmp_path) as root:
        root.write_text("artifact.log", "a long first line that must not survive\n")
        root.write_text("artifact.log", "short\n")
        assert root.read_text("artifact.log") == "short\n"
