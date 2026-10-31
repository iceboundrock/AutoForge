"""The fast feedback targets, and what keeps them from standing for the full suite (ADR 0010, #342).

``tests/fast_targets.py`` maps a change's paths to test modules for
``make test-fast``. These tests pin that its mapping is explicit and
complete, that its selection never narrows what it does not know, that it
reads the change from git as documented and hands pytest exactly what it
selected, and that nothing a FAST run produces can pass for the full
suite: ``make check`` and CI never run it, and the command they do run
collects every test but the opt-in live ones.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests import fast_targets as ft

from .conftest import GIT_IDENT, NO_AUTO_MAINTENANCE, git_out
from .test_ci_workflow import _code, _job, _make_dry_run, _make_rules

ROOT = ft.REPO_ROOT


def _selected(*paths: str) -> ft.Selection:
    return ft.select(paths)


# -- the mapping is explicit and complete (D9) -----------------------------------------


def _package_files() -> list[str]:
    return sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "src" / "autoforge").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )


def _test_helpers() -> list[str]:
    return sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "tests").glob("*.py")
        if not p.name.startswith("test_")
    )


def test_every_package_file_and_test_helper_has_an_entry():
    """A new module, template or helper fails here until its entry says what checks it."""
    missing = [p for p in (*_package_files(), *_test_helpers()) if ft.entry_key(p) is None]
    assert missing == [], f"add an entry to tests/fast_targets.py COVERAGE for: {missing}"


def test_every_entry_names_an_existing_path_and_existing_test_modules():
    suite = set(ft.suite_modules())
    for key, entry in ft.COVERAGE.items():
        assert (ROOT / key).is_dir() if key.endswith("/") else (ROOT / key).is_file(), (
            f"COVERAGE names {key!r}, which does not exist"
        )
        if entry is ft.FULL:
            continue
        for module in entry:
            assert module in suite, f"{key}: {module!r} is no test module"
            assert module not in ft.LIVE_ONLY, f"{key}: {module!r} runs no test by default"


def test_every_entry_that_does_not_select_the_full_suite_selects_something():
    files = [k for k, v in ft.COVERAGE.items() if v is not ft.FULL and not k.endswith("/")]
    selection = ft.select(files)
    assert not selection.full
    empty = [c.path for c in selection.choices if not c.modules]
    assert empty == [], f"these entries select no test module: {empty}"


def test_the_suite_is_every_test_module_under_the_testpath():
    suite = ft.suite_modules()
    assert "test_engine" in suite and "test_fast_targets" in suite
    assert len(suite) == len(list((ROOT / "tests").glob("test_*.py")))
    assert not list((ROOT / "tests").glob("*/test_*.py")), (
        "a test module below tests/ is outside fast_targets.suite_modules()"
    )


# -- the selection (D9) ----------------------------------------------------------------


def test_a_module_selects_its_listed_tests_and_every_test_module_that_imports_it():
    (choice,) = _selected("src/autoforge/evidence.py").choices
    assert not choice.full
    # Listed: the loop's own tests and the unblock tests.
    assert {"test_engine", "test_integration", "test_unblock"} <= choice.modules
    # Importers: found, not listed.
    assert {"test_evidence", "test_certificate", "test_validation_gates"} <= choice.modules
    assert "more importing autoforge.evidence" in choice.reason


def test_the_import_scan_reads_every_spelling_of_an_import(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text(
        "from autoforge import evidence as ev\n"
        "import autoforge.state\n"
        "from .local_helpers import thing\n"
        "from tests.pi_fake import PiFake\n"
        "import os\n"
        "def test_x():\n"
        "    from autoforge.config import default_config\n",
        encoding="utf-8",
    )
    (tests / "test_b.py").write_text("from autoforge.evidence import Definition\n", "utf-8")
    found = ft.importers(tmp_path)
    assert found["autoforge.evidence"] == {"test_a", "test_b"}
    assert found["autoforge.state"] == found["autoforge.config"] == {"test_a"}
    assert found["tests.local_helpers"] == found["tests.pi_fake"] == {"test_a"}
    assert "os" not in found


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        "docs/development.md",
        ".github/workflows/ci.yml",
        "src/autoforge/brand_new_module.py",
        "tests/brand_new_helper.py",
    ],
)
def test_a_path_without_an_entry_selects_the_full_suite(path):
    """The mapping never narrows what it does not know (D9)."""
    selection = _selected(path, "src/autoforge/scheduler.py")
    assert selection.full
    assert selection.modules == ft.suite_modules()
    assert selection.targets() == ["tests"]
    reasons = {c.path: c.reason for c in selection.choices}
    assert reasons[path] == "no entry: the full suite"


@pytest.mark.parametrize(
    "path",
    [
        "src/autoforge/engine.py",
        "src/autoforge/state.py",
        "src/autoforge/prompts/review.md",
        "src/autoforge/prompts/__init__.py",
        "tests/conftest.py",
    ],
)
def test_a_full_entry_selects_the_full_suite(path):
    selection = _selected(path)
    assert selection.full and selection.targets() == ["tests"]
    assert "is FULL" in selection.choices[0].reason


def test_a_test_module_selects_itself_and_a_removed_one_nothing():
    selection = _selected("tests/test_evidence.py", "tests/test_removed_long_ago.py")
    assert not selection.full
    assert selection.modules == ("test_evidence",)
    assert selection.targets() == ["tests/test_evidence.py"]
    assert _selected("tests/test_removed_long_ago.py").targets() == []


def test_the_selection_is_the_union_of_its_paths():
    one = set(_selected("src/autoforge/scheduler.py").modules)
    two = set(_selected("src/autoforge/pi_rpc.py").modules)
    both = _selected("src/autoforge/scheduler.py", "src/autoforge/pi_rpc.py")
    assert set(both.modules) == one | two
    assert both.targets() == [f"tests/{m}.py" for m in sorted(one | two)]


def test_an_exact_entry_wins_over_a_prefix_and_the_longest_prefix_over_a_shorter():
    coverage = {"a/": ("test_x",), "a/b/": ("test_y",), "a/b/c.py": ft.FULL}
    assert ft.entry_key("a/b/c.py", coverage) == "a/b/c.py"
    assert ft.entry_key("a/b/d.py", coverage) == "a/b/"
    assert ft.entry_key("a/e.py", coverage) == "a/"
    assert ft.entry_key("ab/e.py", coverage) is None


@pytest.mark.parametrize(
    ("path", "module"),
    [
        ("src/autoforge/evidence.py", "autoforge.evidence"),
        ("src/autoforge/__init__.py", "autoforge"),
        ("src/autoforge/prompts/__init__.py", "autoforge.prompts"),
        ("tests/pi_fake.py", "tests.pi_fake"),
        ("src/autoforge/prompts/review.md", None),
        ("scripts/x.py", None),
    ],
)
def test_a_path_imports_as_its_module(path, module):
    assert ft.module_name(path) == module


# -- the change, read from git (D9) ----------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return git_out("-C", str(repo), *NO_AUTO_MAINTENANCE, *GIT_IDENT, *args)


def test_the_change_is_read_from_the_merge_base_with_the_base(tmp_path):
    """Committed, staged, unstaged and untracked; both sides of a rename; not
    what the base did after the merge base, and not what git ignores."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for name in ("a.py", "b.py", "c.py"):
        (repo / name).write_text(f"{name}\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "work")
    (repo / "a.py").write_text("changed\n", encoding="utf-8")
    _git(repo, "mv", "b.py", "d.py")
    _git(repo, "commit", "-q", "-am", "work")
    _git(repo, "checkout", "-q", "main")
    (repo / "e.py").write_text("the base moved on\n", encoding="utf-8")
    _git(repo, "add", "e.py")
    _git(repo, "commit", "-q", "-m", "base moves")
    _git(repo, "checkout", "-q", "work")
    (repo / "c.py").write_text("unstaged\n", encoding="utf-8")
    (repo / "f.py").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "f.py")
    (repo / "g.py").write_text("untracked\n", encoding="utf-8")
    (repo / "h.log").write_text("ignored\n", encoding="utf-8")

    merge_base, paths = ft.changed_paths("main", repo)
    assert merge_base == _git(repo, "merge-base", "main", "HEAD")
    assert paths == ["a.py", "b.py", "c.py", "d.py", "f.py", "g.py"]


