"""Crash recovery for LOCAL mode: the boundaries around the run itself.

The matrix over each phase's crash points went with the LOCAL state it
loaded from disk, which the loader refuses since LOCAL was removed (ADR 0009
D4, #387); `tests/test_runlog.py` and the REMOTE engine tests hold its
journal cases. What is left drives LOCAL code in memory, and goes with it
(#388).
"""

from __future__ import annotations

import pytest

from autoforge.errors import StateError
from autoforge.transitions import Phase

from .conftest import make_local_engine
from .local_helpers import (
    impl_result,
    local_repo,
    review_result,
    touch_impl,
)

FEATURE = "features/add-filter.md"


class Crash(BaseException):
    """A power cut, not an exception the controller could ever handle.

    Deriving from ``BaseException`` is deliberate: an ``except Exception``
    anywhere in the step must not be able to turn this into an orderly
    shutdown, because a real crash does not give the controller that chance.
    """


# A LOCAL step's first persist: the binding (step count, the fingerprint the
# agent is judged against), before any agent is launched.
SAVE_BINDING = 1


def crash_on_save(eng, nth: int):
    """Make the ``nth`` remaining state persist die instead of landing."""
    original = eng._save
    left = {"n": nth}

    def dying_save(*a, **k):
        left["n"] -= 1
        if left["n"] == 0:
            raise Crash("power cut before the state reached the disk")
        return original(*a, **k)

    eng._save = dying_save  # type: ignore[method-assign]


# -- the boundaries around the run itself --------------------------------------
def test_a_crash_before_the_run_was_ever_persisted_leaves_no_run(tmp_path):
    """`new_local_run` writes nothing; a crash before the first save is a no-op.

    `resume` must then say there is no run rather than invent one, because a
    silently created run would start implementing against a specification no
    operator confirmed.
    """
    root = local_repo(tmp_path)
    eng = make_local_engine(root, FEATURE)
    crash_on_save(eng, SAVE_BINDING)  # the run's first save
    with pytest.raises(Crash):
        eng.step()

    eng2 = make_local_engine(root, FEATURE, start=False)
    with pytest.raises(StateError, match="no state file"):
        eng2.load()


def test_nothing_outside_the_checkout_is_touched_by_any_of_this(tmp_path):
    """The sentinel the whole LOCAL promise is about, asserted end to end."""
    outside = tmp_path / "sentinel.txt"
    outside.write_text("mine\n", encoding="utf-8")
    before = outside.stat()
    root = local_repo(tmp_path / "checkout")
    eng = make_local_engine(root, FEATURE)
    eng.step()
    eng.provider._handler = lambda req: (touch_impl(root, "v1\n"), impl_result())[1]
    eng.step()
    eng.provider._handler = lambda req: review_result(eng.state.workspace_fingerprint)
    eng.step()
    assert eng.state.phase == Phase.DONE
    assert outside.read_text(encoding="utf-8") == "mine\n"
    assert outside.stat().st_ino == before.st_ino
    assert outside.stat().st_mtime_ns == before.st_mtime_ns
    assert sorted(p.name for p in tmp_path.iterdir()) == ["checkout", "sentinel.txt"]
