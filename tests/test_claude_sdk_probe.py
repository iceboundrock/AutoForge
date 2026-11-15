"""Opt-in gate probes of ``claude-sdk`` on an installed pinned pair (#151; ADR 0007 §3).

Each probe is a scripted launch through the real adapter and the real helper
(:meth:`autoforge.providers.ClaudeSdkProvider.execute`), in the Claude helper
environment ``AUTOFORGE_CLAUDE_SDK_PROBE_PYTHON`` names (an interpreter with
exactly ``claude-agent-sdk`` 0.2.163 installed from ``helpers/claude-sdk/``),
against a loopback stand-in of the Messages API that answers each request
with a scripted assistant message. No model is called, the key is synthetic,
and ``HOME`` and the configuration and session root are temporary
directories, so no credential or setting of the operator's is read and
nothing is spent::

    AUTOFORGE_CLAUDE_SDK_PROBE_PYTHON=/path/to/env/bin/python \\
        uv run pytest tests/test_claude_sdk_probe.py

The gates (ADR 0007 §3's Claude table, amended by #151): G-C1 the session
binds the caller's id and its transcript lies in the root; G-C3 live
questions, one or several, several labels, an answer of the operator's own
words; G-C4 a permission approved once, with a modified input, or denied
with guidance; G-C5 a limit interrupts a running tool and the run ends
within the wind-down; G-C9 nothing the launch started is left; G-C10 a hook
waiting for the operator is not timed out by Claude Code. The Bash tool runs
no function or alias of a shell startup file (#165's isolation, with a
control run that sees both without it). The ``sandboxed``
variants run the same launch inside the outer sandbox (``bwrap``, a REMOTE
agent's facts, the configuration and session root an engine carve-out) and
skip with the backend probe's reason where the host cannot enforce.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from autoforge import sandbox_bwrap
from autoforge.config import ProfileConfig
from autoforge.interaction import (
    Allow,
    Answer,
    Deny,
    InteractionBroker,
    InteractionRequest,
    QuestionResponse,
)
from autoforge.providers import AgentRequest, ClaudeSdkProvider
from autoforge.sandbox_policy import LaunchClass, LaunchFacts, SandboxPlan
from autoforge.sdk_policy import CLAUDE_SDK, ClaudeSdkRoots, interaction_settings

from .conftest import real_sandbox_backend

PYTHON = os.environ.get("AUTOFORGE_CLAUDE_SDK_PROBE_PYTHON", "")
pytestmark = pytest.mark.skipif(
    not PYTHON,
    reason="opt-in: set AUTOFORGE_CLAUDE_SDK_PROBE_PYTHON to a Claude helper environment's python",
)
KEY = "sk-ant-api03-" + "p" * 48  # synthetic: the stand-in never checks it
FINAL = "done\n<<<CONTROL_RESULT>>>\n{}\n<<<END_CONTROL_RESULT>>>"


class _StandIn(BaseHTTPRequestHandler):
    """Answers the n-th tool-bearing request with ``script[n]``'s content
    blocks, streamed as the Messages API streams them; anything else ``ok``."""

    script: list[list[dict]] = []
    requests: list[dict] = []
    calls = 0
    lock = threading.Lock()

    def log_message(self, *args) -> None:
        pass

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        self._json({})

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            request = json.loads(body)
        except ValueError:
            request = {}
        if "count_tokens" in self.path:
            self._json({"input_tokens": 1})
            return
        cls = type(self)
        blocks: list[dict] = [{"type": "text", "text": "ok"}]
        with cls.lock:
            if request.get("tools"):
                cls.requests.append(request)
                if cls.calls < len(cls.script):
                    blocks = cls.script[cls.calls]
                cls.calls += 1
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        message = {
            "id": f"msg_{cls.calls}",
            "type": "message",
            "role": "assistant",
            "model": request.get("model", "m"),
            "content": blocks,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
        if not request.get("stream"):
            self._json(message)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        events = [("message_start", {"message": {**message, "content": [], "stop_reason": None}})]
        for index, block in enumerate(blocks):
            if block["type"] == "text":
                start = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": block["text"]}
            else:
                start = {**block, "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            events += [
                ("content_block_start", {"index": index, "content_block": start}),
                ("content_block_delta", {"index": index, "delta": delta}),
                ("content_block_stop", {"index": index}),
            ]
        events += [
            ("message_delta", {"delta": {"stop_reason": stop}, "usage": {"output_tokens": 2}}),
            ("message_stop", {}),
        ]
        for name, data in events:
            payload = json.dumps({"type": name, **data})
            self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode())

    def _json(self, obj: object) -> None:
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stand_in(monkeypatch, tmp_path) -> Iterator[type[_StandIn]]:
    _StandIn.script, _StandIn.requests, _StandIn.calls = [], [], 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StandIn)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield _StandIn
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


class _Scripted:
    """A resolver that answers each request with the next scripted decision,
    after ``delay`` seconds, and keeps what it was asked."""

    def __init__(self, *decisions, delay: float = 0.0) -> None:
        self.decisions = list(decisions)
        self.delay = delay
        self.asked: list[InteractionRequest] = []

    def resolve(self, request: InteractionRequest, deadline: float):
        self.asked.append(request)
        time.sleep(self.delay)
        return self.decisions.pop(0)


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


_OVERLAYS: list[Path] = []


@pytest.fixture(autouse=True)
def _removable_overlays() -> Iterator[None]:
    """bwrap's overlay work directory is left unreadable; make it removable."""
    yield
    while _OVERLAYS:
        sandbox_bwrap._make_removable(str(_OVERLAYS.pop()))


