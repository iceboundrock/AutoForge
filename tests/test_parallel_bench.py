"""The serial/xdist benchmark of the suite (``tests/parallel_bench.py``, #340).

The tool itself runs the whole suite many times and is run by hand; here its
parsing, reconciliation and statistics run on synthetic reports, and one
serial and one two-worker run of a three-test suite under ``tmp_path`` check
that both report the same tests with the same outcomes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests import parallel_bench
from tests.conftest import git_repo
from tests.parallel_bench import (
    Config,
    Run,
    collected_counts,
    junit_file_seconds,
    junit_outcomes,
    p95,
    parse_config,
    reconcile,
    report,
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
    assert parse_config("serial").mode() == {"workers": 0, "dist": "no"}
    assert parse_config("n4-worksteal") == Config("n4-worksteal", 4, "worksteal")
    assert parse_config("n2-loadfile").pytest_args() == ["-n", "2", "--dist=loadfile"]
    assert parse_config("n2-loadfile").mode() == {"workers": 2, "dist": "loadfile"}


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


CONFIGS = [Config("serial"), Config("n2-load", 2, "load")]


def test_only_a_clean_run_is_timed():
    runs = [
        _run("serial", 100.0),
        _run("serial", 120.0),
        _run("serial", 10.0, returncode=1),  # stopped early
        _run("n2-load", 50.0),
        _run("n2-load", 20.0, missing=["t"]),  # finished sooner by losing tests
    ]
    rows = summarize(runs, CONFIGS)
    assert rows == [
        {
            "config": "serial",
            "runs": 3,
            "clean": 2,
            "median": 110.0,
            "p95": 120.0,
            "min": 100.0,
            "speedup": 1.0,
        },
        {
            "config": "n2-load",
            "runs": 2,
            "clean": 1,
            "median": 50.0,
            "p95": 50.0,
            "min": 50.0,
            "speedup": 2.2,
        },
    ]


def test_a_configuration_without_a_clean_run_has_no_times():
    runs = [_run("serial", 100.0), _run("n2-load", 30.0, changed=[["t", "passed", "failure"]])]
    rows = summarize(runs, CONFIGS)
    assert rows[1] == {
        "config": "n2-load",
        "runs": 1,
        "clean": 0,
        "median": None,
        "p95": None,
        "min": None,
        "speedup": None,
    }
    assert "| n2-load | 1 | 0 | — | — | — | — |" in report(rows, runs, {})


def test_without_a_clean_serial_run_no_configuration_has_a_speedup():
    rows = summarize([_run("serial", 10.0, returncode=1), _run("n2-load", 50.0)], CONFIGS)
    assert [(row["median"], row["speedup"]) for row in rows] == [(None, None), (50.0, None)]


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


def _child_pytest(monkeypatch, tmp_path: Path) -> None:
    """A nested pytest's environment: none of this session's, the plugin importable,
    and a temporary root of its own.

    A run loads ``tests.parallel_bench`` as a plugin, which the benchmark's
    working directory provides and a suite under ``tmp_path`` does not. A
    pytest that ends removes the older ``pytest-<n>`` directories of its
    temporary root that hold no lock, and a session under
    ``tmp_path_retention_policy=none`` (as the benchmark runs it) holds none:
    in the shared root, five nested runs removed this session's own.
    """
    for name in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT"):
        monkeypatch.delenv(name, raising=False)
    path = [str(parallel_bench.REPO_ROOT), os.environ.get("PYTHONPATH", "")]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, path)))
    temproot = tmp_path / "temproot"
    temproot.mkdir()
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(temproot))


def test_a_serial_and_a_two_worker_run_report_the_same_tests(tmp_path: Path, monkeypatch):
    _child_pytest(monkeypatch, tmp_path)
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
        assert run.mode == parse_config(run.config).mode()
    assert "created: 2/2 workers" in Path(runs[1].log).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("config", "addopts", "extra", "ran"),
    [
        ("serial", "-n 2", [], {"workers": 2, "dist": "load"}),
        ("n2-load", "", ["-n", "0"], {"workers": 0, "dist": "no"}),
        # -d sets --dist=load after every --dist, whatever their order.
        ("n2-worksteal", "-d", [], {"workers": 2, "dist": "load"}),
    ],
)
def test_worker_options_from_the_environment_or_the_arguments_show_in_the_mode(
    tmp_path: Path, monkeypatch, config, addopts, extra, ran
):
    _child_pytest(monkeypatch, tmp_path)
    monkeypatch.setenv("PYTEST_ADDOPTS", addopts)
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_tiny.py").write_text("def test_one(): pass\ndef test_two(): pass\n")
    out = tmp_path / "out"
    out.mkdir()
    run = parallel_bench.run_pytest(parse_config(config), 0, out, extra, cwd=suite)
    assert run.returncode == 0
    assert run.mode == ran != parse_config(config).mode()


def _fake_runs(
    monkeypatch,
    commit: str = "c0",
    failing: tuple[str, ...] = (),
    no_report: tuple[str, ...] = (),
    changes: list[str] | None = None,
    ran: dict[str, dict[str, object] | None] | None = None,
    **facts: object,
) -> list[str]:
    """Stand-ins for the host, its worktree and pytest.

    The host's provenance is ``commit`` and ``facts``, the worktree is
    ``commit`` with ``changes`` beside it (none by default; the list is read
    at every check), and every run passes one test, or fails it when
    ``failing`` names the run (``serial-0``), or cannot start and leaves no
    report or mode when ``no_report`` does. A run runs as its configuration
    names unless ``ran`` gives the mode it records. The list names each run
    made.
    """
    made: list[str] = []
    beside = [] if changes is None else changes
    monkeypatch.setattr(
        parallel_bench,
        "provenance",
        lambda: {
            **{key: "same" for key in parallel_bench.COMPARABLE},
            "commit": commit,
            **facts,
        },
    )
    monkeypatch.setattr(parallel_bench, "source", lambda: (commit, list(beside)))
    monkeypatch.setattr(parallel_bench, "collect", lambda extra: {"selected": 1})

    def run_pytest(config, repeat, out, extra):
        stem = f"{config.name}-{repeat}"
        made.append(stem)
        junit = out / f"{stem}.xml"
        junit.unlink(missing_ok=True)
        if stem in no_report:
            return Run(config.name, repeat, 0.5, 4, {}, (1, 1, 1), (1, 1, 1), str(junit), "x.log")
        mode = (ran or {}).get(stem, config.mode())
        failed = stem in failing
        junit.write_text(
            '<testsuite><testcase classname="tests.t" name="test_x">'
            + ('<failure message="boom"/>' if failed else "")
            + "</testcase></testsuite>"
        )
        return Run(
            config.name,
            repeat,
            10.0 + repeat,
            int(failed),
            {"failed" if failed else "passed": 1},
            (1, 1, 1),
            (2, 2, 2),
            str(junit),
            "",
            mode,
        )

    monkeypatch.setattr(parallel_bench, "run_pytest", run_pytest)
    return made


def _results(out: Path) -> dict:
    return json.loads((out / "results.json").read_text(encoding="utf-8"))


def test_a_failed_first_serial_run_is_no_reference_and_is_not_timed(tmp_path, monkeypatch):
    out = tmp_path / "out"
    _fake_runs(monkeypatch, failing=("serial-0",))
    argv = ["--config", "serial", "--config", "n2-load", "--repeats", "2", "--out", str(out)]
    assert parallel_bench.main(argv) == 1
    results = _results(out)
    assert results["provenance"]["reference"] == "serial #1"
    assert results["runs"][0]["differences"]["changed"] == [
        ["tests.t::test_x", "passed", "failure"]
    ]
    assert [(row["clean"], row["median"], row["speedup"]) for row in results["summary"]] == [
        (1, 11.0, 1.0),
        (2, 10.5, round(11.0 / 10.5, 2)),
    ]


def test_without_a_passing_serial_run_nothing_is_reconciled_or_timed(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    _fake_runs(monkeypatch, failing=("serial-0",))
    argv = ["--config", "serial", "--config", "n2-load", "--repeats", "1", "--out", str(out)]
    assert parallel_bench.main(argv) == 1
    results = _results(out)
    assert results["provenance"]["reference"] == "none: no serial run passed"
    assert [run["differences"] for run in results["runs"]] == [
        {"unreconciled": ["no serial run passed to reconcile it with"]}
    ] * 2
    assert [(row["clean"], row["median"], row["speedup"]) for row in results["summary"]] == [
        (0, None, None)
    ] * 2
    assert "  - unreconciled: no serial run passed" in capsys.readouterr().out


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


@pytest.mark.parametrize("report_text", [None, "", '<testsuite><testcase name="t"', "\xff\xfe"])
def test_a_missing_or_malformed_report_reads_as_none(tmp_path, report_text):
    junit = tmp_path / "run.xml"
    if report_text is not None:
        junit.write_bytes(report_text.encode("latin-1"))
    assert parallel_bench.read_outcomes(junit) is None


def test_a_pytest_that_cannot_start_leaves_no_report_not_an_earlier_ones(tmp_path, monkeypatch):
    _child_pytest(monkeypatch, tmp_path)
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_tiny.py").write_text("def test_one(): pass\n")
    out = tmp_path / "out"
    out.mkdir()
    stale = out / "serial-0.xml"
    stale.write_text('<testsuite><testcase classname="test_tiny" name="test_one"/></testsuite>')
    stale_mode = out / "serial-0.mode.json"
    stale_mode.write_text('{"workers": 0, "dist": "no"}')
    run = parallel_bench.run_pytest(parse_config("serial"), 0, out, ["--no-such-option"], cwd=suite)
    assert run.returncode == 4  # pytest's usage error
    assert not stale.exists() and not stale_mode.exists()
    assert parallel_bench.read_outcomes(Path(run.junit)) is None
    assert run.mode is None


def test_a_run_without_a_report_is_failed_unrecorded_and_runs_again(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    argv = ["--config", "serial", "--config", "n2-load", "--repeats", "1", "--out", str(out)]
    _fake_runs(monkeypatch, no_report=("n2-load-0",))
    assert parallel_bench.main(argv) == 1
    assert not (out / "n2-load-0.json").exists()
    results = _results(out)
    assert results["runs"][1]["differences"] == {
        "unreconciled": ["no readable JUnit report: see x.log"]
    }
    assert [(row["clean"], row["median"]) for row in results["summary"]] == [(1, 10.0), (0, None)]
    assert "n2-load #0: 0.5s exit 4 (no JUnit report" in capsys.readouterr().err

    made = _fake_runs(monkeypatch)
    assert parallel_bench.main(argv) == 0
    assert made == ["n2-load-0"]


@pytest.mark.parametrize(
    ("stem", "ran", "message"),
    [
        (
            "serial-0",
            {"workers": 4, "dist": "load"},
            "serial-0 ran on 4 workers (--dist=load), not as serial: something besides the "
            "configuration set pytest-xdist's options (PYTEST_ADDOPTS,",
        ),
        ("n2-load-0", {"workers": 0, "dist": "no"}, "n2-load-0 ran serially, not as n2-load: "),
        (
            "n2-load-0",
            {"workers": 2, "dist": "worksteal"},
            "n2-load-0 ran on 2 workers (--dist=worksteal), not as n2-load: ",
        ),
        (
            "n2-load-0",
            None,
            "n2-load-0 left a JUnit report but no record of how pytest-xdist ran it: "
            "the tests.parallel_bench plugin did not run",
        ),
    ],
)
def test_a_run_that_did_not_run_as_its_configuration_stops_the_benchmark(
    tmp_path, monkeypatch, capsys, stem, ran, message
):
    out = tmp_path / "out"
    made = _fake_runs(monkeypatch, ran={stem: ran})
    argv = ["--config", "serial", "--config", "n2-load", "--repeats", "1", "--out", str(out)]
    with pytest.raises(SystemExit) as exited:
        parallel_bench.main(argv)
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert message in err and err.rstrip().endswith(f"{stem} not recorded")
    assert made[-1] == stem
    assert not (out / f"{stem}.json").exists()
    assert not (out / "results.json").exists()


@pytest.mark.parametrize("damage", ["report", "record"])
def test_a_kept_run_whose_report_or_record_no_longer_reads_runs_again(
    tmp_path, monkeypatch, damage
):
    out = tmp_path / "out"
    argv = ["--config", "serial", "--config", "n2-load", "--repeats", "1", "--out", str(out)]
    _fake_runs(monkeypatch)
    assert parallel_bench.main(argv) == 0
    if damage == "report":
        (out / "n2-load-0.xml").unlink()
    else:
        (out / "n2-load-0.json").write_text('{"config": "n2-load", "rep')
    made = _fake_runs(monkeypatch)
    assert parallel_bench.main(argv) == 0
    assert made == ["n2-load-0"]
    assert parallel_bench.kept_run(out / "n2-load-0.json") is not None


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


@pytest.mark.parametrize(
    ("key", "first", "then"),
    [
        ("cpu_affinity", [0, 1], [0, 6]),  # one size, other cores
        ("tmpdir", "/tmp", "/var/tmp/bench-tmp"),
    ],
)
def test_an_out_directory_of_another_cpu_set_or_tmpdir_is_refused(
    tmp_path, monkeypatch, capsys, key, first, then
):
    out = tmp_path / "out"
    argv = ["--config", "serial", "--repeats", "1", "--out", str(out)]
    _fake_runs(monkeypatch, cpus_usable=2, **{key: first})
    assert parallel_bench.main(argv) == 0
    (out / "serial-0.json").unlink()
    made = _fake_runs(monkeypatch, cpus_usable=2, **{key: then})
    with pytest.raises(SystemExit) as exited:
        parallel_bench.main(argv)
    assert exited.value.code == 2
    assert f"holds runs measured with another {key}" in capsys.readouterr().err
    assert made == []


def test_a_worktree_with_changes_is_refused_before_anything_runs(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    made = _fake_runs(monkeypatch, changes=[" M src/autoforge/engine.py", "?? tests/test_new.py"])
    with pytest.raises(SystemExit) as exited:
        parallel_bench.main(["--config", "serial", "--out", str(out)])
    assert exited.value.code == 2
    assert "uncommitted changes (src/autoforge/engine.py, tests/test_new.py)" in (
        capsys.readouterr().err
    )
    assert made == []
    assert not out.exists()


def test_a_resume_refuses_other_uncommitted_changes_on_the_same_commit(
    tmp_path, monkeypatch, capsys
):
    out = tmp_path / "out"
    argv = ["--config", "serial", "--config", "n2-load", "--repeats", "1", "--out", str(out)]
    _fake_runs(monkeypatch)
    assert parallel_bench.main(argv) == 0
    (out / "n2-load-0.json").unlink()
    made = _fake_runs(monkeypatch, changes=[" M tests/conftest.py"])
    with pytest.raises(SystemExit) as exited:
        parallel_bench.main(argv)
    assert exited.value.code == 2
    assert "uncommitted changes (tests/conftest.py)" in capsys.readouterr().err
    assert made == []


def test_a_change_during_a_run_stops_the_benchmark_before_recording_it(
    tmp_path, monkeypatch, capsys
):
    out = tmp_path / "out"
    changes: list[str] = []
    made = _fake_runs(monkeypatch, changes=changes)
    fake = parallel_bench.run_pytest

    def run_while_editing(*args):
        changes.append(" M src/autoforge/engine.py")
        return fake(*args)

    monkeypatch.setattr(parallel_bench, "run_pytest", run_while_editing)
    argv = ["--config", "serial", "--config", "n2-load", "--repeats", "1", "--out", str(out)]
    with pytest.raises(SystemExit) as exited:
        parallel_bench.main(argv)
    assert exited.value.code == 2
    assert "serial-0 not recorded" in capsys.readouterr().err
    assert made == ["serial-0"]
    assert not (out / "serial-0.json").exists()


def test_the_worktree_is_read_from_git_and_a_status_git_cannot_read_is_no_clean_one(
    tmp_path, monkeypatch
):
    repo = git_repo(tmp_path / "repo")
    monkeypatch.setattr(parallel_bench, "REPO_ROOT", repo)
    head = parallel_bench.source()[0]
    assert len(head) == 40
    assert parallel_bench.source_problem(head) == ""
    (repo / "new.py").write_text("")
    assert "uncommitted changes (new.py)" in parallel_bench.source_problem(head)
    (repo / "new.py").unlink()

    # A corrupt index: HEAD still reads, git status fails and prints nothing.
    (repo / ".git" / "index").write_bytes(b"not an index")
    assert parallel_bench._git("rev-parse", "HEAD").strip() == head
    problem = parallel_bench.source_problem(head)
    assert problem.startswith(
        "the worktree could not be read (`git --no-optional-locks status --porcelain` exited 128"
    )


def test_a_worktree_git_cannot_read_is_refused_before_and_after_a_run(
    tmp_path, monkeypatch, capsys
):
    out = tmp_path / "out"
    argv = ["--config", "serial", "--repeats", "1", "--out", str(out)]
    made = _fake_runs(monkeypatch)
    failure = parallel_bench.UnreadableWorktree("`git status` exited 128: fatal: bad index")

    def unreadable():
        raise failure

    monkeypatch.setattr(parallel_bench, "source", unreadable)
    with pytest.raises(SystemExit) as exited:
        parallel_bench.main(argv)
    assert exited.value.code == 2
    assert "the worktree could not be read (`git status` exited 128" in capsys.readouterr().err
    assert made == [] and not out.exists()

    readable = [True]
    monkeypatch.setattr(
        parallel_bench, "source", lambda: ("c0", []) if readable[0] else unreadable()
    )
    fake = parallel_bench.run_pytest

    def run_then_break(*args):
        readable[0] = False
        return fake(*args)

    monkeypatch.setattr(parallel_bench, "run_pytest", run_then_break)
    with pytest.raises(SystemExit) as exited:
        parallel_bench.main(argv)
    assert exited.value.code == 2
    assert "could not be read" in capsys.readouterr().err
    assert made == ["serial-0"]
    assert not (out / "serial-0.json").exists()


def test_an_out_directory_inside_the_worktree_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(parallel_bench, "REPO_ROOT", tmp_path.resolve())
    made = _fake_runs(monkeypatch)
    with pytest.raises(SystemExit) as exited:
        parallel_bench.main(["--config", "serial", "--out", str(tmp_path / "bench")])
    assert exited.value.code == 2
    assert "--out must lie outside" in capsys.readouterr().err
    assert made == []
    assert not (tmp_path / "bench").exists()
