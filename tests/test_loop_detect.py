"""The loop detector (#194): actions, lines and retries in, verdicts and warnings out.

No process runs here and no clock is read: :class:`LoopMonitor` is fed
fingerprints, bytes and retries with explicit monotonic times, the way the
provider adapters and the executor's readers feed it.
"""

import json

import pytest

from autoforge.config import LoopDetectionConfig
from autoforge.loop_detect import (
    ACTION_KILLED,
    ACTION_WARNED,
    MAX_LINE_BYTES,
    SIGNAL_ACTION_CYCLE,
    SIGNAL_NO_NOVELTY,
    SIGNAL_REPEATED_LINES,
    SIGNAL_RETRY_STORM,
    LoopMonitor,
    action_fingerprint,
    digest,
    line_key,
    span_text,
)

TOKEN = "ghp_" + "Z9" * 18


def _action(name: str, tool_input: object, result: object = "ok") -> tuple[str, bytes]:
    return name, action_fingerprint(name, digest(tool_input), digest(result))


def _monitor(mode: str = "kill", **over) -> tuple[LoopMonitor, list[str]]:
    warnings: list[str] = []
    config = LoopDetectionConfig(mode=mode, **over)
    return LoopMonitor(config, 0.0, warnings.append), warnings


def _feed(monitor: LoopMonitor, actions, *, start: float = 0.0, step: float = 1.0):
    """Feed ``actions`` one ``step`` apart; the verdicts returned, by index."""
    verdicts = {}
    for i, (name, fingerprint) in enumerate(actions):
        verdict = monitor.action(name, fingerprint, start + i * step)
        if verdict is not None:
            verdicts[i] = verdict
    return verdicts


# -- fingerprints and line keys ------------------------------------------------


def test_a_digest_is_of_the_canonical_content_and_reveals_none_of_it():
    assert digest({"a": 1, "b": [2, "x"]}) == digest({"b": [2, "x"], "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})
    assert digest("1") != digest(1)
    assert len(digest(TOKEN)) == 16 and TOKEN.encode() not in digest(TOKEN)


def test_a_value_that_cannot_be_serialized_never_matches_another():
    looped: list = []
    looped.append(looped)
    assert digest(looped) != digest(looped)


def test_an_action_is_its_name_input_and_result():
    base = _action("Bash", {"command": "make test"}, "1 failed")[1]
    assert base == _action("Bash", {"command": "make test"}, "1 failed")[1]
    assert base != _action("Read", {"command": "make test"}, "1 failed")[1]
    assert base != _action("Bash", {"command": "make lint"}, "1 failed")[1]
    assert base != _action("Bash", {"command": "make test"}, "2 failed")[1]
    # The parts are length-prefixed: shifting bytes between them is no match.
    assert action_fingerprint("ab", b"c", b"") != action_fingerprint("a", b"bc", b"")


def test_a_line_key_masks_numbers_hex_escapes_and_spacing():
    assert line_key(b"retrying in 5s (attempt 37)") == line_key(b"retrying in 12s (attempt 38)")
    assert line_key(b"HEAD is now at 3f2a9c1 fix") == line_key(b"HEAD is now at 9b0e4d7 fix")
    assert line_key(b"\x1b[31merror:\x1b[0m   boom") == line_key(b"error: boom")
    assert line_key(b"error: boom") != line_key(b"error: bang")
    # A word made of hex letters only is still a word.
    assert line_key(b"deadbeef cafe") != line_key(b"feedface cafe")


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"   ",
        b"........F.....s...xX..E.",
        b"..FF..ss.. [ 45%]",
        b"[#####     ] 45%",
        b"=" * 40,
        b"12:34:56",
    ],
)
def test_a_line_with_no_letter_is_not_a_line_that_can_loop(line):
    assert line_key(line) is None


def test_span_text():
    assert span_text(42.9) == "42s"
    assert span_text(554) == "9m14s"
    assert span_text(3 * 3600 + 7 * 60) == "3h07m"


# -- A: action cycles ------------------------------------------------------------

