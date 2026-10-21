"""The live Pi smoke test's machinery, offline (#135).

The live test itself (``tests/test_live_pi.py``) needs a real ``pi`` and a
ChatGPT sign-in, so it never runs here. What is tested instead: that the
default options deselect it, that its credential scan finds each pattern
and names it without the match, its other checks and failure reasons, and
its whole flow rehearsed against the fake ``pi`` of ``tests/pi_fake.py``
(the real ``autoforge local doctor`` in a process of its own, the real
engine and ``PiProvider``), passing and failing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import block
from tests.live_pi import (
    CREDENTIAL_PATTERNS,
    LIVE_VARIABLE,
    STEPS,
    SmokeReport,
    SmokeSettings,
    StepFailed,
    classify_failure,
    credential_hits,
    entry_names,
    processes_under,
    run_smoke,
    scan_for_credentials,
    tree_snapshot,
)
from tests.pi_fake import PiFake, PiTurn

ROOT = Path(__file__).resolve().parents[1]


# -- the marker -------------------------------------------------------------------
def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    """``pytest`` on the live test file under the repository's own options."""
    env = {k: v for k, v in os.environ.items() if k not in (LIVE_VARIABLE, "PYTEST_ADDOPTS")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args, "tests/test_live_pi.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_live_test_is_deselected_by_default_and_skips_without_its_variable():
    """The default options leave the live test out of collection (so `make
    check` and CI never run it); `-m live` selects it, and without
    AUTOFORGE_LIVE_PI=1 it skips with its reason and launches nothing."""
    default = _pytest("--collect-only")
    assert default.returncode == 5, default.stdout  # nothing collected
    assert "1 deselected" in default.stdout
    assert "test_one_local_review_runs_on_a_real_pi_subscription" not in default.stdout

    selected = _pytest("-m", "live")
    assert selected.returncode == 0, selected.stdout
    assert "1 skipped" in selected.stdout
    assert f"set {LIVE_VARIABLE}=1 to run the live Pi smoke test" in selected.stdout


# -- credential scan --------------------------------------------------------------
# Synthetic credential shapes, assembled here so that no source file holds one.
_JWT = ".".join(["eyJ" + "hbGciOiJub25lIn0", "eyJ" + "zdWIiOiJ4In0", "c2lnbmF0dXJl"])
_SECRET = "Q" * 24
SAMPLES = {
    "jwt": f"token={_JWT}",
    "bearer": "Authorization: " + "Bearer " + _SECRET,
    "refresh_token": "refresh" + "_token" + f"={_SECRET}",
    '"refresh"': '{"' + "refresh" + f'": "{_SECRET}"' + "}",
    '"access"': '{"' + "access" + f'": "{_SECRET}"' + "}",
    "chatgpt-account-id": "ChatGPT-Account-Id" + f": {_SECRET}",
    "chatgpt_account_id": '{"' + "chatgpt_account_id" + f'": "{_SECRET}"' + "}",
    "sk- key": "key " + "sk-" + "proj-" + _SECRET,
}


def test_every_pattern_has_a_sample():
    assert sorted(SAMPLES) == sorted(name for name, _ in CREDENTIAL_PATTERNS)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_the_scan_finds_each_pattern_and_names_it_without_the_match(tmp_path, name):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "stdout.log").write_text(f"before\n{SAMPLES[name]}\nafter\n")
    (tmp_path / "clean.json").write_text('{"phase": "REVIEW"}\n')
    hits = scan_for_credentials(tmp_path)
    assert (str(Path("logs") / "stdout.log"), name) in hits
    assert {path for path, _ in hits} == {str(Path("logs") / "stdout.log")}
    shown = repr(hits)
    assert _SECRET not in shown and _JWT not in shown

    report = SmokeReport()
    with report.step(STEPS[4]):
        raise StepFailed("; ".join(f"{path}: {hit}" for path, hit in hits))
    assert f"stdout.log: {name}" in report.render() and _SECRET not in report.render()


def test_the_field_names_match_when_escaped_inside_a_json_string(tmp_path):
    escaped = json.dumps(json.dumps({"refresh": _SECRET, "access": _SECRET}))
    assert {'"refresh"', '"access"'} <= set(credential_hits(escaped))


@pytest.mark.parametrize(
    "prose",
    [
        "refresh the page, then access: denied",
        "the bearer of bad news",
        "a task-list-with-many-characters and a risk-assessment-procedure",
        "eyJ alone, or eyJabc without dots",
        '{"refresher": "x", "accessible": true}',
    ],
)
def test_prose_is_not_a_credential(prose):
    assert credential_hits(prose) == []