def _sandbox(tmp_path: Path) -> tuple[Path, Path, SandboxPlan]:
    """An issue worktree, its sandbox directory with the state root, and the
    REMOTE agent's sandbox plan for it, laid out as the engine lays them out."""
    base = Path(os.path.realpath(tmp_path))
    repo = base / "repo"
    _git("init", "-q", "-b", "main", str(repo))
    _git(
        "-C",
        str(repo),
        "-c",
        "user.name=p",
        "-c",
        "user.email=p@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "initial",
    )
    common = repo / ".git"
    work = common / "autoforge" / "worktrees" / "7"
    _git("-C", str(repo), "worktree", "add", "-q", "--detach", str(work), "HEAD")
    state = common / "autoforge" / "sandbox" / "7"
    for name in ("objects", "overlay-work", "cache"):
        (state / name).mkdir(parents=True)
    _OVERLAYS.append(state / "overlay-work")
    roots = ClaudeSdkRoots.under(state)
    roots.config.mkdir(parents=True)
    facts = LaunchFacts(
        launch_class=LaunchClass.REMOTE_AGENT,
        workspace=str(work),
        home=os.environ["HOME"],
        controller_env=dict(os.environ),
        git_entry=str(work / ".git"),
        git_dir=str(common / "worktrees" / "7"),
        common_dir=str(common),
        private_objects=str(state / "objects"),
        overlay_work=str(state / "overlay-work"),
        cache=str(state / "cache"),
        denied_roots=(str(repo), str(common / "autoforge")),
        provider_state=(str(roots.config),),
    )
    return work, state, SandboxPlan(facts)


def _launch(
    tmp_path: Path,
    script: list[list[dict]],
    options=None,
    resolver=None,
    *,
    sandboxed: bool = False,
    **limits,
):
    _StandIn.script = script
    options = dict(options or {})
    profile = ProfileConfig(
        name="fix", provider=CLAUDE_SDK, model="claude-fable-5-2", command=PYTHON, options=options
    )
    adapter = ClaudeSdkProvider()
    adapter.validate_profile(profile)
    plan = None
    if sandboxed:
        work, state, plan = _sandbox(tmp_path)
    else:
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        state = tmp_path / "sandbox"
    roots = ClaudeSdkRoots.under(state)
    roots.config.mkdir(parents=True, exist_ok=True)
    settings = interaction_settings(options, "probe")
    broker = InteractionBroker(
        settings, adapter.interaction_capabilities(profile), resolver or _Scripted()
    )
    req = AgentRequest(
        phase="fix",
        prompt="Probe: reply as scripted.",
        cwd=str(work),
        profile=profile,
        idle_timeout_seconds=limits.get("idle", 300),
        max_runtime_seconds=limits.get("ceiling", 300),
        env_allowlist=("PATH", "HOME", "ANTHROPIC_BASE_URL"),
        interaction=broker,
        state_dir=str(state),
        sandbox=plan,
    )
    adapter.integration_preflight(req.env_allowlist)
    started = time.monotonic()
    result = adapter.execute(req)
    return result, roots, time.monotonic() - started, broker