CYCLES = [
    [_action("Bash", {"command": "pytest"}, "1 failed")],
    [_action("Bash", {"command": "pytest"}, "1 failed"), _action("Read", {"path": "a.py"})],
    [
        _action("Bash", {"command": "pytest"}, "1 failed"),
        _action("Read", {"path": "a.py"}),
        _action("Grep", {"pattern": "def f"}),
    ],
    [
        _action("Bash", {"command": "pytest"}, "1 failed"),
        _action("Read", {"path": "a.py"}),
        _action("Edit", {"path": "a.py", "old": "x", "new": "y"}),
        _action("Edit", {"path": "a.py", "old": "y", "new": "x"}),
    ],
]


@pytest.mark.parametrize("cycle", CYCLES, ids=lambda c: f"period-{len(c)}")
def test_a_cycle_repeated_to_the_threshold_is_a_loop_and_not_before(cycle):
    period = len(cycle)
    monitor, _ = _monitor()
    verdicts = _feed(monitor, cycle * 8)
    assert list(verdicts) == [8 * period - 1]
    finding = verdicts[8 * period - 1]
    assert finding.signal == SIGNAL_ACTION_CYCLE
    assert (finding.period, finding.repeats) == (period, 8)
    assert finding.tools == tuple(name for name, _ in cycle)
    # One step short of the eighth copy, nothing.
    monitor, _ = _monitor()
    assert _feed(monitor, (cycle * 8)[:-1]) == {}


def test_the_verdict_is_returned_once_and_names_the_cycle():
    monitor, _ = _monitor()
    cycle = CYCLES[1]
    # The first copy starts at 0s and the eighth ends at the 16th action.
    verdicts = _feed(monitor, cycle * 12, step=554 / 15)
    assert list(verdicts) == [15]
    assert monitor.verdict is verdicts[15]
    assert verdicts[15].describe() == (
        "the same 2-step action cycle (Bash, Read) repeated 8 times over 9m14s with no new action"
    )
    report = monitor.report(killed=True)
    assert report is not None and report.action == ACTION_KILLED


def test_a_one_step_cycle_is_described_as_the_same_action():
    monitor, _ = _monitor()
    verdicts = _feed(monitor, CYCLES[0] * 8)
    assert verdicts[7].describe().startswith("the same action (Bash) repeated 8 times over 7s")


def test_the_shortest_period_wins_a_tie():
    monitor, _ = _monitor()
    verdicts = _feed(monitor, CYCLES[0] * 8)
    assert verdicts[7].period == 1


def test_the_same_test_after_each_edit_is_no_loop():
    monitor, warnings = _monitor()
    actions = []
    for i in range(40):
        actions.append(_action("Edit", {"path": "a.py", "new": f"attempt {i}"}))
        actions.append(_action("Bash", {"command": "pytest"}, "1 failed"))
    assert _feed(monitor, actions) == {}
    assert warnings == [] and monitor.report(killed=False) is None


def test_git_status_and_diff_interleaved_with_edits_are_no_loop():
    monitor, warnings = _monitor()
    actions = []
    for i in range(30):
        actions.append(_action("Edit", {"path": f"f{i % 3}.py", "new": f"v{i}"}))
        actions.append(_action("Bash", {"command": "git status"}, f"modified: f{i % 3}.py"))
        actions.append(_action("Bash", {"command": "git diff"}, f"+v{i}"))
    assert _feed(monitor, actions) == {}
    assert warnings == []


def test_polling_ci_a_few_times_is_no_loop():
    monitor, _ = _monitor()
    # The same answer five times, a minute apart, then the checks pass.
    polls = [_action("Bash", {"command": "gh pr checks 7"}, "build pending")] * 5
    polls.append(_action("Bash", {"command": "gh pr checks 7"}, "build pass"))
    assert _feed(monitor, polls, step=60.0) == {}
    assert monitor.verdict is None


def test_a_new_action_breaks_the_cycle():
    monitor, _ = _monitor()
    cycle = CYCLES[1]
    actions = cycle * 7 + [_action("Edit", {"path": "a.py", "new": "fix"})] + cycle * 7
    assert _feed(monitor, actions) == {}


# -- B: no novelty --------------------------------------------------------------

