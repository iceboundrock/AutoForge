"""State: serialize/deserialize, atomic save/load, corruption, idempotency."""

import json

import pytest

from autoforge.errors import StateError
from autoforge.state import AutoForgeState, StatePaths, load_state, save_state
from autoforge.transitions import Phase


def make_state(**kw):
    base = dict(
        run_id="af-test-1",
        repository="owner/repo",
        epic_url="https://github.com/owner/repo/issues/1",
        current_issue_url="https://github.com/owner/repo/issues/2",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    base.update(kw)
    return AutoForgeState(**base)


def test_roundtrip_serialize_deserialize():
    s = make_state(
        phase=Phase.REVIEW,
        review_round=3,
        current_pr_url="https://github.com/owner/repo/pull/42",
        next_issue_rejections=["next issue .../issues/999 could not be verified"],
    )
    d = s.to_dict()
    assert d["phase"] == "REVIEW"
    back = AutoForgeState.from_dict(json.loads(json.dumps(d)))
    assert back == s


def test_save_load_atomic(tmp_path):
    p = tmp_path / ".autoforge" / "state.json"
    s = make_state()
    save_state(s, p)
    assert p.exists()
    loaded = load_state(p)
    assert loaded.run_id == "af-test-1"
    assert loaded.phase == Phase.INITIALIZING
    # no temp files left behind
    assert list(p.parent.glob("*.tmp")) == []


def test_save_overwrites_atomically(tmp_path):
    p = tmp_path / "state.json"
    save_state(make_state(review_round=0), p)
    save_state(make_state(review_round=5), p)
    assert load_state(p).review_round == 5


def test_load_missing_raises_helpful_error(tmp_path):
    with pytest.raises(StateError, match="no state file"):
        load_state(tmp_path / "nope" / "state.json")


def test_load_corrupted_json_raises_and_does_not_clobber(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"phase": "REVIEW", "run_id": ', encoding="utf-8")
    with pytest.raises(StateError, match="[Cc]orrupt"):
        load_state(p)
    # file untouched — loader never writes
    assert p.read_text(encoding="utf-8") == '{"phase": "REVIEW", "run_id": '


def test_load_invalid_utf8_raises_state_error_and_does_not_clobber(tmp_path):
    """Invalid UTF-8 is corruption, not a read error: it must surface as StateError."""
    p = tmp_path / "state.json"
    raw = b'\xff\xfe{"phase": "REVIEW"}'
    p.write_bytes(raw)
    with pytest.raises(StateError, match="[Cc]orrupt.*UTF-8"):
        load_state(p)
    assert p.read_bytes() == raw


def test_load_unknown_phase_raises(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"phase": "TELEPORT", "run_id": "x"}), encoding="utf-8")
    with pytest.raises(StateError, match="[Uu]nknown phase"):
        load_state(p)


def test_load_missing_required_field_raises(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"phase": "REVIEW"}), encoding="utf-8")
    with pytest.raises(StateError, match="run_id"):
        load_state(p)


def test_load_rejects_non_list_next_issue_rejections(tmp_path):
    p = tmp_path / "state.json"
    d = make_state().to_dict()
    d["next_issue_rejections"] = "oops"
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(StateError, match="next_issue_rejections"):
        load_state(p)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("counted_merged_prs", [1]),
        ("open_findings", [1]),
        ("last_fix_resolutions", [1]),
        ("next_issue_rejections", [1]),
        ("superseded_prs", [1]),
    ],
)
def test_load_rejects_wrong_list_element_types(tmp_path, field, bad):
    p = tmp_path / "state.json"
    data = make_state().to_dict()
    data[field] = bad
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match=field):
        load_state(p)


def test_state_without_next_issue_rejections_loads_with_empty_list(tmp_path):
    p = tmp_path / "state.json"
    d = make_state().to_dict()
    del d["next_issue_rejections"]
    p.write_text(json.dumps(d), encoding="utf-8")
    assert load_state(p).next_issue_rejections == []


def test_reset_for_new_issue_clears_next_issue_rejections():
    s = make_state(next_issue_rejections=["rejected"])
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert s.next_issue_rejections == []


def test_timestamps_touch_on_save(tmp_path):
    p = tmp_path / "state.json"
    s = make_state(updated_at="2000-01-01T00:00:00+00:00")
    save_state(s, p)
    assert load_state(p).updated_at != "2000-01-01T00:00:00+00:00"


