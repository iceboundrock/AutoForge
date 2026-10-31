"""The live Pi smoke test's machinery, offline (#135).

The live tests themselves (``tests/test_live_pi.py``) need a real ``pi``
and a real credential, so they never run here. What is tested instead: that
the default options deselect them, the settings that choose the auth route,
that the credential scan finds each pattern (and a forwarded key's value)
and names it without the match, the other checks and failure reasons, and
both flows rehearsed against the fake ``pi`` of ``tests/pi_fake.py`` on
both routes, passing and failing: on the API-key route an auth check that
is not ready, of the other type, unreadable, or for a model Pi does not
resolve, and a review answer AutoForge cannot parse. The LOCAL flow runs
the real ``autoforge local doctor`` in a process of its own and the real
engine and ``PiProvider`` on a LOCAL run; the REMOTE flow (ADR 0009
Appendix A.2.3 G) the real ``autoforge doctor`` and a REMOTE run against
the GitHub stand-in, with one rehearsal for each LOCAL one, and that its
stand-in origin is the only repository its seeding writes to, whatever URL
rewrite git's configuration holds (#386 R1-F1). The git that flow runs
itself writes to no repository a ``GIT_*`` variable names and runs no hook,
file-system monitor or signing program of the operator's (#386 R2-F1).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from autoforge.config import ProfileConfig
from autoforge.git_transport import NO_AUTO_MAINTENANCE
from autoforge.providers import AgentRequest
from autoforge.redaction import redact
from tests.conftest import (
    GIT_IDENT,
    Origin,
    block,
    commit_in,
    git_out,
    git_repo,
    github_origin,
    review_result,
    state_tree,
)
from tests.live_pi import (
    AUTH_VARIABLE,
    CREDENTIAL_PATTERNS,
    DEFAULT_MODELS,
    FORWARD_VARIABLE,
    IMPLEMENTATION,
    LIVE_VARIABLE,
    MODEL_VARIABLE,
    PROVIDER_KEY_NAMES,
    REMOTE_MODE,
    REMOTE_TREE_CHANGES,
    STEPS,
    SmokeReport,
    SmokeSettings,
    StepFailed,
    autoforge_commit,
    classify_failure,
    credential_hits,
    entry_names,
    implement_in_worktree,
    processes_under,
    remote_trees,
    run_remote_smoke,
    run_smoke,
    scan_for_credentials,
    settings_from_environment,
    tree_snapshot,
    worktree_head,
    write_repository,
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


LIVE_TESTS = (
    "test_one_local_review_runs_on_the_operators_real_pi",
    "test_one_remote_review_runs_on_the_operators_real_pi",
)


def test_the_live_test_is_deselected_by_default_and_skips_without_its_variable():
    """The default options leave the live tests, LOCAL and REMOTE, out of
    collection (so `make check` and CI never run them); `-m live` selects
    them, and without AUTOFORGE_LIVE_PI=1 each skips with its reason and
    launches nothing."""
    default = _pytest("--collect-only")
    assert default.returncode == 5, default.stdout  # nothing collected
    assert f"{len(LIVE_TESTS)} deselected" in default.stdout
    assert all(name not in default.stdout for name in LIVE_TESTS)

    selected = _pytest("-m", "live")
    assert selected.returncode == 0, selected.stdout
    assert f"{len(LIVE_TESTS)} skipped" in selected.stdout
    assert f"set {LIVE_VARIABLE}=1 to run the live Pi smoke test" in selected.stdout


# -- the settings -----------------------------------------------------------------
def test_the_route_is_never_defaulted_and_each_takes_its_own_default_model():
    with pytest.raises(ValueError, match=f"set {AUTH_VARIABLE} to api_key .* or oauth"):
        settings_from_environment({})
    with pytest.raises(ValueError, match="got 'subscription'"):
        settings_from_environment({AUTH_VARIABLE: "subscription"})
    api_key = settings_from_environment({AUTH_VARIABLE: "api_key"})
    assert (api_key.model, api_key.effort, api_key.require_oauth) == (
        "deepseek/deepseek-flash",
        "low",
        False,
    )
    oauth = settings_from_environment({AUTH_VARIABLE: "oauth", MODEL_VARIABLE: "openai/x"})
    assert (oauth.model, oauth.require_oauth, oauth.forward_key) == ("openai/x", True, "")


def test_only_the_api_key_route_forwards_a_key_and_only_one_named_like_a_key():
    forwarded = settings_from_environment(
        {AUTH_VARIABLE: "api_key", FORWARD_VARIABLE: "DEEPSEEK_API_KEY"}
    )
    assert forwarded.forward_key == "DEEPSEEK_API_KEY"
    with pytest.raises(ValueError, match="the oauth route takes none"):
        settings_from_environment({AUTH_VARIABLE: "oauth", FORWARD_VARIABLE: "OPENAI_API_KEY"})
    with pytest.raises(ValueError, match=r"must name a provider key \(NAME_API_KEY\)"):
        settings_from_environment({AUTH_VARIABLE: "api_key", FORWARD_VARIABLE: "GH_TOKEN"})


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


def test_a_deepseek_shaped_key_is_found_by_its_shape_and_redacted(tmp_path):
    key = "sk-" + "0123456789abcdef" * 2
    (tmp_path / "stderr.log").write_text(f"DEEPSEEK_API_KEY={key}\n")
    assert scan_for_credentials(tmp_path) == [("stderr.log", "sk- key")]
    assert key not in redact(f"DEEPSEEK_API_KEY={key}")


def test_a_forwarded_keys_value_is_found_whatever_its_shape_and_never_shown(tmp_path):
    """A key forwarded on purpose is searched for by value too: a shape the
    patterns (and redaction) do not know still counts."""
    value = "dsk" + _SECRET
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "state.json").write_text(f'{{"note": "{value}"}}\n')
    assert credential_hits(value) == []
    hits = scan_for_credentials(tmp_path, {"DEEPSEEK_API_KEY": value})
    assert hits == [(str(Path("logs") / "state.json"), "the value of DEEPSEEK_API_KEY")]
    assert value not in repr(hits)


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


def test_the_remote_trees_see_the_checkout_the_worktree_and_the_worktrees_head(tmp_path):
    """What step 6 compares around a REMOTE review: a write into the checkout
    or the worktree, and a commit in the worktree (which the fake `pi` cannot
    make), each named by its own message."""
    checkout = git_repo(tmp_path / "checkout")
    worktree = tmp_path / "worktree"
    git_out("-C", str(checkout), "worktree", "add", "-q", "--detach", str(worktree))
    before = remote_trees(checkout, worktree)
    assert before["worktree HEAD"] == git_out("-C", str(checkout), "rev-parse", "HEAD")

    def changed() -> list[str]:
        after = remote_trees(checkout, worktree)
        return [
            message for key, message in REMOTE_TREE_CHANGES.items() if after[key] != before[key]
        ]

    assert changed() == []
    commit_in(worktree, "a reviewer's commit")
    assert changed() == ["the review moved its worktree's HEAD"]
    (worktree / "x.py").write_text("x = 1\n")
    (checkout / "y.py").write_text("y = 1\n")
    assert changed() == [
        "the review changed the checkout",
        "the review changed its worktree",
        "the review moved its worktree's HEAD",
    ]


def test_a_process_running_in_or_naming_the_directory_is_found(tmp_path):
    inside = tmp_path / "work" / "repo"
    inside.mkdir(parents=True)
    sleep = "import time; print('ready', flush=True); time.sleep(60)"
    sleepers = [
        subprocess.Popen([sys.executable, "-c", sleep], cwd=inside, stdout=subprocess.PIPE),
        subprocess.Popen(
            [sys.executable, "-c", sleep, str(tmp_path / "work")], stdout=subprocess.PIPE
        ),
    ]
    # Without /proc (not Linux) only a command line is visible.
    expected = sleepers if os.path.isdir("/proc/self") else sleepers[1:]
    try:
        # Popen returns once the child's exec has begun, which can be before
        # its new command line is in /proc: under load the scan read an empty
        # one (#340). Each sleeper says when it runs.
        for proc in sleepers:
            assert proc.stdout is not None and proc.stdout.readline() == b"ready\n"
        found = processes_under(tmp_path / "work")
        assert {p.pid for p in expected} <= set(found)
        assert os.getpid() not in found
    finally:
        for proc in sleepers:
            proc.kill()
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
    assert processes_under(tmp_path / "work") == []


# -- the whole flow, rehearsed against the fake `pi` ---------------------------------
def _review_block(fingerprint: str, observation: str) -> str:
    payload = {
        "phase": "REVIEW",
        "status": "success",
        "round": 1,
        "reviewed_workspace_fingerprint": fingerprint,
        "needs_fix_round": False,
        "findings": [],
        "observations": [observation],
    }
    return block(payload)


def _ready(model: str, auth_type: str) -> dict:
    return {"status": "ready", "provider": model.split("/", 1)[0], "authType": auth_type}


@dataclass
class Rehearsal:
    """A fake ``pi`` 1.0.1 outside the smoke run's directory, and an agent
    directory of the test's own."""

    fake: PiFake
    command: str

    def settings(self, auth: str = "api_key", **fields) -> SmokeSettings:
        """A run on ``auth``'s route and its default model, the fake's auth
        check answering ``ready`` with the route's authType unless ``check``
        (and ``check_exit``) say otherwise."""
        model = fields.pop("model", DEFAULT_MODELS[auth])
        check = fields.pop("check", None) or _ready(model, auth)
        self.fake.arm(PiTurn(auth=check, auth_exit=fields.pop("check_exit", 0)), call=0)
        return SmokeSettings(command=self.command, auth=auth, model=model, effort="low", **fields)


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    fake = PiFake(tmp_path / "fake")
    wrapper = tmp_path / "bin" / "pi"
    wrapper.parent.mkdir()
    wrapper.write_text(
        f'#!/bin/sh\nif [ "$1" = --version ]; then echo 1.0.1; exit 0; fi\n'
        f'exec "{fake.command}" "$@"\n'
    )
    wrapper.chmod(0o755)
    for name in PROVIDER_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    (tmp_path / "smoke").mkdir()
    return Rehearsal(fake, str(wrapper))