SMALL = {"max_cycle_period": 2, "max_cycle_repeats": 3, "novelty_window_seconds": 60}
# Three actions replayed in an order with no cycle of period 2 or less.
IRREGULAR = [0, 2, 1, 1, 0, 2, 0, 1, 2, 2, 1, 0, 1, 2, 0, 0, 2, 1]


def _irregular_replays():
    distinct = [_action("Read", {"path": f"f{i}.py"}) for i in range(3)]
    return distinct, [distinct[i] for i in IRREGULAR]


def test_no_new_action_for_the_window_with_enough_replays_is_a_loop():
    monitor, _ = _monitor(**SMALL)
    distinct, replays = _irregular_replays()
    verdicts = _feed(monitor, distinct + replays, step=10.0)
    assert verdicts, "a stretch with nothing new must be found"
    index, finding = next(iter(verdicts.items()))
    assert finding.signal == SIGNAL_NO_NOVELTY
    # Not before the window has passed since the last new action (t=20s).
    assert index * 10.0 - 20.0 >= 60.0
    assert finding.repeats >= 6
    assert finding.tools == ("Read",)
    assert finding.describe().startswith("no new action for 1m")


def test_below_the_action_floor_a_long_window_is_inconclusive():
    monitor, warnings = _monitor(**SMALL)
    distinct, replays = _irregular_replays()
    # One replay every 40s: two in any 60s window, a third of the floor of six.
    assert _feed(monitor, distinct + replays, step=40.0) == {}
    # (A pair in the order is half of this small cycle threshold: warned.)
    assert not [text for text in warnings if "no new action" in text]
    calibration = monitor.calibration()
    assert calibration["loop_longest_novelty_free_seconds"] == 40 * 18


def test_a_long_silent_tool_call_is_no_loop():
    monitor, warnings = _monitor()
    distinct = [_action("Bash", {"command": "make slow"}, "ok")]
    assert _feed(monitor, distinct * 2, step=7200.0) == {}
    assert warnings == []


# -- C: repeated lines ------------------------------------------------------------


def _lines(monitor: LoopMonitor, data: bytes, now: float = 1.0, stream: str = "stderr"):
    return monitor.output(stream, data, now)


def test_a_line_repeated_to_the_threshold_is_a_loop():
    monitor, _ = _monitor()
    verdict = None
    for i in range(199):
        verdict = _lines(monitor, f"retrying in {i}s (attempt {i})\n".encode(), float(i))
        assert verdict is None
    verdict = _lines(monitor, b"retrying in 1s (attempt 200)\n", 300.0)
    assert verdict is not None and verdict.signal == SIGNAL_REPEATED_LINES
    assert (verdict.period, verdict.repeats, verdict.stream) == (1, 200, "stderr")
    assert verdict.describe() == (
        "the same stderr line repeated 200 times over 5m00s with no new line"
    )


def test_a_cycle_of_lines_split_across_chunks_is_a_loop():
    monitor, _ = _monitor(max_line_repeats=10)
    data = b"Thinking about the fix\r\nRunning: make test\r\n" * 10
    verdict = None
    for i in range(0, len(data), 7):
        verdict = _lines(monitor, data[i : i + 7]) or verdict
    assert verdict is not None and (verdict.period, verdict.repeats) == (2, 10)


def test_pytest_dots_and_progress_bars_are_never_lines_that_loop():
    monitor, warnings = _monitor(max_line_repeats=10)
    for i in range(500):
        _lines(monitor, b"........................................ [ 45%]\n")
        _lines(monitor, f"\rDownloading wheels: {i}% |{'#' * (i % 40)}|".encode())
        _lines(monitor, b"\r\x1b[2K\xe2\xa0\x8b Thinking\r")
    assert monitor.verdict is None and warnings == []
    assert monitor.calibration()["loop_max_line_repeats"] == 0


