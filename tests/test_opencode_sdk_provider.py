"""The ``opencode-sdk`` adapter and its bridge, end to end without a runtime (#152).

Two kinds of test:

- the real packaged bridge (``sdk_helpers/opencode_bridge.mjs``, staged as a
  launch stages it) against :mod:`tests.opencode_fake`, a fake OpenCode
  server it starts as its runtime: the startup and version read-back, the
  session and input bound by caller ids, the event observation, asks and
  decisions through the launch's broker, the final answer, the failures and
  the teardown. They need Node 24 or later on ``PATH``, and say so when
  they skip (``AUTOFORGE_REQUIRE_NODE_BRIDGE=1`` makes them fail instead);
- the adapter's driver against a fake bridge written in Python
  (:data:`FAKE_BRIDGE`), which needs no Node: the envelope from the
  controller's side, its limits, a crash, a hang.

No test calls a model, a real runtime or GitHub. The pinned runtime itself
was probed separately (ADR 0007, D4.3's amendment).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autoforge import providers
from autoforge.config import ProfileConfig
from autoforge.errors import ConfigurationError, ExecutionError
from autoforge.interaction import (
    Allow,
    Answer,
    Capabilities,
    DecisionKind,
    Deny,
    InteractionBroker,
    Lifecycle,
    QuestionResponse,
    Unavailable,
    UnavailableReason,
)
from autoforge.opencode_sdk import BridgeLimits, BridgeMode, BridgeStart, opencode_id
from autoforge.progress import ProgressKind
from autoforge.providers import AgentRequest, OpenCodeSdkProvider, staged_helper
from autoforge.sandbox_policy import AT_HOST_PATH
from autoforge.sdk_assets import helper_pin
from autoforge.sdk_policy import OpenCodeSdkRoots, interaction_settings

ROOT = Path(__file__).resolve().parent.parent
FAKE_SERVER = Path(__file__).with_name("opencode_fake.py")
ANSWER = 'Done.\n<<<CONTROL_RESULT>>>\n{"phase": "REVIEW"}\n<<<END_CONTROL_RESULT>>>'
KEY = "sk-planted-SECRET-152-0123456789abcdef"
LIVE = Capabilities(
    live_questions=True,
    live_permissions=True,
    unsupported=frozenset({DecisionKind.ALLOW_SESSION, DecisionKind.MODIFIED_INPUT}),
)


def _node() -> str:
    found = shutil.which("node")
    if not found:
        return ""
    try:
        out = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.fullmatch(r"v(\d+)\.\d+\.\d+\s*", out.stdout)
    return found if match and int(match.group(1)) >= 24 else ""


NODE = _node()
needs_node = pytest.mark.skipif(
    not NODE and os.environ.get("AUTOFORGE_REQUIRE_NODE_BRIDGE") != "1",
    reason="the OpenCode bridge needs Node 24 or later on PATH (ADR 0007 D4.4)",
)


class Scripted:
    """A resolver that answers every request with ``decision`` and records it."""

    def __init__(self, decision) -> None:
        self.decision = decision
        self.requests: list = []

    def resolve(self, request, deadline):
        self.requests.append(request)
        return self.decision(request) if callable(self.decision) else self.decision


class Launch:
    """One launch's fixture: the fake runtime, the roots, the request, the log."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str = "answer"):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.server = self.bin / "opencode"
        self.server.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_SERVER}" "$@"\n', encoding="utf-8"
        )
        self.server.chmod(0o755)
        self.workspace = tmp_path / "worktree"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("hello\n", encoding="utf-8")
        self.state_dir = tmp_path / "sandbox"
        self.log = tmp_path / "fake.jsonl"
        self.secret = tmp_path / "secret"
        monkeypatch.setenv("PATH", f"{self.bin}:{os.environ.get('PATH', '')}")
        monkeypatch.setenv("OPENAI_API_KEY", KEY)
        self.set(SCENARIO=scenario, LOG=str(self.log), SECRET_FILE=str(self.secret))
        self.allow = ["PATH", "LANG"]

    def set(self, **values: str) -> None:
        for key, value in values.items():
            self.monkeypatch.setenv(f"FAKE_OPENCODE_{key}", value)

    def profile(self, **options: str) -> ProfileConfig:
        opts = {"server_command": "opencode", "credential_env": "OPENAI_API_KEY", **options}
        return ProfileConfig(
            name="review_round_1",
            provider="opencode-sdk",
            model="fake/m1",
            effort="",
            command=NODE or "node",
            options=opts,
        )

    def roots(self, provider: OpenCodeSdkProvider, profile: ProfileConfig) -> None:
        for root in provider.state_layout(profile):
            (self.state_dir / root.relpath).mkdir(parents=True, exist_ok=True)

    def request(
        self,
        provider: OpenCodeSdkProvider,
        profile: ProfileConfig,
        *,
        broker: InteractionBroker | None = None,
        idle: int | None = 60,
        ceiling: int | None = 120,
        progress=None,
    ) -> AgentRequest:
        self.roots(provider, profile)
        allow = tuple(
            [*self.allow, *(name for name in os.environ if name.startswith("FAKE_OPENCODE_"))]
        )
        return AgentRequest(
            phase="review",
            prompt="Review the change.",
            cwd=str(self.workspace),
            profile=profile,
            idle_timeout_seconds=idle,
            max_runtime_seconds=ceiling,
            env_allowlist=allow,
            env={},
            state_dir=str(self.state_dir),
            interaction=broker
            if broker is not None
            else InteractionBroker(
                interaction_settings(profile.options, "p"), Capabilities(), Scripted(None)
            ),
            progress=progress,
        )

    def entries(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def _limits(**values: int) -> BridgeLimits:
    base = {"settle_seconds": 2, "abort_seconds": 2, "stop_seconds": 2, "startup_seconds": 20}
    base.update(values)
    return BridgeLimits(max_record_bytes=providers.OPENCODE_SDK_MAX_RECORD_BYTES, **base)


def _provider(**limits: int) -> OpenCodeSdkProvider:
    return OpenCodeSdkProvider(limits=_limits(**limits), wind_down_seconds=8, stop_seconds=8)


def _run(launch: Launch, *, provider=None, profile=None, **kwargs):
    provider = provider or _provider()
    profile = profile or launch.profile()
    return provider.execute(launch.request(provider, profile, **kwargs))


# -- the real bridge against the fake runtime ------------------------------------------
@needs_node
def test_a_turn_reaches_the_parser_as_its_final_answer_alone(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch)
    events: list = []
    result = _run(launch, progress=events.append)
    assert result.provider_failure is None, result.provider_failure
    assert result.exit_code == 0 and not result.timed_out
    assert result.stdout == ANSWER
    assert result.interaction is not None and result.interaction.lifecycle is Lifecycle.COMPLETED
    summary = result.provider_summary
    assert summary["runtime_version"] == "1.18.34"
    assert summary["admitted"] is True and summary["input_sent"] is True
    assert summary["server_stopped"] == "sigterm"
    assert summary["bridge_skipped_unknown"] == 0
    assert any(event.kind is ProgressKind.STARTED for event in events)
    entries = launch.entries()
    start = entries[0]
    # The bridge starts the runtime exactly so, with the password set and no
    # username, no TTY, in the worktree, with the policy's environment.
    assert start["argv"] == ["serve", "--hostname=127.0.0.1", "--port=0"]
    assert start["password_set"] and start["password_length"] >= 43
    assert not start["username_set"] and not start["stdin_tty"]
    assert start["credential_set"] and start["cwd"] == str(launch.workspace)
    assert {"OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR", "XDG_DATA_HOME"} <= set(
        start["env_names"]
    )
    # The bridge never ran in the server's process group of its own: one group.
    requests = [e for e in entries if "request" in e]
    assert requests and all(e["authorized"] for e in requests)
    assert {e["path"] for e in requests} <= {
        "/global/health",
        "/event",
        f"/api/session/{summary['session_id']}",
        "/api/session",
        f"/session/{summary['session_id']}/prompt_async",
        f"/session/{summary['session_id']}/message/{summary['input_id']}",
        f"/session/{summary['session_id']}/message",
        "/session/status",
        f"/session/{summary['session_id']}/abort",
    }
    created = next(e for e in entries if "created" in e)
    assert created["agent"] == "autoforge"
    assert created["model"] == {"providerID": "fake", "id": "m1"}
    assert created["location"] == {"directory": str(launch.workspace)}
    prompt = next(e for e in entries if "prompt" in e)
    assert prompt["message_id"] == summary["input_id"]
    assert prompt["agent"] == "autoforge" and prompt["model"] == {
        "providerID": "fake",
        "modelID": "m1",
    }
    # Caller ids in the runtime's own format, so they sort as its own do.
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", summary["session_id"])
    assert re.fullmatch(r"msg_[0-9a-f]{12}[0-9A-Za-z]{14}", summary["input_id"])


@needs_node
def test_the_server_password_reaches_nothing_the_controller_keeps(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch)
    result = _run(launch)
    password = launch.secret.read_text(encoding="utf-8")
    assert len(password) >= 43  # 32 random bytes, base64url (ADR 0007 D3.4)
    kept = json.dumps(
        [result.command, result.stdout, result.stderr, result.provider_summary], default=str
    )
    assert password not in kept
    assert password not in launch.log.read_text(encoding="utf-8")


@needs_node
def test_the_variant_is_the_profiles_effort(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch)
    profile = launch.profile()
    profile.effort = "high"
    result = _run(launch, profile=profile)
    assert result.provider_failure is None
    prompt = next(e for e in launch.entries() if "prompt" in e)
    created = next(e for e in launch.entries() if "created" in e)
    assert prompt["variant"] == "high" and created["model"]["variant"] == "high"


@needs_node
def test_text_parts_join_and_a_synthetic_part_is_left_out(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "two-parts")
    result = _run(launch)
    assert result.stdout == f"first part\n{ANSWER}"


@needs_node
def test_a_tool_is_progress_and_a_loop_action(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "tool")
    events: list = []
    result = _run(launch, progress=events.append)
    assert result.provider_failure is None
    started = [e for e in events if e.kind is ProgressKind.TOOL_STARTED]
    assert [(e.tool, e.detail) for e in started] == [("read", "README.md")]
    assert any(e.kind is ProgressKind.TOOL_FINISHED and e.tool == "read" for e in events)
    assert result.provider_summary["tools"] == 1
    # The completed call reached the loop detector as one action (#194).
    assert result.provider_summary["loop_actions"] == 1


@needs_node
def test_a_question_is_answered_live_through_the_broker(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "question")
    profile = launch.profile(interaction_policy="prompt", max_wait_seconds="30")
    resolver = Scripted(Answer((QuestionResponse(choices=("blue",)),)))
    broker = InteractionBroker(interaction_settings(profile.options, "p"), LIVE, resolver)
    result = _run(launch, profile=profile, broker=broker)
    assert result.provider_failure is None, result.provider_failure
    assert result.stdout == f'answers: [["blue"]]\n{ANSWER}'
    (request,) = resolver.requests
    assert request.questions[0].text == "Which colour?"
    assert [option.label for option in request.questions[0].options] == ["red", "blue"]
    # The runtime's defaults for an absent field: one pick, own words allowed.
    assert not request.questions[0].multiple and request.questions[0].custom
    reply = next(e for e in launch.entries() if "reply" in e)
    assert (reply["kind"], reply["action"], reply["body"]["answers"]) == (
        "question",
        "reply",
        [["blue"]],
    )
    assert result.interaction.lifecycle is Lifecycle.COMPLETED


@needs_node
def test_several_questions_carry_multiple_picks_and_a_custom_answer(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "questions")
    profile = launch.profile(interaction_policy="prompt", max_wait_seconds="30")
    resolver = Scripted(
        Answer(
            (
                QuestionResponse(choices=("red", "blue")),
                QuestionResponse(custom_text="my own words"),
            )
        )
    )
    broker = InteractionBroker(interaction_settings(profile.options, "p"), LIVE, resolver)
    result = _run(launch, profile=profile, broker=broker)
    assert result.provider_failure is None, result.provider_failure
    reply = next(e for e in launch.entries() if "reply" in e)
    assert reply["body"]["answers"] == [["red", "blue"], ["my own words"]]
    second = resolver.requests[0].questions[1]
    assert second.custom and not second.multiple


@needs_node
def test_an_ask_under_fail_is_rejected_and_fails_the_invocation(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "question")
    result = _run(launch)
    assert "interaction_policy is fail" in result.provider_failure
    assert result.interaction.lifecycle is Lifecycle.FAILED
    assert result.stdout == ""
    reply = next(e for e in launch.entries() if "reply" in e)
    assert (reply["kind"], reply["action"]) == ("question", "reject")
    assert result.provider_summary["server_stopped"] in ("sigterm", "sigkill")


@needs_node
def test_a_wait_with_no_terminal_fails_without_a_pause(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "question")
    profile = launch.profile(interaction_policy="prompt", max_wait_seconds="30")
    resolver = Scripted(Unavailable(UnavailableReason.NO_TERMINAL))
    broker = InteractionBroker(interaction_settings(profile.options, "p"), LIVE, resolver)
    result = _run(launch, profile=profile, broker=broker)
    assert "no pause is available" in result.provider_failure
    assert result.interaction.lifecycle is Lifecycle.FAILED


@needs_node
def test_a_permission_is_approved_once(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "permission")
    profile = launch.profile(interaction_policy="prompt", max_wait_seconds="30")
    resolver = Scripted(Allow())
    broker = InteractionBroker(interaction_settings(profile.options, "p"), LIVE, resolver)
    result = _run(launch, profile=profile, broker=broker)
    assert result.provider_failure is None, result.provider_failure
    (request,) = resolver.requests
    assert request.tool_name == "bash" and "git status" in request.summary
    reply = next(e for e in launch.entries() if "reply" in e)
    assert reply["body"] == {"reply": "once"}


@needs_node
def test_a_denial_carries_its_guidance_as_feedback(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "permission")
    profile = launch.profile(interaction_policy="prompt", max_wait_seconds="30")
    resolver = Scripted(Deny(guidance="use git diff instead"))
    broker = InteractionBroker(interaction_settings(profile.options, "p"), LIVE, resolver)
    result = _run(launch, profile=profile, broker=broker)
    assert result.provider_failure is None, result.provider_failure
    reply = next(e for e in launch.entries() if "reply" in e)
    assert reply["body"] == {"reply": "reject", "message": "use git diff instead"}


@needs_node
def test_a_reject_reports_the_asks_it_settled_with_it(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "two-permissions")
    profile = launch.profile(interaction_policy="prompt", max_wait_seconds="30")
    resolver = Scripted(Deny())
    broker = InteractionBroker(interaction_settings(profile.options, "p"), LIVE, resolver)
    result = _run(launch, profile=profile, broker=broker)
    replies = [e for e in launch.entries() if "reply" in e]
    # One reject went out; the sibling it settled was never answered.
    assert len(replies) == 1 and replies[0]["body"]["reply"] == "reject"
    assert result.provider_summary["asks_settled_by_reject"] == 1
    assert result.provider_summary["bridge_sibling_settled"] == 1


@needs_node
def test_an_opencode_permission_fails_under_every_policy_without_its_gate(tmp_path, monkeypatch):
    """The adapter declares no capability (ADR 0007 D3.4, D5.3): under
    ``prompt`` the broker still has nothing to offer, and the ask fails."""
    launch = Launch(tmp_path, monkeypatch, "permission")
    profile = launch.profile(interaction_policy="prompt", max_wait_seconds="30")
    provider = _provider()
    broker = InteractionBroker(
        interaction_settings(profile.options, "p"),
        provider.interaction_capabilities(profile),
        Scripted(Allow()),
    )
    result = _run(launch, provider=provider, profile=profile, broker=broker)
    assert "cannot carry a live permission decision" in result.provider_failure
    reply = next(e for e in launch.entries() if "reply" in e)
    assert reply["body"]["reply"] == "reject"


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("foreign-ask", "came from a session outside the bound session"),
        ("v2-ask", "an ask of a family this bridge does not answer"),
        ("malformed-ask", "carries a field this bridge cannot render"),
        ("unknown-finish", "ended for an unknown reason (content-filter)"),
        ("later-input", "another input was admitted to the session after this one"),
        ("tool-calls-only", "settled after a tool call, with no final answer"),
        ("disconnect", "the event stream ended before the turn did"),
        ("unknown-status", "an unknown status (hibernating)"),
    ],
)
@needs_node
def test_what_the_bridge_cannot_vouch_for_fails_closed(tmp_path, monkeypatch, scenario, reason):
    launch = Launch(tmp_path, monkeypatch, scenario)
    result = _run(launch)
    assert result.provider_failure is not None and reason in result.provider_failure
    assert result.stdout == "" and not result.timed_out
    assert result.provider_summary["server_stopped"] in ("sigterm", "sigkill")


