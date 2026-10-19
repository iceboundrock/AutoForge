"""The stop request ``epics/<n>/stop-request`` (ADR 0008 D16.4; #182).

Its one lock-free writer publishes by ``link(2)`` and never replaces; every
reader is strict and takes a request that does not parse as a drain; only
the L0 holder removes a request or a leftover temporary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from autoforge import scheduler_stop
from autoforge.errors import SchedulerStateError
from autoforge.run_namespace import EpicRef
from autoforge.scheduler_state import (
    AdmissionState,
    AdmissionStatus,
    EndReason,
    Invocation,
    SchedulerConfig,
    SchedulerState,
    scheduler_state_path,
    write_scheduler_state,
)
from autoforge.scheduler_stop import (
    SCHEDULER_CHANGED,
    STOP_SCHEMA,
    StopMode,
    read_stop_request,
    remove_stale_stop_requests,
    request_stop,
    stop_request_path,
)

TS = "2026-10-19T00:00:00+00:00"
EPIC = EpicRef(number=1, issue_id=8_001)
INVOCATION = "0123456789abcdef" * 2
OTHER_INVOCATION = "fedcba9876543210" * 2


@pytest.fixture
def common(tmp_path) -> Path:
    root = tmp_path / "common"
    root.mkdir()
    return Path(os.path.realpath(root))


def _scheduler(common: Path, invocation: str = INVOCATION, *, ended: bool = False) -> None:
    write_scheduler_state(
        common,
        SchedulerState(
            repository_id=700_001,
            repository_owner="owner",
            repository_name="repo",
            epic=EPIC,
            invocation=Invocation(
                invocation, TS, TS if ended else None, EndReason.STOPPED if ended else None
            ),
            config=SchedulerConfig(2, 60, 30, "c" * 64),
            admission=AdmissionState(AdmissionStatus.OPEN),
            created_at=TS,
            updated_at=TS,
        ),
    )


def _entries(common: Path) -> list[str]:
    directory = stop_request_path(common, 1).parent
    return sorted(os.listdir(directory)) if directory.exists() else []


def _write_request(common: Path, text: str) -> None:
    stop_request_path(common, 1).write_text(text)


def test_without_scheduler_json_the_request_is_refused_and_nothing_is_created(common):
    with pytest.raises(SchedulerStateError, match="nothing to stop"):
        request_stop(common, 1, StopMode.DRAIN)
    assert os.listdir(common) == []  # no directory either


def test_a_request_is_bound_to_the_invocation_and_published_by_link(common):
    _scheduler(common)
    delivery = request_stop(common, 1, StopMode.NOW)
    assert delivery.written and delivery.mode is StopMode.NOW
    assert delivery.invocation_id == INVOCATION and not delivery.scheduler_changed
    path = stop_request_path(common, 1)
    document = json.loads(path.read_text())
    assert document["schema"] == STOP_SCHEMA and document["invocation_id"] == INVOCATION
    assert document["mode"] == "now" and set(document) == {
        "schema",
        "invocation_id",
        "mode",
        "requested_at",
    }
    assert os.lstat(path).st_nlink == 1  # the temporary was unlinked
    assert _entries(common) == ["scheduler.json", "stop-request"]
    read = read_stop_request(common, 1)
    assert read.present and read.request is not None and read.mode is StopMode.NOW
    assert read.applies_to(INVOCATION) and not read.applies_to(OTHER_INVOCATION)


def test_a_second_request_writes_nothing_and_reports_the_existing_mode(common):
    _scheduler(common)
    assert request_stop(common, 1, StopMode.DRAIN).written
    path = stop_request_path(common, 1)
    before = (path.read_bytes(), os.lstat(path).st_ino)
    again = request_stop(common, 1, StopMode.NOW)
    assert not again.written and again.mode is StopMode.DRAIN
    assert "already there" in again.message and "nothing was written" in again.message
    assert (path.read_bytes(), os.lstat(path).st_ino) == before  # never replaced
    assert _entries(common) == ["scheduler.json", "stop-request"]


def test_a_scheduler_that_changed_after_the_write_is_reported(common, monkeypatch):
    _scheduler(common)
    real = scheduler_stop.load_scheduler_state
    calls = []

    def load(root, epic_number):
        calls.append(epic_number)
        if len(calls) == 2:  # the re-read after the link: a new scheduler started
            _scheduler(common, OTHER_INVOCATION)
        return real(root, epic_number)

    monkeypatch.setattr(scheduler_stop, "load_scheduler_state", load)
    delivery = request_stop(common, 1, StopMode.DRAIN)
    assert delivery.scheduler_changed and delivery.message == SCHEDULER_CHANGED
    assert delivery.message == "scheduler changed; stop not delivered"
    # The request names the old invocation: the new scheduler removes it.
    assert read_stop_request(common, 1).bound_elsewhere(OTHER_INVOCATION)


def test_a_request_for_an_ended_invocation_says_now_cannot_signal(common):
    _scheduler(common, ended=True)
    delivery = request_stop(common, 1, StopMode.NOW)
    assert delivery.written and delivery.scheduler_ended
    assert "has ended" in delivery.message and "'now' cannot signal them" in delivery.message


_KILLED_BEFORE_LINK = r"""
import os, signal, sys
from autoforge import scheduler_stop
from autoforge.scheduler_stop import StopMode

