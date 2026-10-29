"""The serial/xdist benchmark of the suite (``tests/parallel_bench.py``, #340).

The tool itself runs the whole suite many times and is run by hand; here its
parsing, reconciliation and statistics run on synthetic reports, and one
serial and one two-worker run of a three-test suite under ``tmp_path`` check
that both report the same tests with the same outcomes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import parallel_bench
from tests.parallel_bench import (
    Config,
    Run,
    collected_counts,
    junit_file_seconds,
    junit_outcomes,
    p95,
    parse_config,
    reconcile,
    summarize,
    summary_counts,
)

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
<testcase classname="tests.test_a" name="test_passes" time="1.5"/>
<testcase classname="tests.test_a" name="test_skips[x]" time="0.5">
  <skipped type="pytest.skip" message="no backend">no backend</skipped></testcase>
<testcase classname="tests.test_b" name="test_fails" time="2.0">
  <failure message="assert 1 == 2">assert 1 == 2</failure></testcase>
<testcase classname="tests.test_b" name="test_teardown" time="0.25"/>
<testcase classname="tests.test_b" name="test_teardown" time="0.0">
  <error message="error in teardown">boom</error></testcase>
</testsuite></testsuites>
"""


def test_a_configuration_is_serial_or_workers_and_a_distribution():
    assert parse_config("serial") == Config("serial")
    assert parse_config("serial").pytest_args() == []
    assert parse_config("n4-worksteal") == Config("n4-worksteal", 4, "worksteal")
    assert parse_config("n2-loadfile").pytest_args() == ["-n", "2", "--dist=loadfile"]


@pytest.mark.parametrize("name", ["n0-load", "n2-each", "n2load", "4", "auto", "n-load", ""])
def test_any_other_configuration_is_refused(name):
    with pytest.raises(ValueError, match="unknown configuration"):
        parse_config(name)


def test_every_test_of_a_junit_report_keeps_its_worst_outcome():
    assert junit_outcomes(REPORT) == {
        "tests.test_a::test_passes": "passed",
        "tests.test_a::test_skips[x]": "skipped",
        "tests.test_b::test_fails": "failure",
        "tests.test_b::test_teardown": "error",
    }


def test_the_time_of_each_module_is_summed():
    assert junit_file_seconds(REPORT) == {"tests.test_a": 2.0, "tests.test_b": 2.25}


@pytest.mark.parametrize(
    ("output", "counts"),
    [
        (
            "....\n11550 passed, 6 skipped, 1 deselected in 612.34s (0:10:12)\n",
            {"passed": 11550, "skipped": 6, "deselected": 1},
        ),
        (
            "==== 1 failed, 2 passed, 3 warnings, 1 error in 1.00s ====\n",
            {"failed": 1, "passed": 2, "warning": 3, "error": 1},
        ),
        ("5 tests collected in 0.10s\n", {}),
        ("no summary here\n", {}),
    ],
)
def test_the_last_summary_line_gives_the_counts(output, counts):
    assert summary_counts(output) == counts


@pytest.mark.parametrize(
    ("output", "counts"),
    [
        (
            "tests/test_a.py: 3\n\n11579/11580 tests collected (1 deselected) in 2.76s\n",
            {"collected": 11580, "selected": 11579, "deselected": 1},
        ),
        ("1 test collected in 0.01s\n", {"collected": 1, "selected": 1, "deselected": 0}),
        ("2 passed in 0.10s\n", {}),
    ],
)
def test_the_collect_only_summary_gives_what_was_collected_and_selected(output, counts):
    assert collected_counts(output) == counts


def test_reconciliation_names_every_lost_added_and_changed_test():
    reference = {"a": "passed", "b": "skipped", "c": "passed"}
    run = {"a": "passed", "b": "passed", "d": "passed"}
    assert reconcile(reference, run) == {
        "missing": ["c"],
        "extra": ["d"],
        "changed": [["b", "skipped", "passed"]],
    }
    assert not any(reconcile(reference, dict(reference)).values())


def test_p95_is_the_nearest_rank_so_few_repeats_report_the_slowest():
    assert p95([3.0, 1.0, 2.0]) == 3.0
    assert p95([7.0]) == 7.0
    assert p95([float(n) for n in range(1, 21)]) == 19.0


def _run(config: str, seconds: float, returncode: int = 0, **differences) -> Run:
    run = Run(config, 0, seconds, returncode, {}, (0, 0, 0), (0, 0, 0), "", "")
    run.differences = {"missing": [], "extra": [], "changed": [], **differences}
    return run