def _arm_review(
    fake: PiFake,
    observation: str = "greet() does what the feature asks.",
    text: str | None = None,
    **turn,
):
    """Arm the review: a clean result carrying ``observation``, or ``text`` as it is."""

    def arm(engine):
        if text is None:
            answer = _review_block(engine.state.workspace_fingerprint, observation)
        else:
            answer = text
        fake.arm(PiTurn(text=answer, **turn), call=1)

    return arm


def _config(tmp_path) -> dict:
    return json.loads((tmp_path / "smoke" / "autoforge.json").read_text())


def test_the_smoke_flow_passes_on_the_api_key_route_against_a_fake_pi(tmp_path, rehearsal):
    """The acceptance route: DeepSeek on a key in Pi's own store, with
    require_oauth false, so no per-launch preflight; no key in any launch's
    environment, and the report names the route, the authType and the
    commit."""
    settings = rehearsal.settings("api_key")
    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report, before_review=_arm_review(rehearsal.fake))
    text = report.render()
    assert report.ok, text
    assert all(f"[PASS] {name}" in text for name in STEPS)
    assert "REDACTED" not in text  # nothing in it is, or reads as, a credential
    facts = report.facts
    assert facts["auth route"] == "api_key (require_oauth: false)"
    assert facts["auth"] == "Pi resolved authType api_key for provider 'deepseek'"
    assert facts["credential"] == "Pi's own credential store (no provider key forwarded)"
    assert facts["model"] == "deepseek/deepseek-flash" and facts["thinking"] == "low"
    assert re.fullmatch(r"[0-9a-f]{40}( with uncommitted changes)?", facts["AutoForge commit"])
    assert "authType api_key" in facts["doctor, auth ready"]
    assert "Pi resolved deepseek/deepseek-flash at thinking low" in facts["review"]
    assert facts["review outcome"] == "clean, the run is DONE"
    config = _config(tmp_path)
    assert config["profiles"]["review_round_1"]["options"] == {"require_oauth": False}
    assert "env_allowlist_extra" not in config["execution"]
    [launch] = rehearsal.fake.launches()
    assert "--no-session" in launch["argv"]
    assert not set(PROVIDER_KEY_NAMES) & set(launch["env"])
    auth = rehearsal.fake.auth_checks()
    assert len(auth) == 2 and all("--no-refresh" in e["argv"] for e in auth)  # step 1, doctor
    assert all("--credentials" not in e["argv"] for e in rehearsal.fake.entries() if "argv" in e)


def test_the_smoke_flow_passes_on_the_optional_oauth_route_against_a_fake_pi(tmp_path, rehearsal):
    settings = rehearsal.settings("oauth")
    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report, before_review=_arm_review(rehearsal.fake))
    assert report.ok, report.render()
    assert report.facts["pi"] == "1.0.1"
    assert report.facts["auth route"] == "oauth (require_oauth: true)"
    assert report.facts["auth"] == "Pi resolved authType oauth for provider 'openai'"
    assert "Pi resolved openai/gpt-5.6-terra at thinking low" in report.facts["review"]
    assert _config(tmp_path)["profiles"]["review_round_1"]["options"] == {"require_oauth": True}
    [launch] = rehearsal.fake.launches()
    assert "--no-session" in launch["argv"] and "OPENAI_API_KEY" not in launch["env"]
    auth = [e for e in rehearsal.fake.auth_checks() if "--no-refresh" in e["argv"]]
    assert len(auth) == 3  # step 1, doctor, and the launch's own preflight