@needs_node
def test_a_repeated_ask_fails_closed(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "repeat-ask")
    profile = launch.profile(interaction_policy="prompt", max_wait_seconds="30")
    resolver = Scripted(Unavailable(UnavailableReason.EXPIRED))
    broker = InteractionBroker(interaction_settings(profile.options, "p"), LIVE, resolver)
    result = _run(launch, profile=profile, broker=broker)
    assert result.provider_failure is not None
    assert result.stdout == ""


@needs_node
def test_a_model_error_is_a_failure_and_a_retryable_503_is_transient(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "model-error")
    result = _run(launch)
    assert "the answer ended with an error (APIError)" in result.provider_failure
    assert result.transient_failure == "opencode-sdk: model API error 503"


@needs_node
def test_unknown_observations_are_counted_not_fatal(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "unknown-events")
    result = _run(launch)
    assert result.provider_failure is None
    assert result.provider_summary["bridge_skipped_unknown"] == 2


@needs_node
def test_an_event_past_its_bound_fails_closed(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "huge-event")
    result = _run(launch, provider=_provider(max_event_bytes=64 * 1024))
    assert "larger than its bound" in result.provider_failure


@needs_node
def test_a_final_answer_past_the_record_bound_is_refused_whole(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch)
    launch.set(ANSWER="x" * (80 * 1024))
    provider = OpenCodeSdkProvider(
        limits=BridgeLimits(
            max_record_bytes=64 * 1024, settle_seconds=2, abort_seconds=2, stop_seconds=2
        ),
        wind_down_seconds=8,
        stop_seconds=8,
    )
    result = _run(launch, provider=provider)
    assert "larger than the record bound" in result.provider_failure
    assert result.stdout == ""