ASCII_SPINNER = "|/-\\"
BRAILLE_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Frame ``n`` (0 to 99) of a display a CLI draws over in place with a bare CR.
REDRAWN_DISPLAYS = {
    "tqdm": lambda n: (
        f" {n}%|{'█' * (n // 10)}{'▏▎▍▌▋▊▉'[n % 7]}| {n}/100 [00:{n:02d}<00:12, 4.50it/s]"
    ),
    "tqdm-ascii": lambda n: f" {n}%|{'#' * (n // 10)}{n % 10}| {n}/100 [00:{n:02d}, 4.50it/s]",
    "rich": lambda n: f"   {'━' * (n // 3)}╺{'━' * (33 - n // 3)} {n / 20:.1f}/5.0 MB eta 0:00:12",
    "git": lambda n: f"Receiving objects:  {n}% ({n * 10}/1000), 1.20 MiB | 2.00 MiB/s",
    "docker": lambda n: f"3f2a9c1d: Downloading [{'=' * (n // 5)}>{' ' * (20 - n // 5)}]  45.6MB",
    "braille-spinner": lambda n: f"{BRAILLE_SPINNER[n % 10]} Thinking",
    "geometric-spinner": lambda n: f"{'◐◓◑◒'[n % 4]} Waiting for the model",
    "ascii-spinner-before": lambda n: f"{ASCII_SPINNER[n % 4]} Working",
    "ascii-spinner-after": lambda n: f"Working... {ASCII_SPINNER[n % 4]}",
}


@pytest.mark.parametrize("display", REDRAWN_DISPLAYS.values(), ids=list(REDRAWN_DISPLAYS))
def test_a_redrawn_progress_display_is_no_line_though_the_same_text_ended_by_lf_is(display):
    for n in (0, 7, 45, 99):
        frame = display(n).encode()
        assert line_key(frame, redraw=True) is None
        assert line_key(frame) is not None


@pytest.mark.parametrize(
    "message",
    [
        b"retrying in 5s (attempt 37)",
        b"Error: connection reset by peer; retrying",
        b"rate limited - retrying",
        b"API error 529 (overloaded) [attempt 3/10]",
    ],
)
def test_a_redrawn_message_is_a_line_like_one_ended_by_lf(message):
    assert line_key(message, redraw=True) == line_key(message) is not None


@pytest.mark.parametrize("display", REDRAWN_DISPLAYS.values(), ids=list(REDRAWN_DISPLAYS))
def test_a_progress_bar_or_spinner_redrawn_with_a_bare_cr_never_loops(display):
    monitor, warnings = _monitor(max_line_repeats=10)
    # Drawn and then ended by a CR, or a CR and a clear-line, then drawn.
    for i in range(500):
        assert _lines(monitor, f"{display(i % 100)}\r".encode(), float(i)) is None
    for i in range(500):
        data = f"\r\x1b[2K{display(i % 100)}".encode()
        assert _lines(monitor, data, float(500 + i)) is None
    assert monitor.verdict is None and warnings == []
    assert monitor.calibration()["loop_max_line_repeats"] == 0


@pytest.mark.parametrize(
    "frame",
    [
        "retrying in {i}s (attempt {i})\r",
        "\rretrying in {i}s (attempt {i})",
        "\r⠋ waiting\r| waiting\rretrying in {i}s (attempt {i})",
    ],
    ids=["cr-after", "cr-before", "among-spinner-frames"],
)
def test_a_message_redrawn_with_a_bare_cr_repeated_to_the_threshold_is_a_loop(frame):
    monitor, _ = _monitor()
    verdicts = {}
    for i in range(201):
        verdict = _lines(monitor, frame.format(i=i).encode(), float(i))
        if verdict is not None:
            verdicts[i] = verdict
    # What follows a CR decides it: the 200th message is ended by the 201st write.
    assert list(verdicts) == [200]
    verdict = verdicts[200]
    assert verdict.signal == SIGNAL_REPEATED_LINES
    assert (verdict.period, verdict.repeats, verdict.stream) == (1, 200, "stderr")
    assert monitor.calibration()["loop_max_line_repeats"] == 200


def test_a_crlf_split_across_reads_ends_a_line_not_a_redraw():
    monitor, _ = _monitor(max_line_repeats=3)
    verdict = None
    for i in range(3):
        assert verdict is None
        # A progress mark on a line CRLF ends: a line, not a redraw.
        _lines(monitor, f"Downloading wheels: {i}%\r".encode())
        verdict = _lines(monitor, b"\n")
    assert verdict is not None and (verdict.period, verdict.repeats) == (1, 3)