def test_a_key_forwarded_on_purpose_is_named_reaches_the_launch_and_is_scanned_for(
    tmp_path, rehearsal, monkeypatch
):
    """The exception step 2 allows: the report names the route, never the
    value, and step 5 finds the value wherever AutoForge wrote it, whatever
    its shape."""
    value = "dsk" + _SECRET
    monkeypatch.setenv("DEEPSEEK_API_KEY", value)
    settings = rehearsal.settings("api_key", forward_key="DEEPSEEK_API_KEY")
    report = SmokeReport()
    arm = _arm_review(rehearsal.fake, observation=f"the key is {value}")
    run_smoke(tmp_path / "smoke", settings, report, before_review=arm)
    text = report.render()
    assert report.passed(STEPS[1]) and report.passed(STEPS[3])
    assert report.facts["credential"].startswith(
        "DEEPSEEK_API_KEY forwarded on purpose by execution.env_allowlist_extra"
    )
    assert _config(tmp_path)["execution"]["env_allowlist_extra"] == ["DEEPSEEK_API_KEY"]
    [launch] = rehearsal.fake.launches()
    assert "DEEPSEEK_API_KEY" in launch["env"] and "OPENAI_API_KEY" not in launch["env"]
    ok, message = report.outcomes[STEPS[4]]
    assert not ok and "the value of DEEPSEEK_API_KEY" in message
    assert value not in text and _SECRET not in text


def test_a_forward_naming_an_unset_key_fails_step_2(tmp_path, rehearsal):
    settings = rehearsal.settings("api_key", forward_key="DEEPSEEK_API_KEY")
    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report)
    assert report.outcomes[STEPS[1]] == (
        False,
        f"{FORWARD_VARIABLE} names DEEPSEEK_API_KEY, which is not set",
    )
    assert rehearsal.fake.launches() == []


def test_a_provider_refusal_is_named_and_the_later_checks_still_run(tmp_path, rehearsal):
    """A usage limit is reported as the provider's refusal by its category;
    Pi's error text (which may name the plan) never reaches the report."""
    settings = rehearsal.settings("oauth")
    error = "subscription_sharing_usage_limit_exceeded: your ChatGPT Plus plan, " + _SECRET
    report = SmokeReport()
    arm = _arm_review(rehearsal.fake, outcome="stop_error", error=error)
    run_smoke(tmp_path / "smoke", settings, report, before_review=arm)
    text = report.render()
    assert not report.ok
    assert f"[FAIL] {STEPS[3]}: provider refused (subscription usage limit reached)" in text
    assert f"[PASS] {STEPS[4]}" in text and f"[PASS] {STEPS[5]}" in text
    assert "Plus" not in text and _SECRET not in text


def test_a_model_error_on_the_api_key_route_is_the_providers_refusal(tmp_path, rehearsal):
    settings = rehearsal.settings("api_key")
    report = SmokeReport()
    arm = _arm_review(rehearsal.fake, outcome="stop_error", error="Insufficient Balance " + _SECRET)
    run_smoke(tmp_path / "smoke", settings, report, before_review=arm)
    assert report.outcomes[STEPS[3]] == (False, "provider refused (model error after acceptance)")
    assert _SECRET not in report.render()


@pytest.mark.parametrize(
    "check, exit_code, expected",
    [
        (
            {"status": "not_ready", "provider": "deepseek", "reason": "credentials_not_configured"},
            1,
            "status not_ready, provider 'deepseek', reason credentials_not_configured: "
            "Pi has no credential for 'deepseek'",
        ),
        (
            {"status": "ready", "provider": "openai", "authType": "api_key"},
            0,
            "Pi resolved model 'deepseek/deepseek-flash' to provider 'openai', "
            "not the configured 'deepseek'",
        ),
        (
            {"status": "ready", "provider": "deepseek", "authType": "api_key", "plan": "x"},
            0,
            "without a result AutoForge can read (1 unexpected field(s))",
        ),
    ],
    ids=["not-ready", "another-provider", "unreadable"],
)
def test_an_auth_check_that_does_not_allow_the_api_key_route_stops_before_any_launch(
    tmp_path, rehearsal, check, exit_code, expected
):
    """Not ready, misconfigured or unreadable: step 1 and doctor fail with
    the check's fields alone, and no review is started."""
    settings = rehearsal.settings("api_key", check=check, check_exit=exit_code)
    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report)
    ok, message = report.outcomes[STEPS[0]]
    assert not ok and message.startswith("no usable credential for deepseek/deepseek-flash: ")
    assert expected in message
    assert not report.passed(STEPS[2])
    assert all(name not in report.outcomes for name in STEPS[3:])
    assert rehearsal.fake.launches() == []
    assert "plan" not in report.render()


def test_a_model_pi_does_not_resolve_fails_the_preconditions(tmp_path, rehearsal):
    """Pi 1.3.0 answers a retired id (`deepseek-v4-flash`) with `invalid`."""
    model = "deepseek/deepseek-v4-flash"
    invalid = {"status": "invalid", "provider": model, "reason": "invalid_state"}
    settings = rehearsal.settings("api_key", model=model, check=invalid, check_exit=2)
    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report)
    ok, message = report.outcomes[STEPS[0]]
    assert not ok
    assert f"status invalid, provider '{model}', reason invalid_state" in message
    assert "Pi's model or credential configuration is invalid" in message
    assert rehearsal.fake.launches() == []


@pytest.mark.parametrize(
    "auth, resolved, expected",
    [
        (
            "api_key",
            "oauth",
            "Pi resolved authType oauth for deepseek/deepseek-flash, not the api_key route's",
        ),
        ("oauth", "api_key", "this profile requires Pi's ChatGPT sign-in; Pi resolved an API key"),
    ],
)
def test_a_credential_of_the_other_type_fails_its_route(
    tmp_path, rehearsal, auth, resolved, expected
):
    """An API-key run is no evidence for the sign-in, nor the reverse."""
    settings = rehearsal.settings(auth, check=_ready(DEFAULT_MODELS[auth], resolved))
    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report)
    assert expected in report.outcomes[STEPS[0]][1]
    assert not report.passed(STEPS[2])
    assert rehearsal.fake.launches() == []


def test_a_review_answer_without_a_valid_control_result_fails_step_4(tmp_path, rehearsal):
    """Output parsing on the API-key route: with no correction relaunch, an
    answer AutoForge cannot parse fails the review as AutoForge's failure,
    and the model's text is not shown."""
    settings = rehearsal.settings("api_key")
    report = SmokeReport()
    arm = _arm_review(rehearsal.fake, text="Looks fine to me. " + _SECRET)
    run_smoke(tmp_path / "smoke", settings, report, before_review=arm)
    ok, message = report.outcomes[STEPS[3]]
    assert not ok and message.startswith("AutoForge failure (")
    assert len(rehearsal.fake.launches()) == 1
    assert _SECRET not in report.render()


def test_a_review_that_writes_into_the_tree_fails_twice_over(tmp_path, rehearsal):
    settings = rehearsal.settings("api_key")
    report = SmokeReport()
    arm = _arm_review(rehearsal.fake, writes={"greet.py": "def greet(name):\n    return 'hi'\n"})
    run_smoke(tmp_path / "smoke", settings, report, before_review=arm)
    assert report.outcomes[STEPS[3]][1].startswith("AutoForge failure (VerificationError: ")
    assert report.outcomes[STEPS[5]] == (False, "the review changed the working tree")


def test_a_new_session_entry_fails_the_side_effect_step(tmp_path, rehearsal):
    settings = rehearsal.settings("api_key")

    def arm(engine):
        _arm_review(rehearsal.fake)(engine)
        (tmp_path / "agent" / "sessions" / "--x--").mkdir(parents=True)

    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report, before_review=arm)
    assert report.passed(STEPS[3])
    assert (
        report.outcomes[STEPS[5]][1] == f"1 new entr(ies) under {tmp_path / 'agent' / 'sessions'}"
    )