def _tool_results() -> list[dict]:
    """The tool results the model was sent in the second scripted request."""
    second = _StandIn.requests[1]
    return [
        block
        for message in second["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]


def _nothing_left(marker: str) -> None:
    found = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
    assert not found.strip(), f"a process of the launch outlived it: {marker}"


def test_g_c1_a_completed_turn_binds_the_callers_session_and_root(tmp_path, stand_in):
    result, roots, _, broker = _launch(tmp_path, [[{"type": "text", "text": FINAL}]])
    assert result.provider_failure is None and result.exit_code == 0 and not result.timed_out
    assert result.stdout == FINAL
    session = result.provider_summary["session_id"]
    assert result.provider_summary["sdk_version"] == "0.2.163"
    assert result.provider_summary["cli_version"] == "2.1.286"
    assert result.provider_summary["terminal_reason"] == "completed"
    transcripts = [path.name for path in roots.config.rglob("*.jsonl")]
    assert transcripts == [f"{session}.jsonl"]
    assert KEY not in result.stderr
    assert result.reply_delivered and broker.handled == 0


def test_g_c3_a_question_is_answered_live(tmp_path, stand_in):
    question = {
        "questions": [
            {
                "question": "Which colors?",
                "header": "Colors",
                "multiSelect": True,
                "options": [
                    {"label": "Red", "description": "r"},
                    {"label": "Blue", "description": "b"},
                ],
            },
            {
                "question": "Which size?",
                "header": "Size",
                "multiSelect": False,
                "options": [
                    {"label": "S", "description": "s"},
                    {"label": "M", "description": "m"},
                ],
            },
        ]
    }
    resolver = _Scripted(
        Answer(
            (
                QuestionResponse(choices=("Red", "Blue")),
                QuestionResponse(custom_text="Extra large"),
            )
        )
    )
    script = [
        [{"type": "tool_use", "id": "toolu_q", "name": "AskUserQuestion", "input": question}],
        [{"type": "text", "text": FINAL}],
    ]
    result, _, _, broker = _launch(tmp_path, script, {"interaction_policy": "prompt"}, resolver)
    assert result.provider_failure is None and result.stdout == FINAL
    assert len(resolver.asked) == 1 and len(resolver.asked[0].questions) == 2
    (answered,) = _tool_results()
    assert '"Which colors?"="Red, Blue"' in answered["content"]
    assert '"Which size?"="Extra large"' in answered["content"]
    assert not answered.get("is_error")


@pytest.mark.parametrize(
    ("decision", "expected", "is_error"),
    [
        (Allow(), "original", False),
        (Allow(updated_input={"command": "echo modified", "description": "m"}), "modified", False),
        (Deny(guidance="Use Read instead."), "Use Read instead.", True),
    ],
)
def test_g_c4_a_permission_is_decided_live(tmp_path, stand_in, decision, expected, is_error):
    call = {"command": "echo original", "description": "o"}
    script = [
        [{"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": call}],
        [{"type": "text", "text": FINAL}],
    ]
    options = {"interaction_policy": "prompt", "ask_tools": "Bash"}
    result, _, _, _ = _launch(tmp_path, script, options, _Scripted(decision))
    assert result.provider_failure is None and result.stdout == FINAL
    (outcome,) = _tool_results()
    assert expected in json.dumps(outcome["content"])
    assert bool(outcome.get("is_error")) is is_error


def test_g_c4_two_calls_in_one_turn_are_each_approved_once(tmp_path, stand_in):
    script = [
        [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "echo 1"}},
            {"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {"command": "echo 2"}},
        ],
        [{"type": "text", "text": FINAL}],
    ]
    resolver = _Scripted(Allow(), Allow())
    options = {"interaction_policy": "prompt", "ask_tools": "Bash"}
    result, _, _, _ = _launch(tmp_path, script, options, resolver)
    assert result.provider_failure is None and len(resolver.asked) == 2
    assert {json.dumps(r["content"]) for r in _tool_results()} == {'"1"', '"2"'}


def test_g_c5_g_c9_a_limit_interrupts_a_running_tool_and_leaves_nothing(tmp_path, stand_in):
    marker = f"sleep 61.{os.getpid()}"
    script = [
        [{"type": "tool_use", "id": "toolu_s", "name": "Bash", "input": {"command": marker}}],
        [{"type": "text", "text": FINAL}],
    ]
    result, _, took, _ = _launch(tmp_path, script, ceiling=8)
    assert result.timed_out and result.timeout_limit == "max_runtime"
    assert result.stdout == "" and result.provider_failure is None
    assert result.provider_summary["outcome"] == "interrupted"
    assert took < 8 + ClaudeSdkProvider().interrupt_seconds
    _nothing_left(marker)


def test_g_c10_a_waiting_hook_is_not_timed_out(tmp_path, stand_in):
    script = [
        [{"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {"command": "echo late"}}],
        [{"type": "text", "text": FINAL}],
    ]
    options = {"interaction_policy": "prompt", "ask_tools": "Bash", "max_wait_seconds": "10"}
    result, _, _, _ = _launch(tmp_path, script, options, _Scripted(Allow(), delay=4))
    assert result.provider_failure is None
    (outcome,) = _tool_results()
    assert outcome["content"] == "late" and not outcome.get("is_error")


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> str:
    return real_sandbox_backend(tmp_path_factory)


def test_sandboxed_g_c1_g_c4_a_decided_turn_completes_inside_the_sandbox(
    tmp_path, stand_in, backend
):
    call = {"command": "echo inside", "description": "i"}
    script = [
        [{"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": call}],
        [{"type": "text", "text": FINAL}],
    ]
    options = {"interaction_policy": "prompt", "ask_tools": "Bash"}
    result, roots, _, _ = _launch(tmp_path, script, options, _Scripted(Allow()), sandboxed=True)
    assert result.sandbox is not None and result.sandbox.enforced
    assert result.provider_failure is None and result.stdout == FINAL
    (outcome,) = _tool_results()
    assert outcome["content"] == "inside"
    session = result.provider_summary["session_id"]
    assert [path.name for path in roots.config.rglob("*.jsonl")] == [f"{session}.jsonl"]


def test_sandboxed_g_c5_g_c9_a_limit_inside_the_sandbox_leaves_nothing(tmp_path, stand_in, backend):
    marker = f"sleep 62.{os.getpid()}"
    script = [
        [{"type": "tool_use", "id": "toolu_s", "name": "Bash", "input": {"command": marker}}],
        [{"type": "text", "text": FINAL}],
    ]
    result, _, took, _ = _launch(tmp_path, script, ceiling=8, sandboxed=True)
    assert result.sandbox is not None and result.sandbox.enforced
    assert result.timed_out and result.provider_summary["outcome"] == "interrupted"
    assert took < 8 + ClaudeSdkProvider().interrupt_seconds
    _nothing_left(marker)


_SHELL_PROBE = "type sentinelfn 2>&1 | head -1; alias | grep -c sentinel; declare -F | wc -l"


def _startup_file_probe(tmp_path: Path, monkeypatch) -> str:
    """What a Bash tool command sees of a function and an alias ``~/.bashrc``
    defines, the shell snapshot Claude Code takes of it included."""
    home = Path(os.environ["HOME"])
    (home / ".bashrc").write_text(
        "sentinelfn() { echo LEAKED; }\nalias sentinelalias='echo LEAKED'\n", encoding="utf-8"
    )
    monkeypatch.setenv("SHELL", "/bin/bash")
    call = {"command": _SHELL_PROBE}
    script = [
        [{"type": "tool_use", "id": "toolu_s", "name": "Bash", "input": call}],
        [{"type": "text", "text": FINAL}],
    ]
    result, _, _, _ = _launch(tmp_path, script)
    assert result.provider_failure is None
    (outcome,) = _tool_results()
    return outcome["content"]


def test_the_bash_tool_runs_no_function_or_alias_of_a_startup_file(tmp_path, stand_in, monkeypatch):
    """#165's shell isolation, on the SDK's bundled Claude Code 2.1.286."""
    seen = _startup_file_probe(tmp_path, monkeypatch)
    assert "not found" in seen and seen.splitlines()[1:3] == ["0", "0"]


def test_the_startup_file_probe_sees_a_function_without_the_isolation(
    tmp_path, stand_in, monkeypatch
):
    """The control run: without the cleaner, the snapshot brings both in."""
    from contextlib import contextmanager

    import autoforge.providers as providers

    @contextmanager
    def no_isolation():
        yield {}

    monkeypatch.setattr(providers, "claude_shell_isolation", no_isolation)
    monkeypatch.setattr(providers, "complete_claude_isolation", lambda isolation, env: None)
    seen = _startup_file_probe(tmp_path, monkeypatch)
    assert "sentinelfn is a function" in seen and seen.splitlines()[1] == "1"