def test_symbolic_links_are_not_followed(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(SAMPLES["jwt"])
    (tmp_path / "scanned").mkdir()
    (tmp_path / "scanned" / "link").symlink_to(outside)
    assert scan_for_credentials(tmp_path / "scanned") == []


# -- failure reasons --------------------------------------------------------------
@pytest.mark.parametrize(
    "failure, expected",
    [
        (
            "pi: subscription usage limit reached",
            "provider refused (subscription usage limit reached)",
        ),
        (
            "pi: model error after acceptance: your ChatGPT Plus plan limit",
            "provider refused (model error after acceptance)",
        ),
        (
            "pi: prompt rejected before acceptance: no credential",
            "provider refused (prompt rejected before acceptance)",
        ),
        (
            "pi: the auth preflight refused the launch: status not_ready, provider 'openai'",
            "provider refused (the auth preflight refused the launch)",
        ),
        (
            "pi: model unavailable: openai/x is not among Pi's available models",
            "provider refused (model unavailable)",
        ),
        ("pi: protocol violation: final stopReason 'x'", "AutoForge failure (protocol violation)"),
        (
            "pi: thinking mismatch: configured high, Pi resolved medium",
            "AutoForge failure (thinking mismatch)",
        ),
        (
            "pi: exited before agent_settled (exit 1)",
            "AutoForge failure (exited before agent_settled (exit 1))",
        ),
    ],
)
def test_a_pi_failure_is_classified_by_its_category_alone(failure, expected):
    assert classify_failure(failure) == expected


# -- side effects -----------------------------------------------------------------
def test_entry_names_lists_names_only_and_nothing_for_a_missing_directory(tmp_path):
    assert entry_names(tmp_path / "absent") == frozenset()
    (tmp_path / "s" / "--home-x--").mkdir(parents=True)
    (tmp_path / "s" / "--home-x--" / "1.jsonl").write_text("not read")
    assert entry_names(tmp_path / "s") == {"--home-x--", str(Path("--home-x--") / "1.jsonl")}


def test_the_tree_snapshot_sees_content_mode_and_new_entries_only_outside_git_and_caches(
    tmp_path,
):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("x = 1\n")
    before = tree_snapshot(tmp_path)
    (tmp_path / ".git" / "index").write_text("changed")
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"\0")
    assert tree_snapshot(tmp_path) == {**before, "pkg": "directory"}
    (tmp_path / "a.py").chmod(0o755)
    assert tree_snapshot(tmp_path)["a.py"] != before["a.py"]
    (tmp_path / "a.py").chmod(0o644)
    (tmp_path / "a.py").write_text("x = 2\n")
    assert tree_snapshot(tmp_path)["a.py"] != before["a.py"]


def test_a_process_running_in_or_naming_the_directory_is_found(tmp_path):
    inside = tmp_path / "work" / "repo"
    inside.mkdir(parents=True)
    sleepers = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=inside),
        subprocess.Popen(
            [sys.executable, "-c", "import time, sys; time.sleep(60)", str(tmp_path / "work")]
        ),
    ]
    # Without /proc (not Linux) only a command line is visible.
    expected = sleepers if os.path.isdir("/proc/self") else sleepers[1:]
    try:
        found = processes_under(tmp_path / "work")
        assert {p.pid for p in expected} <= set(found)
        assert os.getpid() not in found
    finally:
        for proc in sleepers:
            proc.kill()
            proc.wait()
    assert processes_under(tmp_path / "work") == []


# -- the whole flow, rehearsed against the fake `pi` ---------------------------------
def _review_block(fingerprint: str) -> str:
    payload = {
        "phase": "REVIEW",
        "status": "success",
        "round": 1,
        "reviewed_workspace_fingerprint": fingerprint,
        "needs_fix_round": False,
        "findings": [],
        "observations": ["greet() does what the feature asks."],
    }
    return block(payload)


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    """A fake ``pi`` 1.0.1 signed in with ChatGPT, outside the smoke run's
    directory, and an agent directory of the test's own."""
    fake = PiFake(tmp_path / "fake")
    wrapper = tmp_path / "bin" / "pi"
    wrapper.parent.mkdir()
    wrapper.write_text(
        f'#!/bin/sh\nif [ "$1" = --version ]; then echo 1.0.1; exit 0; fi\n'
        f'exec "{fake.command}" "$@"\n'
    )
    wrapper.chmod(0o755)
    fake.arm(PiTurn(), call=0)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    (tmp_path / "smoke").mkdir()
    return fake, SmokeSettings(command=str(wrapper), model="openai/gpt-5.6-terra", effort="low")