def test_record_merge_idempotent():
    s = make_state()
    assert s.record_merge("https://github.com/owner/repo/pull/42") is True
    assert s.merged_since_epic_update == 1
    # retry after crash must not double-count
    assert s.record_merge("https://github.com/owner/repo/pull/42") is False
    assert s.merged_since_epic_update == 1
    assert s.record_merge("https://github.com/owner/repo/pull/43") is True
    assert s.merged_since_epic_update == 2
    s.record_epic_update()
    assert s.merged_since_epic_update == 0
    assert len(s.counted_merged_prs) == 2


def test_review_history_roundtrip_and_validation(tmp_path):
    hist = [
        {
            "round": 1,
            "reviewed_head_sha": "a" * 40,
            "result": "needs_fix",
            "finding_count": 1,
            "fingerprint": "abc",
        }
    ]
    s = make_state(review_history=hist, step_count=7)
    back = AutoForgeState.from_dict(json.loads(json.dumps(s.to_dict())))
    assert back.review_history == hist and back.step_count == 7
    p = tmp_path / "state.json"
    d = make_state().to_dict()
    del d["review_history"]  # state written before this field existed
    p.write_text(json.dumps(d), encoding="utf-8")
    assert load_state(p).review_history == []
    for field, bad in (("review_history", "oops"), ("step_count", "3"), ("review_round", True)):
        d = make_state().to_dict()
        d[field] = bad
        p.write_text(json.dumps(d), encoding="utf-8")
        with pytest.raises(StateError, match=field):
            load_state(p)


def test_load_rejects_malformed_review_history_entries(tmp_path):
    """A present but malformed entry is corruption, never "old controller" data."""
    p = tmp_path / "state.json"
    good = {
        "round": 1,
        "reviewed_head_sha": "a" * 40,
        "result": "needs_fix",
        "finding_count": 1,
        "fingerprint": "abc",
        "resolutions": ["d0"],
        "resolutions_truncated": False,
    }
    d = make_state(review_history=[good]).to_dict()
    p.write_text(json.dumps(d), encoding="utf-8")
    assert load_state(p).review_history == [good]
    # a *missing* resolutions key still loads (count-only stagnation rule)
    legacy = {k: v for k, v in good.items() if not k.startswith("resolutions")}
    p.write_text(json.dumps(make_state(review_history=[legacy]).to_dict()), encoding="utf-8")
    assert load_state(p).review_history == [legacy]
    for bad, match in (
        ({**good, "resolutions": "d0"}, "resolutions must be a list"),
        ({**good, "resolutions": {"d0": 1}}, "resolutions must be a list"),
        ({**good, "resolutions": [None]}, "non-empty digest strings"),
        ({**good, "resolutions": [""]}, "non-empty digest strings"),
        ({**good, "resolutions_truncated": "no"}, "resolutions_truncated must be a bool"),
        ({**good, "result": "done"}, "result must be one of"),
        ({**good, "round": "1"}, "round must be an integer"),
        ("not an entry", "must be an object"),
    ):
        p.write_text(json.dumps(make_state(review_history=[bad]).to_dict()), encoding="utf-8")
        with pytest.raises(StateError, match=match) as exc:
            load_state(p)
        assert "review_history" in str(exc.value)


def test_reset_for_new_issue_clears_review_history_but_keeps_step_budget():
    s = make_state(review_history=[{"round": 1}], review_round=1, step_count=9)
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert s.review_history == [] and s.review_round == 0
    assert s.step_count == 9  # cumulative budget survives the switch


def test_replan_state_defaults_and_reset_are_backward_compatible(tmp_path):
    s = make_state(
        execution_attempt=2,
        escalation_count=1,
        superseded_prs=[{"pr_url": "https://github.com/owner/repo/pull/42"}],
        replan_transaction={"stage": "prepared", "transaction_id": "a" * 32},
    )
    # An in-flight transaction belongs to the issue it was opened for: moving
    # to a new issue must not leave one behind for REPLAN_REEXECUTE to replay.
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert s.execution_attempt == 1 and s.escalation_count == 0
    assert s.superseded_prs == [] and s.replan_transaction == {}
    data = make_state().to_dict()
    for field in (
        "execution_attempt",
        "escalation_count",
        "superseded_prs",
        "replan_transaction",
        "verification_failures",
    ):
        del data[field]
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_state(path)
    assert (loaded.execution_attempt, loaded.escalation_count, loaded.superseded_prs) == (1, 0, [])
    assert loaded.replan_transaction == {}