def killed(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)

scheduler_stop.os.link = killed
scheduler_stop.request_stop(sys.argv[1], 1, StopMode.DRAIN)
"""


def test_a_writer_killed_before_its_link_leaves_no_request(common):
    _scheduler(common)
    child = subprocess.run(
        [sys.executable, "-c", _KILLED_BEFORE_LINK, str(common)], capture_output=True, timeout=60
    )
    assert child.returncode == -9
    leftovers = [name for name in _entries(common) if name.startswith(".stop-request.")]
    assert len(leftovers) == 1 and "stop-request" not in _entries(common)
    assert not read_stop_request(common, 1).present  # a temporary is not a request
    # A later request succeeds, and only the L0 holder's start removes the leftover.
    assert request_stop(common, 1, StopMode.DRAIN).written
    assert leftovers[0] in _entries(common)
    assert remove_stale_stop_requests(common, 1, INVOCATION, at_start=False) == ()
    assert leftovers[0] in _entries(common)
    assert remove_stale_stop_requests(common, 1, INVOCATION, at_start=True) == (leftovers[0],)
    assert _entries(common) == ["scheduler.json", "stop-request"]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"schema": "autoforge.scheduler-stop/2", "mode": "now"}),
        json.dumps(
            {
                "schema": STOP_SCHEMA,
                "invocation_id": INVOCATION,
                "mode": "now",
                "requested_at": TS,
                "extra": 1,
            }
        ),
        json.dumps(
            {"schema": STOP_SCHEMA, "invocation_id": INVOCATION, "mode": "soon", "requested_at": TS}
        ),
        json.dumps(
            {"schema": STOP_SCHEMA, "invocation_id": "x", "mode": "now", "requested_at": TS}
        ),
        '{"schema": "autoforge.scheduler-stop/1", "schema": "again"}',
        "null",
    ],
    ids=["bad-json", "other-schema", "extra-field", "bad-mode", "bad-id", "duplicate", "null"],
)
def test_a_request_that_does_not_parse_is_a_drain_for_everyone(common, content):
    _scheduler(common)
    _write_request(common, content)
    read = read_stop_request(common, 1)
    assert read.present and read.request is None and read.problem
    assert read.mode is StopMode.DRAIN
    assert read.applies_to(INVOCATION) and read.applies_to(OTHER_INVOCATION)
    assert not read.bound_elsewhere(INVOCATION)
    delivery = request_stop(common, 1, StopMode.NOW)  # never replaced, even then
    assert not delivery.written and "does not parse" in delivery.message


def test_a_link_at_the_request_is_a_drain_and_is_never_followed(common, tmp_path):
    _scheduler(common)
    target = tmp_path / "elsewhere"
    target.write_text(json.dumps({"schema": STOP_SCHEMA}))
    stop_request_path(common, 1).symlink_to(target)
    read = read_stop_request(common, 1)
    assert read.present and read.request is None and "symbolic link" in read.problem
    assert read.mode is StopMode.DRAIN


def test_only_the_l0_holder_removes_stale_requests(common):
    _scheduler(common)
    assert request_stop(common, 1, StopMode.DRAIN).written
    # Ours is kept, on a tick and at a start.
    assert remove_stale_stop_requests(common, 1, INVOCATION, at_start=False) == ()
    assert remove_stale_stop_requests(common, 1, INVOCATION, at_start=True) == ()
    assert read_stop_request(common, 1).present
    # Another invocation's is removed on any tick.
    assert remove_stale_stop_requests(common, 1, OTHER_INVOCATION, at_start=False) == (
        "stop-request",
    )
    assert not read_stop_request(common, 1).present
    # One that does not parse is kept on a tick and removed at the next start.
    _write_request(common, "{broken")
    assert remove_stale_stop_requests(common, 1, INVOCATION, at_start=False) == ()
    assert read_stop_request(common, 1).present
    assert remove_stale_stop_requests(common, 1, INVOCATION, at_start=True) == ("stop-request",)
    assert _entries(common) == ["scheduler.json"]


def test_reading_creates_nothing(common):
    assert not read_stop_request(common, 1).present
    assert remove_stale_stop_requests(common, 1, INVOCATION, at_start=True) == ()
    assert os.listdir(common) == []
    assert not scheduler_state_path(common, 1).exists()
