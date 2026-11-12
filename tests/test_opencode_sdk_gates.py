"""ADR 0007 §3's OpenCode gates on the pinned pair, under the outer sandbox (#152). Opt-in.

What a probe is (ADR 0007 §3): a scripted launch through the real adapter
and helper, under ``sandbox.mode: required``, with the policy's dedicated
configuration and data roots, against a loopback stand-in model endpoint
(:mod:`tests.opencode_standin`), so no paid call and no credential of the
operator's is needed. The only change to the policy is the one ``provider``
entry of the inline document that names the stand-in.

Run on a host that can enforce the sandbox, with the pinned runtime installed
(``npm install opencode-ai@1.18.34`` puts its binary at
``node_modules/opencode-ai/bin/opencode.exe``) and Node 24 or later on
``PATH``::

    AUTOFORGE_OPENCODE_SDK_GATE_RUNTIME=/path/to/opencode.exe \\
        uv run pytest -rs tests/test_opencode_sdk_gates.py

``AUTOFORGE_OPENCODE_SDK_GATE_READ_ONLY`` lists, colon-separated, more
read-only paths a launch needs on this host, as ``sandbox.read_only_paths``
would (a Homebrew Node loads its libraries from its prefix). The results of
the run #152 made are recorded in ADR 0007, D4.3's amendment; nothing here
declares a capability (D5.3).
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from autoforge import sandbox_bwrap
from autoforge.config import ProfileConfig, default_config
from autoforge.interaction import (
    Answer,
    Capabilities,
    InteractionBroker,
    QuestionResponse,
)
from autoforge.opencode_sdk import BridgeLimits, BridgeMode, BridgeStart
from autoforge.providers import AgentRequest, OpenCodeSdkProvider
from autoforge.sandbox_policy import LaunchClass, LaunchFacts, SandboxPlan
from autoforge.sdk_policy import OpenCodeSdkPolicy, interaction_settings

from .conftest import GIT_IDENT, real_sandbox_backend
from .opencode_standin import ANSWER, StandIn
from .test_opencode_sdk_provider import NODE, Scripted

RUNTIME = os.environ.get("AUTOFORGE_OPENCODE_SDK_GATE_RUNTIME", "")
EXTRA_READ_ONLY = tuple(
    p for p in os.environ.get("AUTOFORGE_OPENCODE_SDK_GATE_READ_ONLY", "").split(":") if p
)

pytestmark = pytest.mark.skipif(
    not RUNTIME or not NODE,
    reason=(
        "opt-in: set AUTOFORGE_OPENCODE_SDK_GATE_RUNTIME to the opencode-ai 1.18.34 binary, "
        "with Node 24 or later on PATH"
    ),
)


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> str:
    return real_sandbox_backend(tmp_path_factory)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


class Gate:
    """One issue's layout as the engine makes it, and launches through the adapter."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, standin: StandIn):
        base = Path(os.path.realpath(tmp_path))
        self.repo = base / "repo"
        self.repo.mkdir()
        _git("init", "-q", "-b", "main", str(self.repo))
        (self.repo / "README.md").write_text("gate\n", encoding="utf-8")
        _git("-C", str(self.repo), "add", "README.md")
        _git("-C", str(self.repo), *GIT_IDENT, "commit", "-q", "-m", "initial")
        self.common = self.repo / ".git"
        self.worktree = self.common / "autoforge" / "worktrees" / "7"
        _git("-C", str(self.repo), "worktree", "add", "-q", "--detach", str(self.worktree), "HEAD")
        self.sandbox = self.common / "autoforge" / "sandbox" / "7"
        for name in ("objects", "overlay-work", "cache"):
            (self.sandbox / name).mkdir(parents=True)
        self.home = base / "home"
        self.home.mkdir()
        self.config = base / "autoforge-child-1"
        self.config.mkdir()
        (self.config / "gitconfig").write_text("", encoding="utf-8")
        self.provider = OpenCodeSdkProvider(
            limits=BridgeLimits(settle_seconds=10, abort_seconds=10, stop_seconds=5),
            wind_down_seconds=30,
        )
        self.profile = ProfileConfig(
            name="review_round_1",
            provider="opencode-sdk",
            model="standin/m1",
            effort="",
            command=NODE,
            options={
                "server_command": RUNTIME,
                "credential_env": "STANDIN_API_KEY",
                "interaction_policy": "prompt",
                "max_wait_seconds": "30",
            },
        )
        self.roots = []
        for root in self.provider.state_layout(self.profile):
            path = self.sandbox / root.relpath
            path.mkdir(parents=True, exist_ok=True)
            self.roots.append((str(path), root))
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("STANDIN_API_KEY", "autoforge-synthetic-standin-152")
        document = OpenCodeSdkPolicy.config_document

        def with_standin(policy: OpenCodeSdkPolicy) -> dict[str, object]:
            doc = document(policy)
            doc["provider"] = {
                "standin": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Stand-in",
                    "options": {"baseURL": standin.base_url, "apiKey": "{env:STANDIN_API_KEY}"},
                    "models": {"m1": {"name": "m1", "tool_call": True}},
                }
            }
            return doc

        monkeypatch.setattr(OpenCodeSdkPolicy, "config_document", with_standin)

    def facts(self) -> LaunchFacts:
        return LaunchFacts(
            launch_class=LaunchClass.REMOTE_AGENT,
            workspace=str(self.worktree),
            home=str(self.home),
            controller_env=dict(os.environ),
            git_entry=str(self.worktree / ".git"),
            git_dir=str(self.common / "worktrees" / "7"),
            common_dir=str(self.common),
            private_objects=str(self.sandbox / "objects"),
            overlay_work=str(self.sandbox / "overlay-work"),
            cache=str(self.sandbox / "cache"),
            controller_config=str(self.config),
            denied_roots=(str(self.repo), str(self.common / "autoforge")),
            operator_paths=EXTRA_READ_ONLY,
            provider_state=tuple(self.roots),
        )

    def request(self, prompt: str, *, broker=None, ceiling: int = 120) -> AgentRequest:
        return AgentRequest(
            phase="review",
            prompt=prompt,
            cwd=str(self.worktree),
            profile=self.profile,
            idle_timeout_seconds=60,
            max_runtime_seconds=ceiling,
            env_allowlist=default_config().execution.agent_environment_names(),
            env={"GIT_CONFIG_GLOBAL": str(self.config / "gitconfig")},
            sandbox=SandboxPlan(self.facts()),
            state_dir=str(self.sandbox),
            interaction=broker
            or InteractionBroker(
                interaction_settings(self.profile.options, "p"), Capabilities(), Scripted(None)
            ),
        )