@needs_node
def test_a_runtime_that_is_not_the_pinned_one_fails_before_any_session(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch)
    launch.set(VERSION="1.18.35")
    result = _run(launch)
    assert "runtime pair mismatch" in result.provider_failure
    assert "1.18.35" in result.provider_failure
    assert not any("created" in e or "prompt" in e for e in launch.entries())


@needs_node
def test_a_session_id_that_already_exists_is_refused(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch)
    provider = _provider()
    profile = launch.profile()
    start = provider.start_for(launch.request(provider, profile), str(launch.server))
    launch.set(EXISTING=start.session_id)
    result = provider.run_bridge(launch.request(provider, profile), start)
    assert "a session with the caller's id already exists" in result.provider_failure
    assert not any("created" in e for e in launch.entries())


@needs_node
def test_an_input_id_that_holds_another_input_is_a_conflict(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch)
    provider = _provider()
    profile = launch.profile()
    start = provider.start_for(launch.request(provider, profile), str(launch.server))
    launch.set(CONFLICT=start.input_id)
    result = provider.run_bridge(launch.request(provider, profile), start)
    assert "holds a different input (a conflict)" in result.provider_failure
    assert not any("prompt" in e for e in launch.entries())


@needs_node
def test_continue_and_recover_read_the_same_data_root(tmp_path, monkeypatch):
    """The session persists in the data root (D2.7): a later launch on it
    continues the session with a new input, and recovers an input's answer
    without admitting anything (D9.9); an earlier input's answer is never
    taken once a later input exists."""
    launch = Launch(tmp_path, monkeypatch)
    provider = _provider()
    profile = launch.profile()
    req = launch.request(provider, profile)
    first = provider.start_for(req, str(launch.server))
    assert provider.run_bridge(req, first).stdout == ANSWER
    second = BridgeStart(
        **{
            **first.__dict__,
            "mode": BridgeMode.CONTINUE,
            "input_id": opencode_id("msg", 2_000_000_000_000),
            "prompt": "And again.",
        }
    )
    continued = provider.run_bridge(launch.request(provider, profile), second)
    assert continued.provider_failure is None and continued.stdout == ANSWER
    recovered = provider.run_bridge(
        launch.request(provider, profile),
        BridgeStart(**{**second.__dict__, "mode": BridgeMode.RECOVER}),
    )
    assert recovered.provider_failure is None and recovered.stdout == ANSWER
    assert recovered.provider_summary["admitted"] is False
    stale = provider.run_bridge(
        launch.request(provider, profile),
        BridgeStart(**{**first.__dict__, "mode": BridgeMode.RECOVER}),
    )
    assert "another input was admitted" in stale.provider_failure
    resent = provider.run_bridge(
        launch.request(provider, profile),
        BridgeStart(**{**second.__dict__, "mode": BridgeMode.CONTINUE}),
    )
    # The identical input is read back, never sent again.
    assert resent.provider_failure is None and resent.provider_summary["input_sent"] is False
    prompts = [e for e in launch.entries() if "prompt" in e]
    assert [e["message_id"] for e in prompts] == [first.input_id, second.input_id]