def test_a_run_that_differs_or_fails_is_not_clean_and_still_counts_towards_the_times():
    runs = [
        _run("serial", 100.0),
        _run("serial", 120.0),
        _run("serial", 10.0, returncode=1),
        _run("n2-load", 50.0),
        _run("n2-load", 60.0, changed=[["t", "passed", "failure"]]),
    ]
    rows = summarize(runs, [Config("serial"), Config("n2-load", 2, "load")])
    assert rows[0] == {
        "config": "serial",
        "runs": 3,
        "clean": 2,
        "median": 100.0,
        "p95": 120.0,
        "min": 10.0,
        "speedup": 1.0,
    }
    assert rows[1]["clean"] == 1
    assert rows[1]["speedup"] == round(100.0 / 55.0, 2)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--config", "n2-load"], "the serial configuration is the reference"),
        (["--config", "serial", "--config", "n2-each"], "unknown configuration"),
        (["--config", "serial", "--repeats", "0"], "--repeats must be at least 1"),
        (["--config", "serial", "--once", "serial"], "given more than once"),
        (["--config", "serial", "--once", "n4"], "unknown configuration"),
    ],
)
def test_the_tool_refuses_before_running_anything(tmp_path, capsys, argv, message):
    with pytest.raises(SystemExit) as exited:
        parallel_bench.main([*argv, "--out", str(tmp_path / "out")])
    assert exited.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_a_serial_and_a_two_worker_run_report_the_same_tests(tmp_path: Path, monkeypatch):
    for name in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT"):
        monkeypatch.delenv(name, raising=False)
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_tiny.py").write_text(
        "import pytest\n"
        "def test_one(): pass\n"
        "def test_two(): pass\n"
        "def test_three(): pytest.skip('by design')\n"
    )
    out = tmp_path / "out"
    out.mkdir()
    runs = [
        parallel_bench.run_pytest(parse_config(name), 0, out, [], cwd=suite)
        for name in ("serial", "n2-load")
    ]
    reports = [junit_outcomes(Path(run.junit).read_text(encoding="utf-8")) for run in runs]
    assert reports[0] == {
        "test_tiny::test_one": "passed",
        "test_tiny::test_two": "passed",
        "test_tiny::test_three": "skipped",
    }
    assert not any(reconcile(reports[0], reports[1]).values())
    for run in runs:
        assert run.returncode == 0 and run.counts == {"passed": 2, "skipped": 1}
    assert "created: 2/2 workers" in Path(runs[1].log).read_text(encoding="utf-8")


def _fake_runs(monkeypatch, commit: str = "c0") -> list[str]:
    """Stand-ins for the host and pytest: every run passes one test; the list names each run."""
    made: list[str] = []
    monkeypatch.setattr(
        parallel_bench,
        "provenance",
        lambda: {key: commit if key == "commit" else "same" for key in parallel_bench.COMPARABLE},
    )
    monkeypatch.setattr(parallel_bench, "collect", lambda extra: {"selected": 1})

    def run_pytest(config, repeat, out, extra):
        made.append(f"{config.name}-{repeat}")
        junit = out / f"{config.name}-{repeat}.xml"
        junit.write_text('<testsuite><testcase classname="tests.t" name="test_x"/></testsuite>')
        return Run(
            config.name,
            repeat,
            10.0 + repeat,
            0,
            {"passed": 1},
            (1, 1, 1),
            (2, 2, 2),
            str(junit),
            "",
        )

    monkeypatch.setattr(parallel_bench, "run_pytest", run_pytest)
    return made


def test_the_same_command_again_keeps_every_recorded_run(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    argv = ["--config", "serial", "--config", "n2-load", "--repeats", "2", "--out", str(out)]
    made = _fake_runs(monkeypatch)
    assert parallel_bench.main(argv) == 0
    assert made == ["serial-0", "n2-load-0", "serial-1", "n2-load-1"]
    first = (out / "results.json").read_text(encoding="utf-8")
    capsys.readouterr()

    made.clear()
    (out / "n2-load-1.json").unlink()  # interrupted during this run
    assert parallel_bench.main(argv) == 0
    assert made == ["n2-load-1"]
    assert capsys.readouterr().err.count("(kept)") == 3
    assert (out / "results.json").read_text(encoding="utf-8") == first


def test_an_out_directory_of_another_commit_is_refused(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    argv = ["--config", "serial", "--repeats", "1", "--out", str(out)]
    _fake_runs(monkeypatch, commit="c0")
    assert parallel_bench.main(argv) == 0
    made = _fake_runs(monkeypatch, commit="c1")
    with pytest.raises(SystemExit) as exited:
        parallel_bench.main(argv)
    assert exited.value.code == 2
    assert "holds runs measured with another commit" in capsys.readouterr().err
    assert made == []