def _arm_review(fake: PiFake, **turn):
    def arm(engine):
        text = _review_block(engine.state.workspace_fingerprint)
        fake.arm(PiTurn(text=text, **turn), call=1)

    return arm


def test_the_smoke_flow_passes_against_a_fake_pi(tmp_path, rehearsal):
    fake, settings = rehearsal
    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report, before_review=_arm_review(fake))
    text = report.render()
    assert report.ok, text
    assert all(f"[PASS] {name}" in text for name in STEPS)
    assert report.facts["pi"] == "1.0.1"
    assert report.facts["review outcome"] == "clean, the run is DONE"
    assert "Pi resolved openai/gpt-5.6-terra at thinking low" in report.facts["review"]
    [launch] = fake.launches()
    assert "--no-session" in launch["argv"] and "OPENAI_API_KEY" not in launch["env"]
    auth = [e for e in fake.auth_checks() if "--no-refresh" in e["argv"]]
    assert len(auth) == 3  # step 1, doctor, and the launch's own preflight
    assert all("--credentials" not in e["argv"] for e in fake.entries() if "argv" in e)


def test_a_provider_refusal_is_named_and_the_later_checks_still_run(tmp_path, rehearsal):
    """A usage limit is reported as the provider's refusal by its category;
    Pi's error text (which may name the plan) never reaches the report."""
    fake, settings = rehearsal
    error = "subscription_sharing_usage_limit_exceeded: your ChatGPT Plus plan, " + _SECRET
    report = SmokeReport()
    arm = _arm_review(fake, outcome="stop_error", error=error)
    run_smoke(tmp_path / "smoke", settings, report, before_review=arm)
    text = report.render()
    assert not report.ok
    assert f"[FAIL] {STEPS[3]}: provider refused (subscription usage limit reached)" in text
    assert f"[PASS] {STEPS[4]}" in text and f"[PASS] {STEPS[5]}" in text
    assert "Plus" not in text and _SECRET not in text


def test_a_review_that_writes_into_the_tree_fails_twice_over(tmp_path, rehearsal):
    fake, settings = rehearsal
    report = SmokeReport()
    arm = _arm_review(fake, writes={"greet.py": "def greet(name):\n    return 'hi'\n"})
    run_smoke(tmp_path / "smoke", settings, report, before_review=arm)
    assert report.outcomes[STEPS[3]][1].startswith("AutoForge failure (VerificationError: ")
    assert report.outcomes[STEPS[5]] == (False, "the review changed the working tree")


def test_a_new_session_entry_fails_the_side_effect_step(tmp_path, rehearsal):
    fake, settings = rehearsal

    def arm(engine):
        _arm_review(fake)(engine)
        (tmp_path / "agent" / "sessions" / "--x--").mkdir(parents=True)

    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report, before_review=arm)
    assert report.passed(STEPS[3])
    assert (
        report.outcomes[STEPS[5]][1] == f"1 new entr(ies) under {tmp_path / 'agent' / 'sessions'}"
    )


def test_an_api_key_fails_step_2_is_not_unset_and_stops_before_any_launch(
    tmp_path, rehearsal, monkeypatch
):
    fake, settings = rehearsal
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report, before_review=_arm_review(fake))
    text = report.render()
    assert f"[FAIL] {STEPS[1]}: OPENAI_API_KEY is set: unset it for this test" in text
    assert all(f"[NOT RUN] {name}" in text for name in STEPS[3:])
    assert os.environ["OPENAI_API_KEY"] == _SECRET and _SECRET not in text
    assert fake.launches() == []


def test_a_missing_pi_fails_the_preconditions_and_launches_nothing(tmp_path, rehearsal):
    _, settings = rehearsal
    report = SmokeReport()
    missing = SmokeSettings(command=str(tmp_path / "nowhere" / "pi"), model=settings.model)
    run_smoke(tmp_path / "smoke", missing, report)
    assert report.outcomes[STEPS[0]][1].endswith("is not on PATH: install Pi yourself (docs/pi.md)")
    assert not report.passed(STEPS[2])
    assert all(name not in report.outcomes for name in STEPS[3:])