@needs_node
def test_a_turn_still_running_at_the_idle_limit_is_aborted_and_stopped(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "silent")
    result = _run(launch, idle=3)
    assert result.timed_out and result.timeout_limit == "idle"
    assert result.provider_failure is None and result.stdout == ""
    assert any("abort" in e for e in launch.entries())
    assert not result.group_survived_kill and not result.capture_abandoned


@needs_node
def test_a_turn_still_running_at_the_ceiling_is_aborted_and_stopped(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch, "slow")
    result = _run(launch, ceiling=4)
    assert result.timed_out and result.timeout_limit == "max_runtime"
    assert any("abort" in e for e in launch.entries())
    assert not result.may_have_survivor


@pytest.mark.parametrize(
    ("start", "reason"),
    [("never", "printed no readiness line"), ("exit", "exited before it was ready")],
)
@needs_node
def test_a_server_that_never_gets_ready_fails_the_startup(tmp_path, monkeypatch, start, reason):
    launch = Launch(tmp_path, monkeypatch)
    launch.set(START=start)
    result = _run(launch, provider=_provider(startup_seconds=2))
    assert reason in result.provider_failure
    assert not result.may_have_survivor


# -- refusals before anything starts ---------------------------------------------------
def test_a_launch_without_the_credential_is_refused_by_name(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY")
    provider = _provider()
    profile = launch.profile()
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY") as caught:
        provider.execute(launch.request(provider, profile))
    assert KEY not in str(caught.value)
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        provider.credential_preflight(profile, tuple(launch.allow))
    assert launch.entries() == []


def test_a_runtime_not_on_the_launch_path_is_refused(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch)
    provider = _provider()
    profile = launch.profile(server_command="opencode-not-installed")
    with pytest.raises(ConfigurationError, match="not an executable on the launch's PATH"):
        provider.credential_preflight(profile, tuple(launch.allow))


def test_a_launch_without_its_state_roots_is_refused(tmp_path, monkeypatch):
    launch = Launch(tmp_path, monkeypatch)
    provider = _provider()
    profile = launch.profile()
    req = launch.request(provider, profile)
    shutil.rmtree(launch.state_dir)
    with pytest.raises(ExecutionError, match="state roots were not prepared"):
        provider.execute(req)
    req.state_dir = None
    with pytest.raises(ExecutionError, match="no state directory"):
        provider.execute(req)


@pytest.mark.parametrize("where", ["empty-config", "config/opencode"])
def test_a_configuration_directory_holding_anything_else_refuses_the_launch(
    tmp_path, monkeypatch, where
):
    """An agent of an earlier launch may have planted a plugin, an agent or a
    command where the runtime loads them (docs/sdk-policy.md §6): the launch
    is refused, naming it, and nothing is deleted."""
    launch = Launch(tmp_path, monkeypatch)
    provider = _provider()
    profile = launch.profile()
    req = launch.request(provider, profile)
    planted = launch.state_dir / "sdk" / "opencode-sdk" / where / "plugin"
    planted.mkdir(parents=True)
    (planted / "x.js").write_text("export const X = async () => ({})\n", encoding="utf-8")
    with pytest.raises(ExecutionError, match=r"holds what AutoForge did not put there \(plugin\)"):
        provider.execute(req)
    assert (planted / "x.js").exists()
    assert launch.entries() == []


def test_the_configuration_directories_are_read_only_with_the_runtimes_ignore_file(tmp_path):
    roots = OpenCodeSdkRoots.under(tmp_path / "sandbox")
    for path in (*roots.writable(), roots.empty_config):
        path.mkdir(parents=True)
    OpenCodeSdkProvider.prepare_config(roots)
    OpenCodeSdkProvider.prepare_config(roots)  # idempotent
    for directory in (roots.config / "opencode", roots.empty_config):
        assert sorted(os.listdir(directory)) == [".gitignore"]
        assert (directory / ".gitignore").read_text(encoding="utf-8") == (
            providers.OPENCODE_SDK_CONFIG_IGNORE
        )
        assert directory.stat().st_mode & 0o777 == 0o555
        directory.chmod(0o755)


def test_a_bundle_that_is_not_the_manifests_is_never_staged(tmp_path, monkeypatch):
    fake = tmp_path / "sdk_helpers"
    shutil.copytree(providers.helpers_dir(), fake)
    pin = helper_pin("opencode-sdk")
    (fake / pin.file).write_bytes(b"// tampered\n")
    monkeypatch.setattr(providers, "helpers_dir", lambda: fake)
    with pytest.raises(ConfigurationError, match="does not match the manifest's sha256"):
        with staged_helper(pin):
            pass


def test_the_helper_is_staged_read_only_and_removed_after_the_launch():
    pin = helper_pin("opencode-sdk")
    with staged_helper(pin) as path:
        staged = Path(path)
        assert staged.read_bytes() == (providers.helpers_dir() / pin.file).read_bytes()
        assert staged.stat().st_mode & 0o777 == 0o400
        assert staged.parent.stat().st_mode & 0o777 == 0o500
        assert not str(staged).startswith(str(ROOT))
    assert not staged.parent.exists()


def test_the_state_layout_is_the_policys_roots():
    """docs/sdk-policy.md §6, as the engine makes and projects them."""
    layout = {root.relpath: root for root in OpenCodeSdkProvider().state_layout(_bare())}
    base = "sdk/opencode-sdk"
    assert set(layout) == {
        f"{base}/{name}" for name in ("home", "config", "data", "cache", "state", "empty-config")
    }
    assert layout[f"{base}/data"].inside == "XDG_DATA_HOME" and layout[f"{base}/data"].writable
    assert layout[f"{base}/config"].inside == "XDG_CONFIG_HOME"
    assert layout[f"{base}/state"].inside == "XDG_STATE_HOME"
    assert layout[f"{base}/home"].inside == "" and layout[f"{base}/cache"].inside == ""
    empty = layout[f"{base}/empty-config"]
    assert empty.inside == AT_HOST_PATH and not empty.writable


def test_the_sandbox_declaration_names_both_executables_and_the_staged_helper(tmp_path):
    server = tmp_path / "opt" / "opencode" / "bin" / "opencode"
    server.parent.mkdir(parents=True)
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o755)
    node = tmp_path / "node" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\n", encoding="utf-8")
    node.chmod(0o755)
    profile = _bare()
    env = {"PATH": f"{server.parent}:{node.parent}", "OPENAI_API_KEY": KEY}
    staged = tmp_path / "stage" / "opencode_bridge.mjs"
    needs = OpenCodeSdkProvider().sandbox_requirements(
        profile, [str(node), str(staged)], env, str(tmp_path / "home")
    )
    assert needs.executable == str(node)
    assert any(str(server).startswith(root) for root in needs.install_roots)
    assert str(staged.parent) in needs.config
    assert [item.label for item in needs.auth] == ["OPENAI_API_KEY"]