@pytest.mark.parametrize("auth", ["api_key", "oauth"])
@pytest.mark.parametrize("name", PROVIDER_KEY_NAMES)
def test_a_provider_key_fails_step_2_is_not_unset_and_stops_before_any_launch(
    tmp_path, rehearsal, monkeypatch, auth, name
):
    settings = rehearsal.settings(auth)
    monkeypatch.setenv(name, _SECRET)
    report = SmokeReport()
    run_smoke(tmp_path / "smoke", settings, report, before_review=_arm_review(rehearsal.fake))
    text = report.render()
    assert f"[FAIL] {STEPS[1]}: {name} is set: unset it for this test" in text
    assert all(f"[NOT RUN] {step}" in text for step in STEPS[3:])
    assert os.environ[name] == _SECRET and _SECRET not in text
    assert rehearsal.fake.launches() == []
    # Step 1 ran without it, as the launch would.
    assert all(name not in e["env"] for e in rehearsal.fake.auth_checks())


def test_a_missing_pi_fails_the_preconditions_and_launches_nothing(tmp_path, rehearsal):
    report = SmokeReport()
    missing = SmokeSettings(command=str(tmp_path / "nowhere" / "pi"))
    run_smoke(tmp_path / "smoke", missing, report)
    assert report.outcomes[STEPS[0]][1].endswith("is not on PATH: install Pi yourself (docs/pi.md)")
    assert not report.passed(STEPS[2])
    assert all(name not in report.outcomes for name in STEPS[3:])


# -- the REMOTE flow, rehearsed against the fake `pi` (ADR 0009 A.2.3 G) ---------------
# One counterpart of each LOCAL rehearsal above, on `run_remote_smoke`: the
# real `autoforge doctor` in a process of its own, the real engine and
# `PiProvider` on a REMOTE run, and the GitHub stand-in (FakeGitHub and a bare
# origin under the run's directory). S2 retires the LOCAL ones (ADR 0009 D12).
def _arm_remote_review(
    fake: PiFake,
    observation: str = "greet() does what the issue asks.",
    text: str | None = None,
    **turn,
):
    """Arm the REMOTE review: a clean result of the PR HEAD the controller
    bound, carrying ``observation``, or ``text`` as it is."""

    def arm(engine):
        if text is None:
            answer = block(
                review_result(1, engine.state.current_head_sha, observations=observation)
            )
        else:
            answer = text
        fake.arm(PiTurn(text=answer, **turn), call=1)

    return arm


def _remote(tmp_path, settings, before_review=None) -> SmokeReport:
    report = SmokeReport()
    run_remote_smoke(tmp_path / "smoke", settings, report, before_review=before_review)
    return report


def _worktrees(tmp_path) -> Path:
    """Where the REMOTE run's per-issue worktrees are: the checkout's git directory."""
    return tmp_path / "smoke" / "repo" / ".git" / "autoforge" / "worktrees"


def test_the_remote_smoke_flow_passes_on_the_api_key_route_against_a_fake_pi(tmp_path, rehearsal):
    """REMOTE counterpart of the acceptance route's rehearsal: the real
    `autoforge doctor` passes its Pi rows while its GitHub rows fail against
    the stand-in's `gh`; the controller opens the PR and posts the review
    comment on the stand-in; the reviewer runs in the per-issue worktree."""
    settings = rehearsal.settings("api_key")
    report = _remote(tmp_path, settings, _arm_remote_review(rehearsal.fake))
    text = report.render()
    assert report.ok, text
    assert all(f"[PASS] {name}" in text for name in STEPS)
    assert "REDACTED" not in text
    facts = report.facts
    assert facts["mode"] == REMOTE_MODE
    assert facts["auth route"] == "api_key (require_oauth: false)"
    assert facts["auth"] == "Pi resolved authType api_key for provider 'deepseek'"
    assert facts["credential"] == "Pi's own credential store (no provider key forwarded)"
    assert re.fullmatch(r"[0-9a-f]{40}( with uncommitted changes)?", facts["AutoForge commit"])
    assert "authType api_key" in facts["doctor, auth ready"]
    github_rows = facts["doctor, GitHub rows"]
    assert github_rows.startswith("failed, as expected: the stand-in's gh answers nothing (")
    assert "gh available" in github_rows and "gh authenticated" in github_rows
    assert "doctor, other failures" not in facts
    assert "Pi resolved deepseek/deepseek-flash at thinking low" in facts["review"]
    assert facts["GitHub stand-in, the controller's writes"] == (
        "create_pull_request, create_pr_comment"
    )
    assert facts["review outcome"] == (
        "clean, the run is READY_FOR_MERGE (nothing is merged: the merge gate is closed)"
    )
    config = _config(tmp_path)
    assert "local" not in config and "env_allowlist_extra" not in config["execution"]
    assert config["github"]["command"] == str(tmp_path / "smoke" / "fake-gh")
    assert config["state_dir"] == str(tmp_path / "smoke" / "state")
    for name in ("review_round_1", "review_round_2_5", "review_round_6_plus"):
        assert config["profiles"][name]["options"] == {"require_oauth": False}
    [launch] = rehearsal.fake.launches()
    assert "--no-session" in launch["argv"]
    assert Path(launch["cwd"]).is_relative_to(_worktrees(tmp_path))
    assert not set(PROVIDER_KEY_NAMES) & set(launch["env"])
    # The review is bound to real git data: the PR HEAD the controller pushed
    # to the bare origin, checked out in the worktree, and its merge base.
    head = git_out("-C", launch["cwd"], "rev-parse", "HEAD")
    assert git_out("-C", launch["cwd"], "show", "--format=", "--name-only", head) == "greet.py"
    [prompt] = rehearsal.fake.prompts()
    assert head in prompt and git_out("-C", launch["cwd"], "merge-base", "main", head) in prompt
    auth = rehearsal.fake.auth_checks()
    assert len(auth) == 2 and all("--no-refresh" in e["argv"] for e in auth)  # step 1, doctor
    assert all("--credentials" not in e["argv"] for e in rehearsal.fake.entries() if "argv" in e)
    # The checkout holds no state of the run: it is under smoke/state.
    assert not (tmp_path / "smoke" / "repo" / ".autoforge").exists()


def test_the_remote_smoke_flow_passes_on_the_optional_oauth_route_against_a_fake_pi(
    tmp_path, rehearsal
):
    settings = rehearsal.settings("oauth")
    report = _remote(tmp_path, settings, _arm_remote_review(rehearsal.fake))
    assert report.ok, report.render()
    assert report.facts["pi"] == "1.0.1"
    assert report.facts["auth route"] == "oauth (require_oauth: true)"
    assert report.facts["auth"] == "Pi resolved authType oauth for provider 'openai'"
    assert "Pi resolved openai/gpt-5.6-terra at thinking low" in report.facts["review"]
    assert _config(tmp_path)["profiles"]["review_round_1"]["options"] == {"require_oauth": True}
    [launch] = rehearsal.fake.launches()
    assert "--no-session" in launch["argv"] and "OPENAI_API_KEY" not in launch["env"]
    auth = [e for e in rehearsal.fake.auth_checks() if "--no-refresh" in e["argv"]]
    assert len(auth) == 3  # step 1, doctor, and the launch's own preflight


