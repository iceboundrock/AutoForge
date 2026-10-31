"""A test's ``tmp_path`` is removed when it passes and kept when any phase failed (#396).

``tmp_path_retention_policy = "failed"`` (``pyproject.toml``) is documented
to keep the directories of tests that fail or error, but pytest (9.1.1)
decides in its ``tmp_path`` finalizer, from the call phase alone. A fixture
that fails in setup, or in a teardown that runs before or after that
finalizer, errors the test, and its directory is still removed (PR #397
review). ``tmp_path`` here makes the directory pytest's does and removes
nothing itself; the hook below removes it once the teardown report is made,
when no phase of the test failed. Under any other policy it removes nothing,
so ``-o tmp_path_retention_policy=all`` keeps every directory. pytest still
removes the whole basetemp after a green session, as ``failed`` has it.
``tests/conftest.py`` imports both, so they replace pytest's own in every test.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Generator
from pathlib import Path

import pytest

_PATH = pytest.StashKey[Path]()
_FAILED = pytest.StashKey[bool]()


# The next number to try for each name, by basetemp, and every directory
# made here, removed or not.
_NEXT: dict[tuple[Path, str], int] = {}
_MADE: set[Path] = set()


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """pytest's ``tmp_path``, named as pytest names it, removed by the hook below.

    pytest's ``mktemp`` lists the whole basetemp to number a directory, and
    leaves a ``<name>current`` link there for every name, so making one cost
    more the more tests had run, removed or not (#341). This counts per name
    instead and lists nothing: the first number not yet tried for the name
    whose directory no test of this session has had and can be made, so a
    directory is never reused in a session. The count alone would not do:
    ``test_a1`` numbered 0 and ``test_a`` numbered 10 are both ``test_a10``,
    free again once the first test passed (PR #411 review).
    """
    name = re.sub(r"[\W]", "_", request.node.name)[:30]
    root = tmp_path_factory.getbasetemp()
    number = _NEXT.get((root, name), 0)
    while True:
        path = root / f"{name}{number}"
        number += 1
        if path in _MADE:
            continue
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        break
    _NEXT[(root, name)] = number
    _MADE.add(path)
    request.node.stash[_PATH] = path
    return path


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    # Outermost, as pytest's own retention hook is, so the report is final
    # (a strict xfail that passed has already been made a failure).
    report = yield
    if report.failed:
        item.stash[_FAILED] = True
    path = item.stash.get(_PATH, None)
    if (
        report.when == "teardown"
        and path is not None
        and not item.stash.get(_FAILED, False)
        and item.config.getini("tmp_path_retention_policy") == "failed"
    ):
        # Best effort, as pytest's: a directory a test left unremovable stays.
        shutil.rmtree(path, ignore_errors=True)
    return report