def _bare() -> ProfileConfig:
    return ProfileConfig(
        name="review_round_1",
        provider="opencode-sdk",
        model="openai/gpt-5.6-luna",
        effort="",
        command="node",
        options={"server_command": "opencode", "credential_env": "OPENAI_API_KEY"},
    )


# -- the driver against a fake bridge (no Node) -----------------------------------------
FAKE_BRIDGE = r"""
import json, os, sys, time
mode = os.environ["FAKE_BRIDGE_MODE"]
log = open(os.environ["FAKE_BRIDGE_LOG"], "a")
def out(record):
    sys.stdout.write(json.dumps(record) + "\n"); sys.stdout.flush()
def read():
    line = sys.stdin.readline()
    if line:
        log.write(line); log.flush()
    return json.loads(line) if line else None
if mode == "crash-at-once":
    sys.exit(7)
env = os.environ.get
out({"type": "hello", "envelope": env("FAKE_BRIDGE_ENVELOPE", "autoforge-opencode-bridge"),
     "protocol": 1, "sdk_version": env("FAKE_BRIDGE_SDK", "1.18.34"),
     "node_version": env("FAKE_BRIDGE_NODE", "24.11.1")})
start = read()
if start is None:
    out({"type": "end", "outcome": "interrupted", "server": {"stopped": "never_started"}})
    sys.exit(0)
out({"type": "server", "runtime_version": "1.18.34"})
model = start["model"]
out({"type": "session", "session_id": start["session_id"], "mode": start["mode"],
     "directory": start["cwd"], "agent": start["agent"], "provider_id": model["provider_id"],
     "model_id": model["model_id"], "variant": model["variant"]})
out({"type": "admitted", "input_id": start["input_id"], "sent": True})
if mode == "crash":
    sys.exit(3)
if mode == "hang":
    while True:
        record = read()
        if record is None or record.get("type") == "interrupt":
            break
    out({"type": "end", "outcome": "interrupted", "server": {"stopped": "sigterm"}})
    sys.exit(0)
if mode == "garbage":
    sys.stdout.write("not json\n"); sys.stdout.flush()
    read(); read()
    sys.exit(0)
if mode == "ask":
    out({"type": "ask", "request_id": "1", "kind": "question", "provider_request_id": "que_1",
         "session_id": start["session_id"], "tool": None,
         "questions": [{"question": "Which?", "header": "H", "multiple": None, "custom": None,
                        "options": [{"label": "a", "description": ""}]}]})
    read()
out({"type": "event", "kind": "status", "status": "busy"})
out({"type": "result", "session_id": start["session_id"], "input_id": start["input_id"],
     "message_id": "msg_x", "finish": "stop", "text": env("FAKE_BRIDGE_TEXT", "the answer")})
out({"type": "end", "outcome": "result", "server": {"stopped": "sigterm", "exit_code": None,
     "signal": "SIGTERM"}, "events": 3, "skipped_unknown": 0, "foreign_events": 0, "asks": 0,
     "sibling_settled": 0})
"""