def test_a_key_forwarded_on_purpose_reaches_the_remote_launch_and_is_found_on_github_too(
    tmp_path, rehearsal, monkeypatch
):
    """The exception step 2 allows, on a REMOTE run: the report names the
    route, never the value, and step 5 finds the value in the run's logs and
    in the review comment the controller sent to the GitHub stand-in."""
    value = "dsk" + _SECRET
    monkeypatch.setenv("DEEPSEEK_API_KEY", value)
    settings = rehearsal.settings("api_key", forward_key="DEEPSEEK_API_KEY")
    arm = _arm_remote_review(rehearsal.fake, observation=f"the key is {value}")
    report = _remote(tmp_path, settings, arm)
    text = report.render()
    assert report.passed(STEPS[1]) and report.passed(STEPS[3])
    assert report.facts["credential"].startswith(
        "DEEPSEEK_API_KEY forwarded on purpose by execution.env_allowlist_extra"
    )
    assert _config(tmp_path)["execution"]["env_allowlist_extra"] == ["DEEPSEEK_API_KEY"]
    [launch] = rehearsal.fake.launches()
    assert "DEEPSEEK_API_KEY" in launch["env"] and "OPENAI_API_KEY" not in launch["env"]
    ok, message = report.outcomes[STEPS[4]]
    assert not ok
    assert "<GitHub stand-in: create_pr_comment>: the value of DEEPSEEK_API_KEY" in message
    assert re.search(r"(^|; )logs/[^;]*: the value of DEEPSEEK_API_KEY", message), message
    assert value not in text and _SECRET not in text


def test_a_forward_naming_an_unset_key_fails_remote_step_2(tmp_path, rehearsal):
    settings = rehearsal.settings("api_key", forward_key="DEEPSEEK_API_KEY")
    report = _remote(tmp_path, settings)
    assert report.outcomes[STEPS[1]] == (
        False,
        f"{FORWARD_VARIABLE} names DEEPSEEK_API_KEY, which is not set",
    )
    assert all(name not in report.outcomes for name in STEPS[3:])
    assert rehearsal.fake.launches() == []


def test_a_provider_refusal_of_the_remote_review_is_named_and_the_later_checks_still_run(
    tmp_path, rehearsal
):
    """A usage limit is reported as the provider's refusal by its category;
    Pi's error text (which may name the plan) never reaches the report, and
    nothing was posted on the stand-in for the round."""
    settings = rehearsal.settings("oauth")
    error = "subscription_sharing_usage_limit_exceeded: your ChatGPT Plus plan, " + _SECRET
    arm = _arm_remote_review(rehearsal.fake, outcome="stop_error", error=error)
    report = _remote(tmp_path, settings, arm)
    text = report.render()
    assert not report.ok
    assert f"[FAIL] {STEPS[3]}: provider refused (subscription usage limit reached)" in text
    assert f"[PASS] {STEPS[4]}" in text and f"[PASS] {STEPS[5]}" in text
    assert report.facts["GitHub stand-in, the controller's writes"] == "create_pull_request"
    assert "Plus" not in text and _SECRET not in text


def test_a_model_error_of_the_remote_review_on_the_api_key_route_is_the_providers_refusal(
    tmp_path, rehearsal
):
    settings = rehearsal.settings("api_key")
    error = "Insufficient Balance " + _SECRET
    arm = _arm_remote_review(rehearsal.fake, outcome="stop_error", error=error)
    report = _remote(tmp_path, settings, arm)
    assert report.outcomes[STEPS[3]] == (False, "provider refused (model error after acceptance)")
    assert _SECRET not in report.render()


@pytest.mark.parametrize(
    "check, exit_code, expected",
    [
        (
            {"status": "not_ready", "provider": "deepseek", "reason": "credentials_not_configured"},
            1,
            "status not_ready, provider 'deepseek', reason credentials_not_configured: "
            "Pi has no credential for 'deepseek'",
        ),
        (
            {"status": "ready", "provider": "openai", "authType": "api_key"},
            0,
            "Pi resolved model 'deepseek/deepseek-flash' to provider 'openai', "
            "not the configured 'deepseek'",
        ),
        (
            {"status": "ready", "provider": "deepseek", "authType": "api_key", "plan": "x"},
            0,
            "without a result AutoForge can read (1 unexpected field(s))",
        ),
    ],
    ids=["not-ready", "another-provider", "unreadable"],
)
def test_an_auth_check_that_does_not_allow_the_api_key_route_stops_the_remote_flow_first(
    tmp_path, rehearsal, check, exit_code, expected
):
    """Not ready, misconfigured or unreadable: step 1 and the REMOTE doctor
    fail with the check's fields alone, and no run is started: no worktree,
    nothing on the stand-in."""
    settings = rehearsal.settings("api_key", check=check, check_exit=exit_code)
    report = _remote(tmp_path, settings)
    ok, message = report.outcomes[STEPS[0]]
    assert not ok and message.startswith("no usable credential for deepseek/deepseek-flash: ")
    assert expected in message
    assert not report.passed(STEPS[2])
    assert all(name not in report.outcomes for name in STEPS[3:])
    assert rehearsal.fake.launches() == [] and not _worktrees(tmp_path).exists()
    assert "plan" not in report.render()


def test_a_model_pi_does_not_resolve_fails_the_remote_preconditions(tmp_path, rehearsal):
    model = "deepseek/deepseek-v4-flash"
    invalid = {"status": "invalid", "provider": model, "reason": "invalid_state"}
    settings = rehearsal.settings("api_key", model=model, check=invalid, check_exit=2)
    report = _remote(tmp_path, settings)
    ok, message = report.outcomes[STEPS[0]]
    assert not ok
    assert f"status invalid, provider '{model}', reason invalid_state" in message
    assert "Pi's model or credential configuration is invalid" in message
    assert rehearsal.fake.launches() == []


@pytest.mark.parametrize(
    "auth, resolved, expected",
    [
        (
            "api_key",
            "oauth",
            "Pi resolved authType oauth for deepseek/deepseek-flash, not the api_key route's",
        ),
        ("oauth", "api_key", "this profile requires Pi's ChatGPT sign-in; Pi resolved an API key"),
    ],
)
def test_a_credential_of_the_other_type_fails_its_route_in_the_remote_flow(
    tmp_path, rehearsal, auth, resolved, expected
):
    settings = rehearsal.settings(auth, check=_ready(DEFAULT_MODELS[auth], resolved))
    report = _remote(tmp_path, settings)
    assert expected in report.outcomes[STEPS[0]][1]
    assert not report.passed(STEPS[2])
    assert rehearsal.fake.launches() == []


def test_a_remote_review_answer_without_a_valid_control_result_fails_step_4(tmp_path, rehearsal):
    """With no correction relaunch, an answer AutoForge cannot parse fails
    the REMOTE review as AutoForge's failure, nothing is posted for the
    round, and the model's text is not shown."""
    settings = rehearsal.settings("api_key")
    arm = _arm_remote_review(rehearsal.fake, text="Looks fine to me. " + _SECRET)
    report = _remote(tmp_path, settings, arm)
    ok, message = report.outcomes[STEPS[3]]
    assert not ok
    assert message.startswith("AutoForge failure (ControlResultValidationError: "), message
    assert "did not return a valid CONTROL_RESULT after 1 attempt(s)" in message
    assert len(rehearsal.fake.launches()) == 1
    assert report.facts["GitHub stand-in, the controller's writes"] == "create_pull_request"
    assert _SECRET not in report.render()


