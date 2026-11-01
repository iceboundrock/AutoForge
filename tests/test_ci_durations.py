"""The CI duration report and its regression policy (#344).

``tests/ci_durations.py`` turns the JUnit report of the ``test`` job's pytest
into a summary, compares it with main's recent runs of the same category and
keeps main's history. These tests drive it on synthetic reports and one real
pytest run of a three-file suite, and pin what the policy promises: a
missing, unreadable or failing run never gets ``ok``; a window holds only
clean runs of a push to main in the same category, recent and one per
commit; the verdicts follow the median and p95 thresholds; a cold uv cache
is compared with cold runs only; nothing written holds a failure's message or
captured output; and ``report`` exits 0 whatever it finds. What keeps the
report from deciding the job is in ``tests/test_ci_workflow.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from tests import ci_durations
from tests.ci_durations import Policy, compare, record, summarize, window_of

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "ghp_" + "A1b2C3d4" * 4 + "Zz9y"  # a classic token's shape, never a real one


def junit(cases: list[tuple[str, str, str, float]], suite_seconds: float | None = None) -> str:
    """A pytest-shaped JUnit report of ``(classname, name, outcome, seconds)`` cases."""
    body = []
    for classname, name, outcome, seconds in cases:
        inner = {
            "passed": "",
            "failed": f"<failure message={quoteattr('assert ' + TOKEN)}>trace {TOKEN}</failure>",
            "errors": f"<error message={quoteattr('teardown')}>trace</error>",
            "skipped": f"<skipped type='pytest.skip' message={quoteattr('no zsh')}>x</skipped>",
            "xfailed": "<skipped type='pytest.xfail' message='known'/>",
            # one entry, two outcomes: the worse one counts, whatever their order
            "errors+failed": "<error message='e'>t</error><failure message='f'>t</failure>",
        }[outcome]
        body.append(
            f"<testcase classname={quoteattr(classname)} name={quoteattr(name)} "
            f"time='{seconds}'>{inner}<system-out>{TOKEN}</system-out></testcase>"
        )
    time = "" if suite_seconds is None else f" time='{suite_seconds}'"
    return (
        "<?xml version='1.0' encoding='utf-8'?><testsuites name='pytest tests'>"
        f"<testsuite name='pytest'{time} timestamp='2026-11-01T11:50:00' hostname='h'>"
        f"{''.join(body)}</testsuite></testsuites>"
    )


def github_env(**overrides: str) -> dict[str, str]:
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_JOB": "test",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_RUN_ID": "1000",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "o/r",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "ImageOS": "ubuntu24",
        "ImageVersion": "20261020.1",
    }
    env.update(overrides)
    return env


def make_summary(
    tmp_path: Path,
    seconds: float,
    *,
    days_ago: float = 1,
    n: int = 0,
    event: str = "push",
    ref: str = "refs/heads/main",
    python: str = "3.11",
    cache_hit: str = "true",
    outcome: str = "success",
    cases: list[tuple[str, str, str, float]] | None = None,
    image: str = "20261020.1",
) -> dict:
    """The summary the tool writes for a run of ``seconds``; ``n`` makes its commit and run."""
    if cases is None:
        cases = [
            ("tests.test_a", "test_one", "passed", seconds * 0.6),
            ("tests.test_b", "test_two", "passed", seconds * 0.3),
            ("tests.test_b", "test_three", "skipped", 0.0),
        ]
    path = tmp_path / f"junit-{n}-{seconds}-{days_ago}-{python}-{cache_hit}.xml"
    path.write_text(junit(cases, seconds), encoding="utf-8")
    env = github_env(
        GITHUB_SHA=f"{n:040x}",
        GITHUB_RUN_ID=str(1000 + n),
        GITHUB_EVENT_NAME=event,
        GITHUB_REF=ref,
        ImageVersion=image,
    )
    ctx = ci_durations.context_from(
        env,
        NOW - timedelta(days=days_ago),
        python=python,
        cache_hit=cache_hit,
        pytest_outcome=outcome,
        check_run=f"test (py{python})",
    )
    summary, _ = summarize(path, ctx)
    return summary


def main_history(tmp_path: Path, seconds: list[float], **kwargs) -> list[dict]:
    """Clean pushes to main, newest first: ``seconds[0]`` a day ago, the next two days ago..."""
    return [
        make_summary(tmp_path, s, days_ago=i + 1, n=i + 1, **kwargs) for i, s in enumerate(seconds)
    ]


def current(tmp_path: Path, seconds: float, **kwargs) -> dict:
    kwargs.setdefault("event", "pull_request")
    kwargs.setdefault("ref", "refs/pull/7/merge")
    return make_summary(tmp_path, seconds, days_ago=0, n=999, **kwargs)


def test_a_report_gives_every_count_each_modules_time_the_slowest_tests_and_the_skips(
    tmp_path: Path,
):
    cases = [
        ("tests.test_a", "test_one", "passed", 2.0),
        ("tests.test_a.TestK", "test_two", "passed", 5.0),
        # pytest reports an error in teardown after a passing call as a second entry
        ("tests.test_a", "test_three", "passed", 1.0),
        ("tests.test_a", "test_three", "errors", 1.5),
        ("tests.test_b", "test_four", "failed", 0.5),
        ("tests.test_b", "test_five", "skipped", 0.0),
        ("tests.test_b", "test_six", "skipped", 0.0),
        ("tests.test_b", "test_seven", "xfailed", 0.1),
        ("tests.test_b", "test_eight", "errors+failed", 0.0),
    ]
    path = tmp_path / "junit.xml"
    path.write_text(junit(cases, 12.5), encoding="utf-8")
    ctx = ci_durations.context_from(github_env(), NOW, python="3.11", pytest_outcome="failure")
    summary, tests = summarize(path, ctx)
    assert summary["status"] == "complete"
    assert summary["counts"] == {
        "tests": 8,
        "passed": 2,
        "failed": 1,
        "errors": 2,
        "skipped": 2,
        "xfailed": 1,
    }
    assert summary["suite_seconds"] == 12.5
    assert summary["test_seconds"] == pytest.approx(9.1)
    assert summary["modules"] == {"tests.test_a": 8.5, "tests.test_b": 0.6}
    assert [t["id"] for t in summary["slowest"][:2]] == [
        "tests.test_a.TestK::test_two",
        "tests.test_a::test_one",
    ]
    assert summary["skip_reasons"] == [
        {"reason": "no zsh", "count": 2},
        {"reason": "known", "count": 1},
    ]
    assert not summary["clean"]
    assert "1 failed and 2 errors" in summary["problem"]
    assert {"id": "tests.test_a::test_three", "outcome": "errors", "seconds": 1.5} in tests
    assert summary["category"] == {
        "job": "test",
        "python": "3.11",
        "selection": "default",
        "cache": "unknown",
        "runner": "github-hosted Linux X64",
    }
    assert summary["run"]["run_url"] == "https://github.com/o/r/actions/runs/1000/attempts/1"
    assert summary["run"]["image"] == "ubuntu24 20261020.1"


def test_a_real_pytest_run_is_summarized_with_its_counts(tmp_path: Path, monkeypatch):
    """The report pytest itself writes, with ``--junitxml`` as the test job passes it."""
    for name in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT"):
        monkeypatch.delenv(name, raising=False)
    temproot = tmp_path / "temproot"
    temproot.mkdir()
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(temproot))
    suite = tmp_path / "suite"
    (suite / "tests").mkdir(parents=True)
    (suite / "tests" / "__init__.py").write_text("")
    (suite / "tests" / "test_one.py").write_text(
        "import pytest\n"
        "def test_pass(): pass\n"
        "@pytest.mark.skip(reason='not here')\n"
        "def test_skip(): pass\n"
        "@pytest.mark.xfail(reason='known')\n"
        "def test_xfail(): assert False\n"
    )
    (suite / "tests" / "test_two.py").write_text("class TestK:\n    def test_m(self): pass\n")
    (suite / "tests" / "test_three.py").write_text(f"def test_fail(): assert {TOKEN!r} == ''\n")
    report = tmp_path / "durations" / "junit.xml"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}"],
        cwd=suite,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    ctx = ci_durations.context_from({}, NOW, python="3.11", pytest_outcome="failure")
    summary, tests = summarize(report, ctx)
    assert summary["counts"] == {
        "tests": 5,
        "passed": 2,
        "failed": 1,
        "errors": 0,
        "skipped": 1,
        "xfailed": 1,
    }
    assert set(summary["modules"]) == {"tests.test_one", "tests.test_two", "tests.test_three"}
    assert {"reason": "not here", "count": 1} in summary["skip_reasons"]
    assert {t["id"]: t["outcome"] for t in tests}["tests.test_two.TestK::test_m"] == "passed"
    assert TOKEN not in json.dumps([summary, tests])
    assert summary["category"]["job"] == "local" and summary["run"]["run_id"] == ""


@pytest.mark.parametrize(
    ("text", "status"),
    [
        (None, "missing"),
        ("not xml", "unreadable"),
        ("<html/>", "unreadable"),
        ("<testsuites/>", "unreadable"),
        ("<testsuite time='-1'/>", "unreadable"),
        (
            "<testsuite time='1'><testcase classname='t' name='x' time='nan'/></testsuite>",
            "unreadable",
        ),
    ],
)
def test_a_missing_or_unreadable_report_is_never_clean_and_gets_no_verdict(
    tmp_path: Path, text: str | None, status: str
):
    path = tmp_path / "junit.xml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    ctx = ci_durations.context_from(github_env(), NOW, pytest_outcome="skipped")
    summary, tests = summarize(path, ctx)
    assert (summary["status"], summary["clean"], summary["counts"], tests) == (
        status,
        False,
        None,
        [],
    )
    assert "the pytest step's outcome was skipped" in summary["problem"]
    comparison = compare(summary, main_history(tmp_path, [100.0] * 10))
    assert comparison["verdict"] == "no-data"
    assert "Verdict: no-data" in ci_durations.render(summary, comparison)


@pytest.mark.parametrize(
    ("outcome", "cases", "clean"),
    [
        ("success", None, True),
        ("", None, True),  # by hand: no step outcome, the counts decide
        ("failure", None, False),  # pytest failed with no failed test: exit 5, an internal error
        ("success", [], False),  # no test ran
        ("success", [("tests.test_a", "test_x", "failed", 1.0)], False),
        ("success", [("tests.test_a", "test_x", "errors", 1.0)], False),
    ],
)
def test_only_a_clean_run_is_compared(tmp_path: Path, outcome: str, cases, clean: bool):
    summary = current(tmp_path, 100.0, outcome=outcome, cases=cases)
    assert summary["clean"] is clean
    verdict = compare(summary, main_history(tmp_path, [100.0] * 10))["verdict"]
    assert verdict == ("ok" if clean else "not-comparable")


@pytest.mark.parametrize(
    ("seconds", "verdict"),
    [
        (110.0, "ok"),
        (120.4, "ok"),  # the median is 109.5: slower above 120.45
        (120.5, "slower"),
        (129.8, "slower"),  # the p95 (nearest rank of 20) is 118: regression above 129.8
        (129.9, "regression"),
        (98.6, "ok"),  # faster below 98.55
        (98.5, "faster"),
    ],
)
def test_the_verdicts_follow_the_median_and_p95_thresholds(
    tmp_path: Path, seconds: float, verdict: str
):
    history = main_history(tmp_path, [100.0 + i for i in range(20)])
    comparison = compare(current(tmp_path, seconds), history)
    assert comparison["verdict"] == verdict
    assert (comparison["samples"], comparison["median"], comparison["p95"]) == (20, 109.5, 118.0)
    assert comparison["thresholds"] == {"regression": 129.8, "slower": 120.45, "faster": 98.55}


def test_fewer_main_runs_than_the_minimum_give_no_verdict(tmp_path: Path):
    slow = current(tmp_path, 500.0)
    few = compare(slow, main_history(tmp_path, [100.0] * (ci_durations.MIN_SAMPLES - 1)))
    assert few["verdict"] == "insufficient-baseline"
    assert few["thresholds"] is None and few["median"] is None
    assert compare(slow, main_history(tmp_path, [100.0] * ci_durations.MIN_SAMPLES))["verdict"] == (
        "regression"
    )


def test_the_window_holds_only_clean_main_pushes_of_the_category_recent_and_one_per_commit(
    tmp_path: Path,
):
    now = current(tmp_path, 100.0)
    keep = make_summary(tmp_path, 101.0, days_ago=2, n=1)
    excluded = [
        make_summary(tmp_path, 1.0, days_ago=1, n=2, event="pull_request"),
        make_summary(tmp_path, 1.0, days_ago=1, n=3, ref="refs/heads/other"),
        make_summary(tmp_path, 1.0, days_ago=1, n=4, outcome="failure"),
        make_summary(tmp_path, 1.0, days_ago=1, n=5, python="3.12"),
        make_summary(tmp_path, 1.0, days_ago=1, n=6, cache_hit="false"),
        make_summary(tmp_path, 1.0, days_ago=ci_durations.MAX_AGE_DAYS + 1, n=7),
        make_summary(tmp_path, 1.0, days_ago=-1, n=8),  # recorded after the current run
        make_summary(tmp_path, 1.0, days_ago=1, n=999),  # the current run's own commit
        make_summary(tmp_path, 1.0, days_ago=1, n=9, cases=[]),  # no test: not clean
        {**make_summary(tmp_path, 1.0, n=10), "suite_seconds": "fast"},  # hand-edited
    ]
    # Two runs of one commit: the newer one stands for it.
    older = make_summary(tmp_path, 1.0, days_ago=3, n=11)
    newer = {**make_summary(tmp_path, 102.0, days_ago=2.5, n=11)}
    window = window_of([keep, *excluded, older, newer], now, Policy())
    assert window == [keep, newer]
    many = main_history(tmp_path, [100.0] * (ci_durations.WINDOW + 5))
    assert window_of(many, now, Policy()) == many[: ci_durations.WINDOW]


def test_a_cold_cache_run_is_compared_with_cold_runs_and_shown_beside_warm_ones(tmp_path: Path):
    warm = main_history(tmp_path, [100.0] * 8)
    cold = [
        make_summary(tmp_path, 130.0, days_ago=1.5 + i, n=50 + i, cache_hit="false")
        for i in range(2)
    ]
    comparison = compare(current(tmp_path, 131.0, cache_hit="false"), warm + cold)
    assert comparison["verdict"] == "insufficient-baseline"
    assert comparison["samples"] == 2
    assert comparison["other_cache"] == [{"cache": "warm", "samples": 8, "median": 100.0}]
    text = ci_durations.render(current(tmp_path, 131.0, cache_hit="false"), comparison)
    assert "For reference, not a verdict: 8 runs of main with a warm uv cache" in text
    warm_run = compare(current(tmp_path, 101.0), warm + cold)
    assert (warm_run["verdict"], warm_run["samples"]) == ("ok", 8)
    assert warm_run["other_cache"] == [{"cache": "cold", "samples": 2, "median": 130.0}]


def test_the_notes_name_fewer_tests_more_skips_and_another_runner_image(tmp_path: Path):
    history = main_history(tmp_path, [100.0] * 6)
    fewer = [
        ("tests.test_a", "test_one", "passed", 30.0),
        ("tests.test_b", "test_two", "skipped", 0.0),
        ("tests.test_b", "test_three", "skipped", 0.0),
    ]
    run = current(tmp_path, 60.0, cases=fewer, image="20261101.1")
    comparison = compare(run, history)
    assert comparison["verdict"] == "faster"
    sha = f"{1:040x}"[:12]
    assert comparison["notes"] == [
        f"1 more skipped tests than main's run at {sha}",
        f"the runner image is ubuntu24 20261101.1; main's run at {sha} had ubuntu24 20261020.1",
    ]
    lost = [("tests.test_a", "test_one", "passed", 30.0)]
    notes = compare(current(tmp_path, 60.0, cases=lost), history)["notes"]
    assert notes[0].startswith(f"2 fewer tests than main's run at {sha}: a run that tests less")


def test_the_modules_that_grew_most_are_named(tmp_path: Path):
    history = main_history(tmp_path, [100.0] * 6)
    grown = [
        ("tests.test_a", "test_one", "passed", 60.0),
        ("tests.test_b", "test_two", "passed", 90.0),
        ("tests.test_b", "test_three", "skipped", 0.0),
        ("tests.test_new", "test_x", "passed", 5.0),
    ]
    comparison = compare(current(tmp_path, 160.0, cases=grown), history)
    assert comparison["verdict"] == "regression"
    assert comparison["modules"] == [
        {"module": "tests.test_b", "seconds": 90.0, "median": 30.0, "delta": 60.0}
    ]


def test_record_adds_only_a_clean_push_to_main_and_keeps_one_sample_per_commit(tmp_path: Path):
    history = main_history(tmp_path, [100.0] * 3)
    pr_run = current(tmp_path, 100.0)
    kept, note = record(history, pr_run)
    assert kept == sorted(history, key=lambda s: s["run"]["recorded_at"])
    assert note == "this run was not added to main's history: it is not a run of a push to main"
    failed = make_summary(tmp_path, 100.0, days_ago=0, n=20, outcome="failure")
    assert "the pytest step's outcome was failure" in record(history, failed)[1]
    first = make_summary(tmp_path, 100.0, days_ago=0.5, n=21)
    rerun = make_summary(tmp_path, 105.0, days_ago=0, n=21)
    after, note = record(record(history, first)[0], rerun)
    assert note == "this run was added to main's history"
    ours = [s for s in after if s["run"]["sha"] == f"{21:040x}"]
    assert [s["suite_seconds"] for s in ours] == [105.0]
    assert "slowest" not in ours[0] and "skip_reasons" not in ours[0]
    capped, _ = record(main_history(tmp_path, [100.0] * 10), rerun, cap=4)
    assert len(capped) == 4 and capped[-1]["run"]["sha"] == f"{21:040x}"
    assert record([{"schema": "something else"}, *history], pr_run)[0] == kept


@pytest.mark.parametrize(
    ("content", "note"),
    [
        (None, "no history of main was restored"),
        ("{", "it does not read"),
        ('{"schema": "x", "samples": []}', "it is not a autoforge-ci-durations-history/1 file"),
        ('{"schema": "autoforge-ci-durations-history/1"}', "it holds no list of samples"),
    ],
)
def test_a_history_that_does_not_read_is_ignored_with_a_note(
    tmp_path: Path, content: str | None, note: str
):
    path = tmp_path / "history.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    samples, said = ci_durations.load_history(path)
    assert samples == [] and note in said


def test_a_malformed_sample_of_the_history_is_dropped_and_counted(tmp_path: Path):
    history = main_history(tmp_path, [100.0] * 2)
    path = tmp_path / "history.json"
    broken = {**history[0], "clean": "yes"}
    path.write_text(ci_durations.history_text([*history, broken, 7]), encoding="utf-8")
    samples, note = ci_durations.load_history(path)
    assert samples == history
    assert note == "2 malformed samples of the history were ignored"


def _report(tmp_path: Path, env: dict[str, str], junit_text: str | None) -> tuple[int, Path]:
    directory = tmp_path / "durations"
    directory.mkdir(exist_ok=True)
    report = directory / "junit.xml"
    report.unlink(missing_ok=True)
    if junit_text is not None:
        report.write_text(junit_text, encoding="utf-8")
    argv = ["report", "--dir", str(directory), "--python", "3.11", "--cache-hit", "true"]
    argv += ["--pytest-outcome", "success", "--check-run", "test (py3.11)"]
    return ci_durations.main(argv, env, NOW), directory


def test_report_records_pushes_to_main_and_gives_a_pull_request_its_verdict(tmp_path: Path):
    summary_file = tmp_path / "step-summary.md"
    cases = [("tests.test_a", "test_one", "passed", 90.0)]
    for n in range(1, 7):
        env = github_env(
            GITHUB_SHA=f"{n:040x}",
            GITHUB_RUN_ID=str(n),
            GITHUB_STEP_SUMMARY=str(summary_file),
        )
        code, directory = _report(tmp_path, env, junit(cases, 100.0 + n))
        assert code == 0
    history = json.loads((directory / ci_durations.HISTORY_FILE).read_text(encoding="utf-8"))
    assert len(history["samples"]) == 6
    pr = github_env(
        GITHUB_SHA="f" * 40,
        GITHUB_RUN_ID="77",
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_REF="refs/pull/7/merge",
        GITHUB_STEP_SUMMARY=str(summary_file),
    )
    before = (directory / ci_durations.HISTORY_FILE).read_bytes()
    code, directory = _report(tmp_path, pr, junit(cases, 150.0))
    assert code == 0
    assert (directory / ci_durations.HISTORY_FILE).read_bytes() == before
    out = directory / ci_durations.REPORT_DIR
    assert sorted(p.name for p in out.iterdir()) == [
        "comparison.json",
        "summary.json",
        "summary.md",
        "tests.json",
    ]
    comparison = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert (comparison["verdict"], comparison["samples"]) == ("regression", 6)
    markdown = (out / "summary.md").read_text(encoding="utf-8")
    assert summary_file.read_text(encoding="utf-8").endswith(markdown)
    assert summary_file.read_text(encoding="utf-8").count("### Test durations") == 7
    assert "**Verdict: regression**" in markdown
    assert "`test (py3.11)`, aggregated by the required check `ci`" in markdown
    assert "Report only: this never decides the job" in markdown
    assert "[run 77, attempt 1](https://github.com/o/r/actions/runs/77/attempts/1)" in markdown


def test_report_on_main_keeps_the_history_when_the_run_is_not_clean(tmp_path: Path):
    _report(tmp_path, github_env(), junit([("tests.test_a", "t", "passed", 1.0)], 1.0))
    code, directory = _report(
        tmp_path,
        github_env(GITHUB_SHA="b" * 40, GITHUB_RUN_ID="2"),
        junit([("tests.test_a", "t", "failed", 1.0)], 1.0),
    )
    assert code == 0
    history = json.loads((directory / ci_durations.HISTORY_FILE).read_text(encoding="utf-8"))
    assert [s["run"]["sha"] for s in history["samples"]] == ["a" * 40]
    comparison = json.loads((directory / "report" / "comparison.json").read_text("utf-8"))
    assert comparison["verdict"] == "not-comparable"
    assert comparison["notes"][-1].startswith("this run was not added to main's history: 1 failed")


def test_report_exits_zero_as_ci_runs_it_whatever_it_finds(tmp_path: Path):
    """The workflow's command, on a bare interpreter: no report and a corrupt history."""
    directory = tmp_path / "durations"
    (directory / "history").mkdir(parents=True)
    (directory / ci_durations.HISTORY_FILE).write_text("{ not json", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), **github_env()}
    env["GITHUB_STEP_SUMMARY"] = str(tmp_path / "summary.md")
    completed = subprocess.run(
        [sys.executable, "-I", "tests/ci_durations.py", "report", "--dir", str(directory)]
        + ["--python", "3.11", "--cache-hit", "", "--pytest-outcome", "cancelled"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "**Verdict: no-data**: no JUnit report at" in completed.stdout
    assert "the pytest step's outcome was cancelled" in completed.stdout
    assert "it does not read" in completed.stdout
    history = json.loads((directory / ci_durations.HISTORY_FILE).read_text(encoding="utf-8"))
    assert history == {"schema": ci_durations.HISTORY_SCHEMA, "samples": []}


def test_nothing_written_holds_a_failure_message_or_captured_output(tmp_path: Path):
    cases = [
        ("tests.test_a", "test_one", "failed", 1.0),
        ("tests.test_a", "test_two", "errors", 1.0),
        ("tests.test_a", "test_three", "passed", 1.0),
    ]
    _, directory = _report(tmp_path, github_env(), junit(cases, 3.0))
    assert TOKEN in (directory / "junit.xml").read_text(encoding="utf-8")
    for path in [*(directory / "report").iterdir(), directory / ci_durations.HISTORY_FILE]:
        text = path.read_text(encoding="utf-8")
        assert TOKEN not in text and "trace" not in text, path


def test_compare_by_hand_reads_downloaded_artifacts_and_fails_only_when_asked(
    tmp_path: Path, capsys
):
    artifacts = tmp_path / "artifacts"
    for i, summary in enumerate(main_history(tmp_path, [100.0] * 3)):
        (artifacts / f"durations-py3.11-attempt{i}").mkdir(parents=True)
        (artifacts / f"durations-py3.11-attempt{i}" / "summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
    history = main_history(tmp_path, [100.0] * 6)[3:]
    (artifacts / "history.json").write_text(ci_durations.history_text(history), "utf-8")
    (artifacts / "junk").mkdir()
    (artifacts / "junk" / "summary.json").write_text('{"schema": 1}', encoding="utf-8")
    now = tmp_path / "now.json"
    now.write_text(json.dumps(current(tmp_path, 200.0)), encoding="utf-8")
    out = tmp_path / "comparison.json"
    argv = ["compare", "--current", str(now), "--baseline", str(artifacts), "--json-out", str(out)]
    assert ci_durations.main(argv) == 0
    comparison = json.loads(out.read_text(encoding="utf-8"))
    assert (comparison["verdict"], comparison["samples"]) == ("regression", 6)
    assert any("neither a summary nor a history" in note for note in comparison["notes"])
    assert "**Verdict: regression**" in capsys.readouterr().out
    assert ci_durations.main([*argv, "--fail-on-regression"]) == 1
    assert ci_durations.main([*argv, "--fail-on-regression", "--min-samples", "7"]) == 0
    now.write_text('{"schema": "other"}', encoding="utf-8")
    assert ci_durations.main(argv) == 2


def test_markdown_table_cells_stay_in_their_cell(tmp_path: Path):
    cases = [("tests.test_a", "test_x[a|b\nc]", "skipped", 1.0)]
    path = tmp_path / "junit.xml"
    path.write_text(junit(cases, 1.0).replace("no zsh", "needs | a\nshell"), encoding="utf-8")
    summary, _ = summarize(path, ci_durations.context_from(github_env(), NOW))
    text = ci_durations.render(summary, None)
    assert "| tests.test_a::test_x[a\\|b c] | 1.00 |" in text
    assert "| 1 | needs \\| a shell |" in text


def test_a_long_skip_reason_is_cut_to_its_bound(tmp_path: Path):
    path = tmp_path / "junit.xml"
    reason = "x" * (ci_durations.REASON_LIMIT + 50)
    path.write_text(
        junit([("tests.test_a", "t", "skipped", 0.0)], 1.0).replace("no zsh", reason), "utf-8"
    )
    summary, _ = summarize(path, ci_durations.context_from({}, NOW))
    (kept,) = summary["skip_reasons"]
    assert len(kept["reason"]) == ci_durations.REASON_LIMIT and kept["reason"].endswith("…")


def test_the_cache_state_and_the_runner_are_read_as_the_category_says():
    assert [ci_durations.cache_state(v) for v in ("true", "false", "", "maybe")] == [
        "warm",
        "cold",
        "unknown",
        "unknown",
    ]
    local = ci_durations.context_from({}, NOW, python="3.12")
    assert local.runner.startswith("local ") and local.run_url == "" and local.job == "local"
    hosted = ci_durations.context_from(github_env(RUNNER_ENVIRONMENT="self-hosted"), NOW)
    assert hosted.runner == "self-hosted Linux X64"