def test_quarantine_state_file_renames_without_overwriting(tmp_path, monkeypatch):
    from datetime import datetime

    from autoforge import state as state_mod
    from autoforge.state import quarantine_state_file

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 6, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(state_mod, "datetime", FrozenDatetime)
    p = tmp_path / "state.json"
    p.write_text("garbage-1", encoding="utf-8")
    first = quarantine_state_file(p)
    assert first == tmp_path / "state.json.corrupt-20260906T120000Z"
    assert not p.exists() and first.read_text(encoding="utf-8") == "garbage-1"

    # same timestamp again: the earlier quarantined file is never overwritten
    p.write_text("garbage-2", encoding="utf-8")
    second = quarantine_state_file(p)
    assert second == tmp_path / "state.json.corrupt-20260906T120000Z.1"
    assert first.read_text(encoding="utf-8") == "garbage-1"
    assert second.read_text(encoding="utf-8") == "garbage-2"


def test_quarantine_missing_state_file_raises(tmp_path):
    from autoforge.state import quarantine_state_file

    with pytest.raises(StateError, match="cannot move"):
        quarantine_state_file(tmp_path / "state.json")


def test_quarantine_preserves_archive_created_after_candidate_selection(tmp_path, monkeypatch):
    """R2-F1: a destination that appears between candidate selection and the move is
    never overwritten; the move retries with the next numeric suffix."""
    import os
    from datetime import datetime

    from autoforge import state as state_mod
    from autoforge.state import quarantine_state_file

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 6, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(state_mod, "datetime", FrozenDatetime)
    p = tmp_path / "state.json"
    p.write_text("garbage-new", encoding="utf-8")
    expected_first = tmp_path / "state.json.corrupt-20260906T120000Z"
    assert not expected_first.exists()  # candidate selection would pick this name

    real_link = os.link
    attempts: list[str] = []

    def racing_link(src, dst, *args, **kwargs):
        # The reservation is made relative to an open directory descriptor, so
        # `dst` is a bare name; that is the point -- no pathname is re-resolved
        # between selecting a candidate and claiming it.
        attempts.append(os.fspath(dst))
        if len(attempts) == 1:
            # Another process wins the race for the selected name right before our move.
            assert os.fspath(dst) == expected_first.name
            expected_first.write_text("preexisting-archive", encoding="utf-8")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", racing_link)
    moved = quarantine_state_file(p)

    assert attempts == [
        expected_first.name,
        expected_first.name + ".1",
    ]
    assert moved == tmp_path / "state.json.corrupt-20260906T120000Z.1"
    assert expected_first.read_text(encoding="utf-8") == "preexisting-archive"
    assert moved.read_text(encoding="utf-8") == "garbage-new"
    assert not p.exists()


def test_quarantine_gives_up_after_bounded_attempts(tmp_path, monkeypatch):
    import os

    from autoforge import state as state_mod
    from autoforge.state import quarantine_state_file

    monkeypatch.setattr(state_mod, "_QUARANTINE_MAX_ATTEMPTS", 3)
    p = tmp_path / "state.json"
    p.write_text("garbage", encoding="utf-8")

    def always_taken(src, dst, *args, **kwargs):
        raise FileExistsError(17, "File exists", os.fspath(dst))

    monkeypatch.setattr(os, "link", always_taken)
    with pytest.raises(StateError, match="no free name after 3 attempts"):
        quarantine_state_file(p)
    assert p.read_text(encoding="utf-8") == "garbage"  # original untouched