def test_a_remote_review_that_writes_into_its_worktree_fails_the_side_effect_step(
    tmp_path, rehearsal
):
    """The LOCAL rehearsal fails twice over; this one fails once, and that
    difference is the controller's, documented here rather than fixed. A
    REMOTE review is bound to the PR HEAD commit, which a write into the
    worktree does not change, so the controller accepts the review and posts
    its comment (step 4 passes); only step 6 sees the write."""
    settings = rehearsal.settings("api_key")
    writes = {"greet.py": "def greet(name):\n    return 'hi'\n"}
    report = _remote(tmp_path, settings, _arm_remote_review(rehearsal.fake, writes=writes))
    assert report.passed(STEPS[3])
    assert report.facts["GitHub stand-in, the controller's writes"] == (
        "create_pull_request, create_pr_comment"
    )
    assert report.outcomes[STEPS[5]] == (False, "the review changed its worktree")
    [worktree] = _worktrees(tmp_path).iterdir()
    assert (worktree / "greet.py").read_text() == writes["greet.py"]


def test_a_new_session_entry_fails_the_remote_side_effect_step(tmp_path, rehearsal):
    settings = rehearsal.settings("api_key")

    def arm(engine):
        _arm_remote_review(rehearsal.fake)(engine)
        (tmp_path / "agent" / "sessions" / "--x--").mkdir(parents=True)

    report = _remote(tmp_path, settings, arm)
    assert report.passed(STEPS[3])
    assert (
        report.outcomes[STEPS[5]][1] == f"1 new entr(ies) under {tmp_path / 'agent' / 'sessions'}"
    )


@pytest.mark.parametrize("auth", ["api_key", "oauth"])
@pytest.mark.parametrize("name", PROVIDER_KEY_NAMES)
def test_a_provider_key_fails_remote_step_2_is_not_unset_and_stops_before_any_launch(
    tmp_path, rehearsal, monkeypatch, auth, name
):
    settings = rehearsal.settings(auth)
    monkeypatch.setenv(name, _SECRET)
    report = _remote(tmp_path, settings, _arm_remote_review(rehearsal.fake))
    text = report.render()
    assert f"[FAIL] {STEPS[1]}: {name} is set: unset it for this test" in text
    assert all(f"[NOT RUN] {step}" in text for step in STEPS[3:])
    assert os.environ[name] == _SECRET and _SECRET not in text
    assert rehearsal.fake.launches() == [] and not _worktrees(tmp_path).exists()
    # Step 1 ran without it, as the launch would.
    assert all(name not in e["env"] for e in rehearsal.fake.auth_checks())


def test_a_missing_pi_fails_the_remote_preconditions_and_launches_nothing(tmp_path, rehearsal):
    missing = SmokeSettings(command=str(tmp_path / "nowhere" / "pi"))
    report = _remote(tmp_path, missing)
    assert report.outcomes[STEPS[0]][1].endswith("is not on PATH: install Pi yourself (docs/pi.md)")
    assert not report.passed(STEPS[2])
    assert all(name not in report.outcomes for name in STEPS[3:])


# -- the REMOTE flow's origin, seeded inside the run's directory (#386 R1-F1) ----------
# An operator's git configuration or environment may rewrite a URL
# (`url.<other>.insteadOf`, `pushInsteadOf`), name a credential helper, an SSH
# command, an askpass program or a hooks directory. The stand-in origin is
# seeded and published to by `Origin`, whose git reads none of that and uses
# no transport, so none of it can send the seed to another repository,
# reach a network remote, or run.
REWRITE_ROUTES = (
    "GIT_CONFIG_GLOBAL",
    "~/.gitconfig",
    "XDG_CONFIG_HOME",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "the checkout's .git/config",
)
# Where the rewrite sends the seed: another repository on this machine, or a
# network remote whose SSH command is the sentinel (so nothing reaches it).
REWRITE_DESTINATIONS = ("another file:// repository", "an ssh:// remote")
# The hooks a push, a ref update or a commit could run from a planted directory.
PLANTED_HOOKS = ("pre-push", "reference-transaction", "post-commit", "pre-receive", "update")


def holds_nothing(bare: Path) -> bool:
    """``bare`` holds no ref and no object, read by the test's own git."""
    objects = set(git_out("--git-dir", str(bare), "count-objects", "-v").splitlines())
    refs = git_out("--git-dir", str(bare), "for-each-ref")
    return refs == "" and {"count: 0", "in-pack: 0"} <= objects


@dataclass
class Planted:
    """The repository a planted configuration redirects to, and the log of
    every sentinel it would run."""

    decoy: Path
    log: Path

    def ran(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


def _sentinel(path: Path, log: Path, label: str, status: int = 1) -> Path:
    """A program that logs it ran and exits ``status`` at once (failing by
    default), reading nothing (an SSH command that waited for its stdin would
    hang the git that started it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{label} $*" >> "{log}"\nexit {status}\n')
    path.chmod(0o755)
    return path


def _plant_rewrites(
    route: str, destination: str, url: str, checkout: Path, tmp_path: Path, monkeypatch
) -> Planted:
    """Make every git that reads ``route`` rewrite ``url`` to ``destination``,
    with a credential helper, an SSH command, an askpass program and a
    hooks directory of sentinels; the environment routes (``GIT_SSH_COMMAND``,
    ``GIT_ASKPASS``, ``SSH_ASKPASS``) are planted whatever the route."""
    planted = tmp_path / "planted"
    log = planted / "ran.log"
    decoy = planted / "decoy.git"
    git_out("init", "-q", "--bare", "-b", "main", str(decoy))
    target = (
        f"file://{decoy}"
        if destination.startswith("another")
        else "ssh://git@example.invalid/owner/repo.git"
    )
    hooks = planted / "hooks"
    for name in PLANTED_HOOKS:
        _sentinel(hooks / name, log, f"hook {name}")
    entries = {
        f"url.{target}.insteadOf": url,
        f"url.{target}.pushInsteadOf": url,
        "credential.helper": f"!{_sentinel(planted / 'credential', log, 'credential')}",
        "core.sshCommand": str(_sentinel(planted / "ssh", log, "core.sshCommand")),
        "core.askPass": str(_sentinel(planted / "askpass", log, "core.askPass")),
        "core.hooksPath": str(hooks),
    }
    monkeypatch.setenv("GIT_SSH_COMMAND", str(_sentinel(planted / "ssh-env", log, "GIT_SSH")))
    monkeypatch.setenv("GIT_ASKPASS", str(_sentinel(planted / "askpass-env", log, "GIT_ASKPASS")))
    monkeypatch.setenv("SSH_ASKPASS", str(_sentinel(planted / "ssh-askpass", log, "SSH_ASKPASS")))
    _plant_config(route, entries, checkout, planted, monkeypatch)
    return Planted(decoy=decoy, log=log)


def _plant_config(
    route: str, entries: dict[str, str], checkout: Path | None, planted: Path, monkeypatch
) -> None:
    """Make every git that reads ``route`` read ``entries`` (``checkout`` is
    the repository of the checkout's route). Whatever the route, ``HOME`` and
    ``XDG_CONFIG_HOME`` are a home in ``planted``, and no ``GIT_CONFIG*``
    variable this process's own environment set stays."""
    for name in [n for n in os.environ if n.startswith("GIT_CONFIG")]:
        monkeypatch.delenv(name)  # what this process's own environment set
    home = planted / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    def write(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        for key, value in entries.items():
            git_out("config", "--file", str(path), key, value)

    if route == "GIT_CONFIG_GLOBAL":
        write(planted / "global.gitconfig")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(planted / "global.gitconfig"))
    elif route == "~/.gitconfig":
        write(home / ".gitconfig")
    elif route == "XDG_CONFIG_HOME":
        write(home / ".config" / "git" / "config")
    elif route == "GIT_CONFIG_SYSTEM":
        write(planted / "system.gitconfig")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(planted / "system.gitconfig"))
    elif route == "GIT_CONFIG_COUNT":
        monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(entries)))
        for i, (key, value) in enumerate(entries.items()):
            monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", key)
            monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", value)
    elif route == "GIT_CONFIG_PARAMETERS":
        monkeypatch.setenv(
            "GIT_CONFIG_PARAMETERS", " ".join(f"'{k}'='{v}'" for k, v in entries.items())
        )
    else:
        assert checkout is not None, route
        write(checkout / ".git" / "config")


