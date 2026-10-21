"""The live Pi smoke test (#135): a real ``pi``, a real ChatGPT sign-in, one model call.

Opt-in twice over: the ``live`` marker deselects it unless the command line
selects it (``-m live``), and it then skips unless ``AUTOFORGE_LIVE_PI=1``.
``make check`` and CI therefore never run it. To run it, sign in to Pi
yourself first (``pi``, then ``/login openai``), unset ``OPENAI_API_KEY``,
and from the repository root::

    AUTOFORGE_LIVE_PI=1 uv run pytest -m live tests/test_live_pi.py

It spends one small REVIEW of subscription quota. ``AUTOFORGE_LIVE_PI_MODEL``
(default ``openai/gpt-5.6-terra``) and ``AUTOFORGE_LIVE_PI_EFFORT`` (default
``low``) choose the model. The report it prints at the end carries no
account detail and is the evidence to post on #135. The steps are in
``tests/live_pi.py``; ``tests/test_live_pi_support.py`` rehearses them
offline against the fake ``pi``.
"""

from __future__ import annotations

import os

import pytest

import autoforge.child_env as child_env_module
import autoforge.engine as engine_module
from tests.live_pi import LIVE_VARIABLE, SmokeReport, run_smoke, settings_from_environment

pytestmark = pytest.mark.live

# The real launch preflight and sandbox probe, taken before the suite's
# autouse fixtures stand in for them: the smoke run is the operator's own.
_OPERATOR_HOME = child_env_module.operator_home
_PROBE_SANDBOX = engine_module.probe_sandbox


def test_one_local_review_runs_on_a_real_pi_subscription(tmp_path, monkeypatch, capsys):
    """Steps 1 to 7 of #135 against the operator's own Pi and sign-in."""
    selected = os.environ.get(LIVE_VARIABLE, "")
    if not selected:
        pytest.skip(f"set {LIVE_VARIABLE}=1 to run the live Pi smoke test (one model call)")
    if selected != "1":
        pytest.fail(f"{LIVE_VARIABLE} must be 1 or unset, got {selected!r}")
    monkeypatch.setattr(child_env_module, "operator_home", _OPERATOR_HOME)
    monkeypatch.setattr(engine_module, "probe_sandbox", _PROBE_SANDBOX)

    report = SmokeReport()
    try:
        run_smoke(tmp_path, settings_from_environment(), report)
    finally:
        with capsys.disabled():
            print("\n" + report.render())
    assert report.ok, report.render()