def test_a_long_line_is_keyed_by_its_head():
    monitor, _ = _monitor(max_line_repeats=3)
    head = b"x" * MAX_LINE_BYTES
    for i in range(2):
        assert _lines(monitor, head + str(i).encode() * 5000 + b"\n") is None
    assert _lines(monitor, head + b"y\n") is not None


def test_streams_are_tracked_apart():
    monitor, _ = _monitor(max_line_repeats=4)
    for _ in range(3):
        assert _lines(monitor, b"same line\n", stream="stdout") is None
        assert _lines(monitor, b"same line\n", stream="stderr") is None
    assert _lines(monitor, b"same line\n", stream="stdout").stream == "stdout"


# -- D: retry storms ---------------------------------------------------------------


def test_retries_in_a_row_over_the_window_are_a_loop_and_a_turn_ends_them():
    monitor, _ = _monitor(max_cycle_repeats=3, novelty_window_seconds=60)
    assert monitor.retry(0.0) is None
    assert monitor.retry(30.0) is None
    monitor.turn(40.0)
    assert monitor.retry(50.0) is None
    assert monitor.retry(80.0) is None
    verdict = monitor.retry(110.0)
    assert verdict is not None and verdict.signal == SIGNAL_RETRY_STORM
    assert verdict.describe() == ("3 provider retries in a row over 1m00s with no completed turn")
    assert monitor.calibration()["loop_max_retry_streak"] == 3


def test_many_quick_retries_are_not_a_storm_until_the_window_passes():
    monitor, _ = _monitor(max_cycle_repeats=3, novelty_window_seconds=60)
    assert all(monitor.retry(float(i)) is None for i in range(20))


# -- modes, warnings, reports ------------------------------------------------------


def test_warn_mode_warns_from_half_the_threshold_at_bounded_intervals_and_never_kills():
    monitor, warnings = _monitor("warn")
    cycle = CYCLES[1]
    # 40 copies of a 2-step cycle, 10s apart: 800s in all.
    verdicts = _feed(monitor, cycle * 40, step=10.0)
    assert verdicts == {} and monitor.verdict is None
    assert warnings[0] == "possible loop: 2-step cycle (Bash, Read) repeated 4×"
    # Once more when it became conclusive, then at most every 120s.
    assert warnings[1] == (
        "loop detected, not killed (mode warn): 2-step cycle (Bash, Read) repeated 8×"
    )
    assert 3 <= len(warnings) <= 2 + 800 // 120
    assert monitor.warnings == len(warnings)
    report = monitor.report(killed=False)
    assert report is not None and report.action == ACTION_WARNED
    assert report.finding.repeats >= 8 and report.warnings == len(warnings)


def test_kill_mode_warns_before_it_kills():
    monitor, warnings = _monitor("kill")
    _feed(monitor, CYCLES[1] * 8)
    assert warnings == ["possible loop: 2-step cycle (Bash, Read) repeated 4×"]
    assert monitor.verdict is not None


def test_a_verdict_on_a_child_that_exited_first_is_reported_as_a_finding():
    monitor, _ = _monitor("kill")
    _feed(monitor, CYCLES[0] * 8)
    report = monitor.report(killed=False)
    assert report is not None and report.action == ACTION_WARNED


def test_off_mode_says_nothing_but_still_calibrates():
    monitor, warnings = _monitor("off")
    assert _feed(monitor, CYCLES[1] * 20) == {}
    for _ in range(300):
        monitor.output("stderr", b"same line\n", 1.0)
    assert warnings == [] and monitor.verdict is None
    assert monitor.report(killed=False) is None
    assert monitor.calibration() == {
        "loop_actions": 40,
        "loop_max_cycle_repeats": 20,
        "loop_max_cycle_period": 2,
        "loop_longest_novelty_free_seconds": 38,
        "loop_longest_novelty_free_actions": 38,
        "loop_max_line_repeats": 300,
        "loop_max_retry_streak": 0,
    }