def test_quarantine_unlink_failure_leaves_original_and_drops_reservation(tmp_path, monkeypatch):
    import os

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.write_text("garbage", encoding="utf-8")
    real_unlink = os.unlink

    def failing_unlink(path, *args, **kwargs):
        if os.fspath(path) == p.name:
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", failing_unlink)
    with pytest.raises(StateError, match="cannot move"):
        quarantine_state_file(p)
    assert p.read_text(encoding="utf-8") == "garbage"
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_quarantine_refuses_a_source_replaced_after_reservation(tmp_path, monkeypatch):
    import os

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.write_text("corrupt", encoding="utf-8")
    real_link = os.link
    replaced = False

    def racing_link(src, dst, *args, **kwargs):
        nonlocal replaced
        result = real_link(src, dst, *args, **kwargs)
        if not replaced:
            replaced = True
            p.unlink()
            p.write_text("fresh state", encoding="utf-8")
        return result

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(StateError, match="changed while being quarantined"):
        quarantine_state_file(p)
    assert p.read_text(encoding="utf-8") == "fresh state"
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_a_run_id_that_is_a_path_is_corrupt_state(tmp_path):
    """R3-F2: `run_id` names `<state_dir>/logs/<run_id>`, so it is a write path.

    Loading only checked that it was non-empty, so a hand-edited or truncated
    state carrying "../escape" was accepted and every log artifact of the run
    was then written outside the state directory. A state file that cannot be
    turned into a path fails loudly here, naming the state file, rather than
    silently relocating the controller's own audit trail.
    """
    p = tmp_path / "state.json"
    for bad in ("../escape", "/absolute", "sub/dir", "..", "."):
        data = make_state().to_dict()
        data["run_id"] = bad
        p.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(StateError, match="invalid run_id"):
            load_state(p)
    # A generated identifier still round-trips.
    save_state(make_state(run_id="af-20260101T000000Z-abc123"), p)
    assert load_state(p).run_id == "af-20260101T000000Z-abc123"


def test_load_dangling_symlink_is_corrupt_state(tmp_path):
    """R4-F2: a dangling state.json symlink is an existing (unreadable) entry, not 'no state'.

    The refusal names the link, never what it points at: the controller reads
    and replaces its own regular file, so a link is refused whether its target
    exists, is a FIFO, or is someone else's file. Classifying the target would
    imply some targets are acceptable.
    """
    import os

    p = tmp_path / "state.json"
    p.symlink_to("missing-target.json")
    with pytest.raises(StateError, match="a symbolic link, not a regular file"):
        load_state(p)
    assert p.is_symlink() and os.readlink(p) == "missing-target.json"


def test_quarantine_moves_dangling_symlink_entry_itself(tmp_path):
    import os

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.symlink_to("missing-target.json")
    moved = quarantine_state_file(p)
    assert not os.path.lexists(p)
    assert moved.is_symlink() and os.readlink(moved) == "missing-target.json"


def test_quarantine_moves_symlink_without_following_it(tmp_path):
    """The link is archived as a link; the file it points to is never touched."""
    import os

    from autoforge.state import quarantine_state_file

    target = tmp_path / "elsewhere.json"
    target.write_text("{not json", encoding="utf-8")
    p = tmp_path / "state.json"
    p.symlink_to(target.name)
    moved = quarantine_state_file(p)
    assert not os.path.lexists(p)
    assert moved.is_symlink() and os.readlink(moved) == target.name
    assert not target.is_symlink() and target.read_text(encoding="utf-8") == "{not json"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["elsewhere.json", moved.name]