class FakeBridge:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "ok"):
        self.launch = Launch(tmp_path, monkeypatch)
        self.script = tmp_path / "fake_bridge.py"
        self.script.write_text(FAKE_BRIDGE, encoding="utf-8")
        self.records = tmp_path / "bridge-in.jsonl"
        monkeypatch.setenv("FAKE_BRIDGE_MODE", mode)
        monkeypatch.setenv("FAKE_BRIDGE_LOG", str(self.records))
        self.launch.allow.extend(["FAKE_BRIDGE_MODE", "FAKE_BRIDGE_LOG"])
        script = str(self.script)

        def staged(pin):
            from contextlib import nullcontext

            return nullcontext(script)

        monkeypatch.setattr(providers, "staged_helper", staged)

    def run(self, provider=None, **kwargs):
        profile = self.launch.profile()
        profile.command = sys.executable
        return _run(self.launch, provider=provider, profile=profile, **kwargs)

    def sent(self) -> list[dict]:
        if not self.records.exists():
            return []
        return [json.loads(line) for line in self.records.read_text(encoding="utf-8").splitlines()]


def test_the_driver_sends_start_after_hello_and_reads_the_answer(tmp_path, monkeypatch):
    bridge = FakeBridge(tmp_path, monkeypatch)
    result = bridge.run()
    assert result.provider_failure is None and result.stdout == "the answer"
    (start,) = bridge.sent()
    assert start["type"] == "start" and start["mode"] == "new"
    assert start["prompt"] == "Review the change."
    assert start["server_command"] == str(bridge.launch.server)
    assert start["expect"] == {"runtime_version": "1.18.34"}
    assert start["policy"]["provider"] == "opencode-sdk" and start["policy"]["agent"] == "autoforge"
    assert start["limits"]["max_record_bytes"] == providers.OPENCODE_SDK_MAX_RECORD_BYTES
    assert start["model"] == {"provider_id": "fake", "model_id": "m1", "variant": None}