@pytest.fixture
def gate(tmp_path, monkeypatch, backend):
    with StandIn() as standin:
        made = Gate(tmp_path, monkeypatch, standin)
        try:
            yield made
        finally:
            # What the launch left read-only, made removable for tmp_path's cleanup.
            sandbox_bwrap._make_removable(str(made.sandbox / "overlay-work"))
            for path in made.sandbox.rglob("*"):
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o755)


def _contained(result) -> None:
    assert result.sandbox is not None and result.sandbox.enforced
    assert not result.may_have_survivor, result.leftovers


def test_g_o1_o12_o13_a_turn_binds_its_session_and_input_and_answers(gate):
    """G-O1, G-O12, G-O13 and G-O10 for a normal end: the session under the
    caller's id, the version read back, the port read from the readiness line
    while 4096 is taken, the server stopped and the group empty."""
    taken = socket.socket()
    try:
        taken.bind(("127.0.0.1", 4096))
        taken.listen(1)
    except OSError:
        pass  # already taken by something else: the same case
    try:
        result = gate.provider.execute(gate.request("plain"))
    finally:
        taken.close()
    assert result.provider_failure is None, (result.provider_failure, result.stderr[-2000:])
    assert result.stdout == ANSWER
    summary = result.provider_summary
    assert summary["runtime_version"] == "1.18.34"
    assert summary["server_stopped"] in ("sigterm", "exited")
    _contained(result)
    # The session's history persists in the data root, outside the worktree.
    data = gate.sandbox / "sdk" / "opencode-sdk" / "data" / "opencode"
    assert (data / "opencode.db").exists()


