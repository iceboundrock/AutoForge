"""tmp_path retention (#396, ``tests/tmp_path_retention.py``): a test's
``tmp_path`` is removed once the test has passed and kept when its setup, its
call or its teardown failed.

Each case runs a synthetic suite in a pytest process of its own, wired as
``tests/conftest.py`` wires this suite, and lists the directories its
basetemp holds afterwards with the evidence each test wrote there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CONFTEST = """\
from tests import tmp_path_retention

tmp_path = tmp_path_retention.tmp_path
pytest_runtest_makereport = tmp_path_retention.pytest_runtest_makereport
"""

SUITE = """\
import pytest


@pytest.fixture
def breaks_in_setup(tmp_path):
    (tmp_path / "evidence").write_text("setup")
    raise RuntimeError("setup")


@pytest.fixture
def breaks_in_teardown(tmp_path):
    (tmp_path / "evidence").write_text("teardown")
    yield
    raise RuntimeError("teardown")


@pytest.fixture
def breaks_after_tmp_path():
    # Set up before tmp_path, so torn down after it: pytest's own finalizer has
    # already decided by the time this fails.
    yield
    raise RuntimeError("late teardown")


def test_passes(tmp_path):
    (tmp_path / "evidence").write_text("passed")


def test_skips(tmp_path):
    (tmp_path / "evidence").write_text("skipped")
    pytest.skip("skipped")


def test_fails(tmp_path):
    (tmp_path / "evidence").write_text("call")
    assert False


@pytest.mark.xfail(strict=True)
def test_xpasses_strict(tmp_path):
    (tmp_path / "evidence").write_text("xpassed")


def test_setup_errors(breaks_in_setup):
    pass


def test_teardown_errors(breaks_in_teardown):
    pass


def test_late_teardown_errors(breaks_after_tmp_path, tmp_path):
    (tmp_path / "evidence").write_text("late teardown")
"""

FAILED = {
    "test_fails0": "call",
    "test_xpasses_strict0": "xpassed",
    "test_setup_errors0": "setup",
    "test_teardown_errors0": "teardown",
    "test_late_teardown_errors0": "late teardown",
}


def _left_behind(tmp_path: Path, *options: str) -> dict[str, str]:
    (tmp_path / "pytest.ini").write_text("[pytest]\ntmp_path_retention_policy = failed\n")
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "test_suite.py").write_text(SUITE)
    basetemp = tmp_path / "basetemp"
    # None of this session's pytest settings reach the inner one: a --basetemp
    # in PYTEST_ADDOPTS would have it empty this session's basetemp.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = str(REPO)
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            f"--basetemp={basetemp}",
            *options,
            "test_suite.py",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    assert "2 failed, 3 passed, 1 skipped, 3 errors" in run.stdout, run.stdout
    return {
        entry.name: (entry / "evidence").read_text()
        for entry in basetemp.iterdir()
        if entry.is_dir() and not entry.is_symlink()
    }


def test_a_failure_in_any_phase_keeps_the_tmp_path_and_nothing_else_does(tmp_path):
    assert _left_behind(tmp_path) == FAILED


def test_the_all_policy_keeps_every_tmp_path(tmp_path):
    assert _left_behind(tmp_path, "-o", "tmp_path_retention_policy=all") == {
        **FAILED,
        "test_passes0": "passed",
        "test_skips0": "skipped",
    }