# -- non-regular state entries (R6-F2) -----------------------------------------
def _call_with_timeout(fn, seconds: float = 5.0):
    """Run ``fn`` in a daemon thread; fail the test instead of hanging forever."""
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
    assert not t.is_alive(), f"call blocked for more than {seconds}s"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_load_fifo_state_entry_fails_loudly_without_blocking(tmp_path):
    """R6-F2: a FIFO without a writer must not hang load_state; it is a corrupt entry."""
    import os

    p = tmp_path / "state.json"
    os.mkfifo(p)
    with pytest.raises(StateError, match="a FIFO, not a regular file"):
        _call_with_timeout(lambda: load_state(p))
    assert os.path.lexists(p) and [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_load_symlink_to_fifo_fails_loudly_without_blocking(tmp_path):
    import os

    os.mkfifo(tmp_path / "real.fifo")
    p = tmp_path / "state.json"
    p.symlink_to("real.fifo")
    with pytest.raises(StateError, match="a symbolic link, not a regular file"):
        _call_with_timeout(lambda: load_state(p))
    assert p.is_symlink() and os.readlink(p) == "real.fifo"


def test_load_directory_state_entry_is_corrupt_state(tmp_path):
    p = tmp_path / "state.json"
    p.mkdir()
    (p / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(StateError, match="a directory, not a regular file.*by hand"):
        load_state(p)
    assert p.is_dir() and (p / "keep").read_text(encoding="utf-8") == "x"


def test_load_socket_state_entry_is_corrupt_state(tmp_path, monkeypatch):
    import socket

    monkeypatch.chdir(tmp_path)  # AF_UNIX paths are short-limited; bind relative
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind("state.json")
        with pytest.raises(StateError, match="a socket, not a regular file"):
            _call_with_timeout(lambda: load_state(tmp_path / "state.json"))
    finally:
        sock.close()
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_load_rejects_entry_swapped_for_a_fifo_after_inspection(tmp_path, monkeypatch):
    """The open descriptor is re-checked, so a swap between lstat and open is caught."""
    import os

    from autoforge import state as state_mod

    p = tmp_path / "state.json"
    p.write_text("{}", encoding="utf-8")
    real_lstat = os.lstat

    swapped = False

    def swapping_lstat(path, *a, **kw):
        nonlocal swapped
        st = real_lstat(path, *a, **kw)
        if not swapped and os.fspath(path) == p.name:
            swapped = True
            p.unlink()
            os.mkfifo(p)
        return st

    monkeypatch.setattr(state_mod.os, "lstat", swapping_lstat)
    with pytest.raises(StateError, match="a FIFO, not a regular file"):
        _call_with_timeout(lambda: load_state(p))


def test_quarantine_moves_fifo_entry_without_opening_it(tmp_path):
    import os
    import stat

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    os.mkfifo(p)
    moved = _call_with_timeout(lambda: quarantine_state_file(p))
    assert not os.path.lexists(p)
    assert stat.S_ISFIFO(os.lstat(moved).st_mode)
    assert [q.name for q in tmp_path.iterdir()] == [moved.name]


def test_quarantine_moves_symlink_to_fifo_as_a_link(tmp_path):
    import os
    import stat

    from autoforge.state import quarantine_state_file

    os.mkfifo(tmp_path / "real.fifo")
    p = tmp_path / "state.json"
    p.symlink_to("real.fifo")
    moved = _call_with_timeout(lambda: quarantine_state_file(p))
    assert moved.is_symlink() and os.readlink(moved) == "real.fifo"
    assert stat.S_ISFIFO(os.lstat(tmp_path / "real.fifo").st_mode)


def test_quarantine_refuses_directory_and_leaves_it_untouched(tmp_path):
    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.mkdir()
    (p / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(StateError, match="it is a directory.*by hand"):
        quarantine_state_file(p)
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]
    assert (p / "keep").read_text(encoding="utf-8") == "x"


def test_save_state_fsyncs_the_directory_entry_after_the_rename(tmp_path, monkeypatch):
    """PR #44, O3: fsyncing the bytes says nothing about the rename that publishes them.

    A crash right after `save_state` returned could leave the previous
    `state.json` in place — losing the pending-invocation checkpoint a LOCAL
    write phase persists *before* launching an agent.
    """
    import os

    from autoforge import state as state_mod

    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])

    p = tmp_path / "run" / "state.json"
    save_state(make_state(), p)
    assert len(synced) >= 2, "the temp file and its directory must both be flushed"
    assert load_state(p).run_id == "af-test-1"

    # Best effort: a filesystem that cannot fsync a directory must not fail the
    # run. The replace is atomic either way; only its durability is weaker.
    import stat as stat_mod

    def refusing_fsync(fd):
        if stat_mod.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(22, "no directory fsync here")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", refusing_fsync)
    save_state(make_state(review_round=4), p)
    assert load_state(p).review_round == 4
    assert state_mod is not None  # the module under test, imported above


def test_save_state_does_not_swallow_fatal_directory_fsync(tmp_path, monkeypatch):
    import os
    import stat as stat_mod

    p = tmp_path / "state.json"
    save_state(make_state(), p)
    real_fsync = os.fsync

    def failing_fsync(fd):
        if stat_mod.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "I/O error")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="I/O error"):
        save_state(make_state(review_round=4), p)


def test_state_paths_reject_a_normal_directory_replacement(tmp_path):
    paths = StatePaths.from_state_dir(tmp_path / "state")
    with paths.open_root(create=True):
        pass
    moved = tmp_path / "moved"
    (tmp_path / "state").rename(moved)
    (tmp_path / "state").mkdir()
    with pytest.raises(StateError, match="state directory .* replaced"):
        paths.open_root()