def test_g_o2_o8_an_input_is_admitted_once_and_its_answer_recovered(gate):
    """G-O2: an identical input re-admitted under its id is read back, never
    sent again, and another input under that id is a conflict. G-O8: a new
    server on the same data root reads the answer from the history."""
    req = gate.request("plain")
    first = gate.provider.start_for(req, RUNTIME)
    assert gate.provider.run_bridge(req, first).stdout == ANSWER
    again = BridgeStart(**{**first.__dict__, "mode": BridgeMode.CONTINUE})
    resent = gate.provider.run_bridge(gate.request("plain"), again)
    assert resent.provider_failure is None and resent.stdout == ANSWER
    assert resent.provider_summary["input_sent"] is False
    conflict = BridgeStart(**{**again.__dict__, "prompt": "something else"})
    refused = gate.provider.run_bridge(gate.request("x"), conflict)
    assert "holds a different input (a conflict)" in refused.provider_failure
    recovered = gate.provider.run_bridge(
        gate.request("plain"), BridgeStart(**{**first.__dict__, "mode": BridgeMode.RECOVER})
    )
    assert recovered.provider_failure is None and recovered.stdout == ANSWER
    for result in (resent, refused, recovered):
        _contained(result)


def test_g_o3_a_live_question_is_answered(gate):
    """G-O3 (one question): the ask reaches the broker and the answer the model."""
    live = Capabilities(live_questions=True)
    resolver = Scripted(Answer((QuestionResponse(choices=("blue",)),)))
    broker = InteractionBroker(interaction_settings(gate.profile.options, "p"), live, resolver)
    result = gate.provider.execute(gate.request("GATE:question", broker=broker))
    assert result.provider_failure is None, (result.provider_failure, result.stderr[-2000:])
    assert '\\"Which colour?\\"=\\"blue\\"' in result.stdout
    assert result.stdout.endswith(ANSWER)
    (request,) = resolver.requests
    assert [option.label for option in request.questions[0].options] == ["red", "blue"]
    _contained(result)


def test_g_o3_several_questions_take_several_picks_and_custom_words(gate):
    """G-O3 (the rest of its row): two questions in one ask, one taking
    several picks and one an answer of the operator's own words, answered in
    order through the adapter's own capabilities."""
    resolver = Scripted(
        Answer(
            (
                QuestionResponse(choices=("red", "blue")),
                QuestionResponse(custom_text="my own words"),
            )
        )
    )
    broker = InteractionBroker(
        interaction_settings(gate.profile.options, "p"),
        gate.provider.interaction_capabilities(gate.profile),
        resolver,
    )
    result = gate.provider.execute(gate.request("GATE:questions", broker=broker))
    assert result.provider_failure is None, (result.provider_failure, result.stderr[-2000:])
    (request,) = resolver.requests
    first, second = request.questions
    assert first.multiple and not second.multiple
    assert first.custom and second.custom  # the runtime's default (QuestionInfo)
    assert '\\"Which colours?\\"=\\"red, blue\\"' in result.stdout
    assert '\\"Which name?\\"=\\"my own words\\"' in result.stdout
    _contained(result)


def test_g_o5_o10_an_interrupt_ends_the_turn_and_leaves_nothing(gate):
    """G-O5 and G-O10 at a limit: the turn is aborted, not only killed (its
    message ends with the runtime's abort error, read back by a recovery),
    and nothing of the launch is left."""
    req = gate.request("GATE:slow", ceiling=10)
    start = gate.provider.start_for(req, RUNTIME)
    result = gate.provider.run_bridge(req, start)
    assert result.timed_out and result.timeout_limit == "max_runtime"
    _contained(result)
    recovered = gate.provider.run_bridge(
        gate.request("GATE:slow"), BridgeStart(**{**start.__dict__, "mode": BridgeMode.RECOVER})
    )
    assert "MessageAbortedError" in recovered.provider_failure