@pytest.mark.parametrize("destination", REWRITE_DESTINATIONS)
@pytest.mark.parametrize("route", REWRITE_ROUTES)
def test_the_origin_is_seeded_and_published_to_alone_whatever_rewrites_the_url(
    tmp_path, monkeypatch, route, destination
):
    """ADR 0009 A.2.3 G (#386 R1-F1): with a rewrite of the stand-in origin's
    URL planted on each route git reads one from (to another repository, or
    to an SSH remote whose command is a sentinel), and a credential helper,
    SSH command, askpass and hooks of sentinels beside it, the seeding and a
    forced publish write into the origin alone: the other repository holds no
    ref and no object, and no sentinel ran. A plain `git push -f` to the
    origin's `file://` URL, the seeding before this fix, fails every case."""
    checkout = git_repo(tmp_path / "checkout")
    first = git_out("-C", str(checkout), "rev-parse", "HEAD")
    second = commit_in(checkout, "second")
    where = tmp_path / "stand-in"
    url = f"file://{where / 'origin.git'}"
    with monkeypatch.context() as planting:
        planted = _plant_rewrites(route, destination, url, checkout, tmp_path, planting)
        origin = Origin.seeded(where, checkout)
        assert origin.url == url
        origin.publish(checkout, first, "feature")
        origin.publish(checkout, second, "feature")
        origin.publish(checkout, first, "feature")  # forced: not a fast-forward
        origin.delete("main")
        origin.publish(checkout, second, "main")
    assert planted.ran() == []
    assert holds_nothing(planted.decoy)
    refs = git_out(
        "--git-dir", str(where / "origin.git"), "for-each-ref", "--format=%(refname) %(objectname)"
    )
    assert refs.splitlines() == [f"refs/heads/feature {first}", f"refs/heads/main {second}"]


def test_the_remote_flow_seeds_its_own_origin_whatever_the_operators_gitconfig_rewrites(
    tmp_path, rehearsal, monkeypatch
):
    """ADR 0009 A.2.3 G (#386 R1-F1): the operator's `~/.gitconfig` rewrites
    the URL of the run's stand-in origin, for fetches and pushes, to another
    repository. The REMOTE flow still passes: its origin is seeded with the
    checkout's `main`, the controller pushes the PR's head there, and the
    other repository holds no ref and no object."""
    where = tmp_path / "smoke"
    url = f"file://{where / 'origin.git'}"
    decoy = tmp_path / "decoy.git"
    git_out("init", "-q", "--bare", "-b", "main", str(decoy))
    home = tmp_path / "home"
    home.mkdir()
    for key in ("insteadOf", "pushInsteadOf"):
        git_out("config", "--file", str(home / ".gitconfig"), f"url.file://{decoy}.{key}", url)
    settings = rehearsal.settings("api_key")
    with monkeypatch.context() as planting:
        for name in [n for n in os.environ if n.startswith("GIT_CONFIG")]:
            planting.delenv(name)
        planting.setenv("HOME", str(home))
        planting.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        report = _remote(tmp_path, settings, _arm_remote_review(rehearsal.fake))

    assert report.ok, report.render()
    assert holds_nothing(decoy)
    origin = where / "origin.git"
    main = git_out("-C", str(where / "repo"), "rev-parse", "main")
    assert git_out("--git-dir", str(origin), "rev-parse", "refs/heads/main") == main
    [launch] = rehearsal.fake.launches()
    head = git_out("-C", launch["cwd"], "rev-parse", "HEAD")
    pushed = git_out(
        "--git-dir", str(origin), "for-each-ref", "--format=%(objectname)", "refs/heads/autoforge"
    )
    assert pushed == head


# -- the REMOTE flow's own git, isolated from the operator's (#386 R2-F1) --------------
# Besides seeding its origin, the REMOTE flow runs git itself: it gives its
# checkout an `origin`, its scripted implementer stages and commits in the
# per-issue worktree, and it reads that worktree's HEAD and the AutoForge
# commit. The operator's shell may name another repository, or a part of one,
# for every git to use (`GIT_DIR`, ...), and their git configuration a hooks
# directory, a file-system monitor or a signing program. The flow's writes are
# the suite's isolated git and its reads the controller's own reader, so none
# of that reaches another checkout or runs.
REPOSITORY_SELECTIONS = (
    "GIT_DIR",
    "GIT_DIR and GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
)
# The routes the operator's own configuration comes by: REWRITE_ROUTES but
# the checkout's, which the flow makes itself.
OPERATOR_ROUTES = tuple(route for route in REWRITE_ROUTES if route.startswith(("G", "~", "X")))
# Every hook staging a path, committing or the ref update of a commit can run.
COMMIT_HOOKS = (
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "reference-transaction",
    "post-index-change",
    "pre-auto-gc",
)
GITHUB_URL = "https://github.com/owner/repo.git"


def _select(selection: str, other: Path) -> dict[str, str]:
    """The variables that name ``other``'s repository, or a part of it, as
    ``selection`` says."""
    git = str(other / ".git")
    return {
        "GIT_DIR": {"GIT_DIR": git},
        "GIT_DIR and GIT_WORK_TREE": {"GIT_DIR": git, "GIT_WORK_TREE": str(other)},
        "GIT_COMMON_DIR": {"GIT_COMMON_DIR": git},
        "GIT_INDEX_FILE": {"GIT_INDEX_FILE": str(other / ".git" / "index")},
        "GIT_OBJECT_DIRECTORY": {"GIT_OBJECT_DIRECTORY": str(other / ".git" / "objects")},
    }[selection]