@pytest.mark.parametrize(
    ("variable", "value", "reason"),
    [
        ("FAKE_BRIDGE_ENVELOPE", "autoforge-other", "not autoforge-opencode-bridge 1"),
        ("FAKE_BRIDGE_SDK", "1.18.35", "SDK pair mismatch"),
        ("FAKE_BRIDGE_NODE", "22.1.0", "below the supported 24"),
    ],
)
def test_a_helper_that_is_not_the_pinned_pair_is_never_started(
    tmp_path, monkeypatch, variable, value, reason
):
    bridge = FakeBridge(tmp_path, monkeypatch)
    monkeypatch.setenv(variable, value)
    bridge.launch.allow.append(variable)
    result = bridge.run()
    assert reason in result.provider_failure
    assert bridge.sent() == []  # no start record was sent


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("crash", "the bridge exited without its last record"),
        ("crash-at-once", "the bridge exited without its last record (hello)"),
        ("garbage", "a record is not JSON"),
    ],
)
def test_a_bridge_that_breaks_the_envelope_fails_the_invocation(
    tmp_path, monkeypatch, mode, reason
):
    result = FakeBridge(tmp_path, monkeypatch, mode).run()
    assert reason in result.provider_failure
    assert result.stdout == ""


def test_a_silent_bridge_is_interrupted_at_the_idle_limit(tmp_path, monkeypatch):
    bridge = FakeBridge(tmp_path, monkeypatch, "hang")
    result = bridge.run(idle=2)
    assert result.timed_out and result.timeout_limit == "idle"
    assert [record["type"] for record in bridge.sent()] == ["start", "interrupt"]


def test_an_ask_without_a_broker_is_rejected_and_fails(tmp_path, monkeypatch):
    bridge = FakeBridge(tmp_path, monkeypatch, "ask")
    provider = _provider()
    profile = bridge.launch.profile()
    profile.command = sys.executable
    req = bridge.launch.request(provider, profile)
    req.interaction = None
    result = provider.execute(req)
    assert "the launch has no broker" in result.provider_failure
    decision = bridge.sent()[1]
    assert decision == {"type": "decision", "request_id": "1", "directive": "fail"}