def test_an_unreadable_base_stops_the_tool(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    with pytest.raises(SystemExit, match="git merge-base no-such-revision HEAD failed"):
        ft.changed_paths("no-such-revision", repo)


# -- the run (D5, D9) ------------------------------------------------------------------


class _Runs:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.code = code

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, self.code)


def test_run_hands_pytest_exactly_the_selection_and_returns_its_status(capsys):
    runs = _Runs(code=1)
    code = ft.main(["--run", "src/autoforge/scheduler.py", "--", "-x", "-n", "4"], run=runs)
    assert code == 1
    ((argv, kwargs),) = runs.calls
    modules = _selected("src/autoforge/scheduler.py").modules
    assert argv == [
        sys.executable,
        "-m",
        "pytest",
        *(f"tests/{m}.py" for m in modules),
        "-x",
        "-n",
        "4",
    ]
    assert kwargs == {"cwd": ft.REPO_ROOT}
    err = capsys.readouterr().err
    assert "1 path(s) given" in err
    assert f"selected {len(modules)} of {len(ft.suite_modules())} test modules" in err
    assert "pytest exited 1; a FAST run is feedback, not evidence" in err


def test_run_of_the_full_suite_hands_pytest_its_testpath(capsys):
    runs = _Runs()
    assert ft.main(["--run", "src/autoforge/engine.py"], run=runs) == 0
    ((argv, _),) = runs.calls
    assert argv == [sys.executable, "-m", "pytest", "tests"]
    assert "selected the full suite" in capsys.readouterr().err