def _other_checkout(path: Path) -> Path:
    """A checkout with a tracked file: a configuration, an index, refs and
    objects of its own."""
    other = git_repo(path)
    (other / "other.txt").write_text("other\n", encoding="utf-8")
    git_out("-C", str(other), "add", "other.txt")
    git_out("-C", str(other), *NO_AUTO_MAINTENANCE, *GIT_IDENT, "commit", "-qm", "other")
    return other


def _plant_hooks(route: str, planted: Path, monkeypatch) -> Path:
    """Make every git that reads ``route`` run a sentinel for each of
    :data:`COMMIT_HOOKS` (exiting 0, so that git goes on), a file-system
    monitor and a signing program for every commit; the log of what ran."""
    log = planted / "ran.log"
    hooks = planted / "hooks"
    for name in COMMIT_HOOKS:
        _sentinel(hooks / name, log, f"hook {name}", status=0)
    entries = {
        "core.hooksPath": str(hooks),
        "core.fsmonitor": str(_sentinel(planted / "fsmonitor", log, "core.fsmonitor")),
        "commit.gpgSign": "true",
        "gpg.program": str(_sentinel(planted / "gpg", log, "gpg.program")),
    }
    _plant_config(route, entries, None, planted, monkeypatch)
    return log


def _ran(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def _smoke_checkout(tmp_path: Path) -> tuple[Path, Path]:
    """The REMOTE flow's checkout as :func:`write_repository` makes it, and a
    worktree of it detached at its ``main``, as the controller adds one."""
    repo = write_repository(tmp_path / "smoke" / "repo")
    worktree = tmp_path / "smoke" / "worktree"
    git_out("-C", str(repo), "worktree", "add", "-q", "--detach", str(worktree))
    return repo, worktree


def _flows_own_git(repo: Path, worktree: Path) -> tuple[str, str, str]:
    """What the REMOTE flow runs git for itself, in its order: the checkout's
    ``origin``, the scripted implementation's commit (its answer), the
    worktree's HEAD, the AutoForge commit."""
    github_origin(repo)
    request = AgentRequest(
        phase="ANALYZE_EXECUTE",
        prompt="",
        cwd=str(worktree),
        profile=ProfileConfig(name="analyze_execute", provider="scripted"),
        idle_timeout_seconds=None,
        max_runtime_seconds=None,
    )
    answer = implement_in_worktree(request)
    return answer, worktree_head(worktree), autoforge_commit()


def _assert_landed_in_the_smoke_checkout(
    repo: Path, worktree: Path, answer: str, head: str, commit: str
) -> None:
    """The flow's own git did its work where it should: the checkout's origin
    is GitHub's, and the worktree's HEAD, the HEAD read and reported, is the
    implementation alone on ``main``, nothing left staged."""
    assert git_out("-C", str(repo), "remote", "get-url", "origin") == GITHUB_URL
    assert head == git_out("-C", str(worktree), "rev-parse", "HEAD") and head in answer
    assert git_out("-C", str(worktree), "rev-parse", "HEAD^") == git_out(
        "-C", str(repo), "rev-parse", "main"
    )
    assert git_out("-C", str(worktree), "show", "--format=", "--name-only", head) == IMPLEMENTATION
    assert git_out("-C", str(worktree), "status", "--porcelain") == ""
    assert re.fullmatch(r"[0-9a-f]{40}( with uncommitted changes)?", commit), commit


@pytest.mark.parametrize("selection", REPOSITORY_SELECTIONS)
def test_the_remote_flows_own_git_never_writes_to_a_repository_the_environment_selects(
    tmp_path, monkeypatch, selection
):
    """ADR 0009 A.2.3 G (#386 R2-F1): this process's environment names another
    checkout's repository, or a part of it, as ``selection`` says. The git the
    REMOTE flow runs itself still gives the smoke checkout its ``origin``,
    commits the scripted implementation on the worktree's detached HEAD, and
    reads that HEAD and the AutoForge commit, while nothing under the other
    checkout is written: its configuration, index, refs and objects are as
    they were. The flow's ordinary git before this fix wrote there on every
    selection."""
    other = _other_checkout(tmp_path / "other")
    repo, worktree = _smoke_checkout(tmp_path)
    before = state_tree(other)
    with monkeypatch.context() as selecting:
        for name, value in _select(selection, other).items():
            selecting.setenv(name, value)
        answer, head, commit = _flows_own_git(repo, worktree)
    assert state_tree(other) == before
    _assert_landed_in_the_smoke_checkout(repo, worktree, answer, head, commit)


@pytest.mark.parametrize("route", OPERATOR_ROUTES)
def test_the_remote_flows_own_git_runs_no_hook_monitor_or_signer_the_operator_configured(
    tmp_path, monkeypatch, route
):
    """ADR 0009 A.2.3 G (#386 R2-F1): the operator's git configuration, on
    each route it comes by, names a hooks directory holding every hook that
    staging and committing run, a file-system monitor, and a signing program
    for every commit, all sentinels. The git the REMOTE flow runs itself
    still sets the checkout's ``origin``, commits the implementation and
    reads the HEADs, and no sentinel ran. The flow's ordinary git before
    this fix ran the hooks and the monitor, and could not commit unsigned."""
    repo, worktree = _smoke_checkout(tmp_path)
    with monkeypatch.context() as planting:
        log = _plant_hooks(route, tmp_path / "planted", planting)
        answer, head, commit = _flows_own_git(repo, worktree)
    assert _ran(log) == []
    _assert_landed_in_the_smoke_checkout(repo, worktree, answer, head, commit)


def test_the_remote_flow_passes_beside_the_checkout_git_dir_names_and_runs_no_planted_hook(
    tmp_path, rehearsal, monkeypatch
):
    """ADR 0009 A.2.3 G (#386 R2-F1): the review's two reproductions on the
    whole REMOTE flow. ``GIT_DIR`` names another checkout's repository and the
    operator's ``~/.gitconfig`` the sentinels of the test above. The flow
    still passes; its checkout's ``origin`` is GitHub's and the PR head the
    controller pushed is the implementer's commit; nothing under the other
    checkout was written, and no sentinel ran."""
    other = _other_checkout(tmp_path / "other")
    before = state_tree(other)
    settings = rehearsal.settings("api_key")
    with monkeypatch.context() as planting:
        log = _plant_hooks("~/.gitconfig", tmp_path / "planted", planting)
        planting.setenv("GIT_DIR", str(other / ".git"))
        report = _remote(tmp_path, settings, _arm_remote_review(rehearsal.fake))

    assert report.ok, report.render()
    assert state_tree(other) == before
    assert _ran(log) == []
    repo = tmp_path / "smoke" / "repo"
    assert git_out("-C", str(repo), "remote", "get-url", "origin") == GITHUB_URL
    [launch] = rehearsal.fake.launches()
    head = git_out("-C", launch["cwd"], "rev-parse", "HEAD")
    assert git_out("-C", launch["cwd"], "show", "--format=", "--name-only", head) == IMPLEMENTATION
    origin = tmp_path / "smoke" / "origin.git"
    pushed = git_out(
        "--git-dir", str(origin), "for-each-ref", "--format=%(objectname)", "refs/heads/autoforge"
    )
    assert pushed == head
