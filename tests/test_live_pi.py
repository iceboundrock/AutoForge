"""The live Pi smoke test (#135): a real ``pi``, a real credential, one model call.

Opt-in twice over: the ``live`` marker deselects it unless the command line
selects it (``-m live``), and it then skips unless ``AUTOFORGE_LIVE_PI=1``.
``make check`` and CI therefore never run it. The auth route is named
explicitly, never defaulted:

- ``AUTOFORGE_LIVE_PI_AUTH=api_key``: a provider API key with
  ``require_oauth: false``, ``deepseek/deepseek-flash`` by default; the key
  in Pi's own credential store, or forwarded on purpose with
  ``AUTOFORGE_LIVE_PI_FORWARD_KEY=DEEPSEEK_API_KEY``. This is the
  acceptance run of #135's amendment of 2026-10-20.
- ``AUTOFORGE_LIVE_PI_AUTH=oauth``: the ChatGPT sign-in with
  ``require_oauth: true``, ``openai/gpt-5.6-terra`` by default. An optional
  regression run.

One test, one model call: a REMOTE REVIEW against a GitHub stand-in that
never reaches GitHub (ADR 0009 Appendix A.2.3 G). Give Pi its credential
yourself first, leave ``OPENAI_API_KEY`` and ``DEEPSEEK_API_KEY`` unset
(unless forwarding one), and from the repository root::

    AUTOFORGE_LIVE_PI=1 AUTOFORGE_LIVE_PI_AUTH=api_key uv run pytest -m live \\
        tests/test_live_pi.py::test_one_remote_review_runs_on_the_operators_real_pi

It spends one small REVIEW of the account's quota.
``AUTOFORGE_LIVE_PI_MODEL`` and ``AUTOFORGE_LIVE_PI_EFFORT`` (default
``low``) choose the model. The report it prints at the end carries no key
or account detail and is the evidence to post on #135. The steps are in
``tests/live_pi.py``; ``tests/test_live_pi_support.py`` rehearses them
offline against the fake ``pi``.
"""

from __future__ import annotations

import os

import pytest

import autoforge.child_env as child_env_module
import autoforge.engine as engine_module
from tests.live_pi import (
    LIVE_VARIABLE,
    SmokeReport,
    run_remote_smoke,
    settings_from_environment,
)

pytestmark = pytest.mark.live

# The real launch preflight and sandbox probe, taken before the suite's
# autouse fixtures stand in for them: the smoke run is the operator's own.
_OPERATOR_HOME = child_env_module.operator_home
_PROBE_SANDBOX = engine_module.probe_sandbox


def _run_live(flow, tmp_path, monkeypatch, capsys) -> None:
    """Skip without ``AUTOFORGE_LIVE_PI=1``; otherwise run ``flow`` on the
    operator's settings, print its report and require every step to pass."""
    selected = os.environ.get(LIVE_VARIABLE, "")
    if not selected:
        pytest.skip(f"set {LIVE_VARIABLE}=1 to run the live Pi smoke test (one model call)")
    if selected != "1":
        pytest.fail(f"{LIVE_VARIABLE} must be 1 or unset, got {selected!r}")
    try:
        settings = settings_from_environment()
    except ValueError as exc:
        pytest.fail(str(exc))
    monkeypatch.setattr(child_env_module, "operator_home", _OPERATOR_HOME)
    monkeypatch.setattr(engine_module, "probe_sandbox", _PROBE_SANDBOX)

    report = SmokeReport()
    try:
        flow(tmp_path, settings, report)
    finally:
        with capsys.disabled():
            print("\n" + report.render())
    assert report.ok, report.render()


def test_one_remote_review_runs_on_the_operators_real_pi(tmp_path, monkeypatch, capsys):
    """Steps 1 to 7 of #135 against the operator's own Pi and credential, on
    a REMOTE run (ADR 0009 Appendix A.2.3 G): the real ``autoforge doctor``'s
    Pi rows, then one REMOTE REVIEW on Pi of a PR the controller opened on
    the GitHub stand-in."""
    _run_live(run_remote_smoke, tmp_path, monkeypatch, capsys)