def test_nothing_selected_runs_nothing(capsys):
    runs = _Runs()
    assert ft.main(["--run", "tests/test_removed_long_ago.py"], run=runs) == 0
    assert runs.calls == []
    assert "nothing to run" in capsys.readouterr().err


def test_without_run_it_prints_the_targets_and_runs_nothing(capsys):
    runs = _Runs()
    assert ft.main(["./tests/test_evidence.py", "src/autoforge/engine.py"], run=runs) == 0
    assert runs.calls == []
    out, err = capsys.readouterr()
    assert out.split() == ["tests"]
    assert "a FAST run is feedback, not evidence" in err


def test_a_given_path_is_read_relative_to_the_repository(capsys):
    absolute = str(ft.REPO_ROOT / "tests" / "test_evidence.py")
    assert ft.main([absolute, "./tests/test_certificate.py"], run=_Runs()) == 0
    assert capsys.readouterr().out.split() == [
        "tests/test_certificate.py",
        "tests/test_evidence.py",
    ]


def test_pytest_arguments_need_run():
    with pytest.raises(SystemExit) as raised:
        ft.main(["src/autoforge/engine.py", "--", "-x"], run=_Runs())
    assert raised.value.code == 2


# -- never a gate, and the full suite stays full (D6, D7, D8) --------------------------


def test_the_fast_target_runs_the_tool_and_is_never_a_gate():
    """``make test-fast`` is the tool, against ``main`` by default; no target of
    ``make check`` or ``check-matrix`` depends on it and no CI job runs it, so a
    FAST pass can never be what turns a check green (ADR 0010 D6, D7)."""
    assert _make_dry_run("test-fast") == [
        "uv run python -m tests.fast_targets --base main --run --"
    ]
    assert _make_dry_run("test-fast", assignments=("FAST_BASE=v1", "FAST_PYTEST_ARGS=-x")) == [
        "uv run python -m tests.fast_targets --base v1 --run -- -x"
    ]
    rules = _make_rules()
    for gate in ("check", "check-matrix", "test"):
        assert "test-fast" not in rules[gate].prerequisites
    assert all("fast_targets" not in line for line in rules["test"].recipe)
    code = _code()
    assert "test-fast" not in code and "fast_targets" not in code
    # The suite CI runs on each interpreter is the whole default suite.
    assert "      - run: uv run pytest\n" in _job(code, "test") + "\n"


def test_the_default_suite_deselects_nothing_but_the_live_tests():
    """What ``uv run pytest`` collects in CI and ``make check`` is the whole of
    ``tests/`` but the opt-in live tests (ADR 0010 D8).

    The suite's own options are candidate code: a PR can add a ``-m``, a
    ``-k``, an ``--ignore`` or a collection hook and narrow what the full
    suite runs without touching a protected path. This test is a tripwire
    for that, not a control: the same PR can edit it, and review is what
    catches a PR that does both. A new pytest option fails here until this
    test says it narrows nothing.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = data["tool"]["pytest"]["ini_options"]
    assert set(options) == {"testpaths", "addopts", "markers", "tmp_path_retention_policy"}
    assert options["testpaths"] == ["tests"]
    assert shlex.split(options["addopts"]) == ["-q", "-rs", "-m", "not live"]
    hooks = (
        "pytest_ignore_collect",
        "pytest_collection_modifyitems",
        "pytest_collect_file",
        "pytest_pycollect_makemodule",
        "pytest_pycollect_makeitem",
    )
    for plugin in ("conftest.py", "tmp_path_retention.py"):
        text = (ROOT / "tests" / plugin).read_text(encoding="utf-8")
        assert not [h for h in hooks if f"def {h}(" in text], f"tests/{plugin} drops tests"