def test_a_warning_sink_that_raises_is_dropped_and_changes_nothing():
    calls = []

    def warn(text: str) -> None:
        calls.append(text)
        raise RuntimeError("closed")

    monitor = LoopMonitor(LoopDetectionConfig(mode="kill"), 0.0, warn, warn_interval=0.0)
    verdicts = _feed(monitor, CYCLES[0] * 8)
    assert len(calls) == 1 and list(verdicts) == [7]


def test_the_report_holds_names_counts_and_times_and_nothing_an_agent_wrote():
    monitor = LoopMonitor(LoopDetectionConfig(mode="kill"), 100.0)
    secret = {"command": f"curl -H 'Authorization: token {TOKEN}' https://x"}
    # A tool name is cleaned like a progress event's: redacted, then clipped.
    cycle = [_action("Bash", secret, f"echo {TOKEN}"), _action(f"mcp {TOKEN}", {"q": TOKEN})]
    _feed(monitor, cycle * 8, start=130.0, step=2.0)
    report = monitor.report(killed=True)
    assert report is not None
    record = report.record()
    assert record["signal"] == SIGNAL_ACTION_CYCLE and record["action"] == ACTION_KILLED
    assert (record["period"], record["repeats"]) == (2, 8)
    assert record["started_after_seconds"] == 30.0
    assert record["detected_after_seconds"] == 60.0
    assert record["tools"][0] == "Bash"
    text = json.dumps(record)
    assert TOKEN not in text and "curl" not in text
    assert set(record) == {
        "signal",
        "action",
        "period",
        "repeats",
        "tools",
        "stream",
        "started_after_seconds",
        "detected_after_seconds",
        "description",
    }


# -- end to end: the engine over the fake claude ----------------------------------
def _looping_lines(copies: int, secret: str = "", tail: list | None = None) -> list:
    """``copies`` of the same two tool calls, same inputs and same results."""
    from tests import claude_fake

    lines: list = [claude_fake.init()]
    for i in range(copies):
        lines += [
            claude_fake.tool_use(f"b{i}", "Bash", command=f"run-suite-q7 {secret}".strip()),
            claude_fake.tool_result(f"b{i}", f"suite-q7 failed {secret}".strip()),
            claude_fake.tool_use(f"r{i}", "Read", file_path="$CWD/src/a.py"),
            claude_fake.tool_result(f"r{i}", "contents"),
        ]
    return lines + (tail or [])


def _step_files(state_dir) -> dict[str, dict]:
    (progress_log,) = state_dir.rglob("progress.log")
    step = progress_log.parent
    return {
        name: json.loads((step / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("request", "execution")
    }


def test_e2e_kill_mode_kills_a_looping_agent_and_leaves_the_phase_unchanged(tmp_path, fake_github):
    """#194 acceptance: a looping agent that would otherwise run on is
    killed as on a timeout; the error names the cycle; the phase is not
    advanced; ``execution.json`` says why, with names and counts only."""
    import time

    from autoforge.errors import ExecutionTimeoutError
    from autoforge.transitions import Phase
    from tests.test_progress import _engine_on_fake_claude

    eng, state_dir = _engine_on_fake_claude(
        tmp_path, fake_github, _looping_lines(12, secret=TOKEN, tail=[{"sleep": 30}])
    )
    eng.config.execution.loop_detection = LoopDetectionConfig(mode="kill")
    lines: list[str] = []
    eng.progress_output = lines.append
    started = time.monotonic()
    with pytest.raises(ExecutionTimeoutError) as excinfo:
        eng.step()
    assert time.monotonic() - started < 20
    message = str(excinfo.value)
    assert message.startswith(
        "agent 'analyze_execute' was killed: the same 2-step action cycle (Bash, Read) "
        "repeated 8 times over "
    )
    assert "with no new action" in message
    assert message.endswith("then 'resume'.")
    assert eng.state.phase == Phase.ANALYZE_EXECUTE

    shown = [line.split("] ", 1)[1] for line in lines]
    assert "loop detection kill, attempt " in shown[0]
    assert "possible loop: 2-step cycle (Bash, Read) repeated 4×" in shown
    assert shown[-1].startswith("agent killed: the same 2-step action cycle (Bash, Read)")

    files = _step_files(state_dir)
    assert files["request"]["loop_detection"]["mode"] == "kill"
    execution = files["execution"]
    assert execution["ended_by"] == "loop"
    assert execution["timed_out"] is True and execution["timeout_limit"] == "loop"
    assert execution["loop_detection"] == LoopDetectionConfig(mode="kill").as_dict()
    loop = execution["loop"]
    assert (loop["signal"], loop["action"], loop["period"], loop["repeats"]) == (
        "action_cycle",
        "killed",
        2,
        8,
    )
    assert loop["tools"] == ["Bash", "Read"]
    assert 0 <= loop["started_after_seconds"] <= loop["detected_after_seconds"]
    assert execution["loop_warnings"] >= 1
    assert execution["provider_summary"]["loop_max_cycle_repeats"] == 8


def test_e2e_warn_default_warns_at_bounded_intervals_and_never_kills(tmp_path, fake_github):
    """The default mode is ``warn``: the loop is reported in progress and in
    ``execution.json`` but the agent runs to its own end, and forty copies
    of the cycle give two warnings, not one per action."""
    from tests import claude_fake
    from tests.test_progress import ANALYZE_OK, _engine_on_fake_claude

    from .conftest import block

    eng, state_dir = _engine_on_fake_claude(
        tmp_path, fake_github, _looping_lines(40, tail=[claude_fake.result(block(ANALYZE_OK))])
    )
    assert eng.config.execution.loop_detection.mode == "warn"
    lines: list[str] = []
    eng.progress_output = lines.append
    out = eng.step()
    assert out.next_phase == "REVIEW"

    shown = [line.split("] ", 1)[1] for line in lines]
    warned = [line for line in shown if "loop" in line and not line.startswith("launching")]
    assert warned == [
        "possible loop: 2-step cycle (Bash, Read) repeated 4×",
        "loop detected, not killed (mode warn): 2-step cycle (Bash, Read) repeated 8×",
    ]
    (progress_log,) = state_dir.rglob("progress.log")
    assert progress_log.read_text(encoding="utf-8").splitlines() == lines

    execution = _step_files(state_dir)["execution"]
    assert execution["ended_by"] == "exit" and execution["timed_out"] is False
    assert execution["loop"]["action"] == "warned" and execution["loop"]["repeats"] >= 8
    assert execution["loop_warnings"] == 2
    assert execution["provider_summary"]["loop_max_cycle_repeats"] == 40


def test_e2e_no_input_result_or_digest_reaches_any_file_or_line(tmp_path, fake_github):
    """A token-shaped string in the looping tool's input and result reaches
    no progress line and no file, and neither does a digest of either."""
    from autoforge.errors import ExecutionTimeoutError
    from tests.test_progress import _engine_on_fake_claude

    eng, state_dir = _engine_on_fake_claude(
        tmp_path, fake_github, _looping_lines(12, secret=TOKEN, tail=[{"sleep": 30}])
    )
    eng.config.execution.loop_detection = LoopDetectionConfig(mode="kill")
    lines: list[str] = []
    eng.progress_output = lines.append
    with pytest.raises(ExecutionTimeoutError) as excinfo:
        eng.step()
    tool_input = digest({"command": f"run-suite-q7 {TOKEN}"})
    result = digest({"type": "tool_result", "content": f"suite-q7 failed {TOKEN}"})
    digests = [tool_input, result, action_fingerprint("Bash", tool_input, result)]
    secrets = [TOKEN.encode(), b"run-suite-q7", b"suite-q7 failed"]
    secrets += digests + [d.hex().encode() for d in digests]
    for value in secrets:
        assert value.decode("latin-1") not in str(excinfo.value)
        for line in lines:
            assert value.decode("latin-1") not in line
    files = [p for p in state_dir.rglob("*") if p.is_file()]
    assert any(p.name == "execution.json" for p in files)
    for path in files:
        data = path.read_bytes()
        for value in secrets:
            assert value not in data, (path, value)
