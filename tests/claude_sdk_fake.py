"""A fake ``claude_agent_sdk`` for the Claude SDK helper's tests (#151).

The real helper program (``sdk_helpers/claude_helper.py``) runs unchanged as
a child, under the real adapter and the real duplex handle, in a throwaway
virtual environment whose site-packages holds this fake instead of the SDK.
Nothing reaches a network or a model, and no real SDK or Claude Code is
needed.

The fake keeps the names, fields and call shapes the helper uses (checked
against claude-agent-sdk 0.2.163): the message and block dataclasses,
``ClaudeAgentOptions``, ``HookMatcher``, the permission results, and a
``ClaudeSDKClient`` with ``connect``, ``query``, ``receive_response``,
``interrupt`` and ``disconnect``. What a session does is scripted by its
prompt, a JSON object (:func:`script`):

``init``
    fields merged over the ``system/init`` the fake emits first, which it
    builds from the options as the pinned CLI does (``false`` emits none);
``steps``
    in order: ``{"message": {...}}`` yields one message (``kind`` names its
    class); ``{"init": {...}}`` yields a ``system/init`` there, built as the
    first one is (for an init that comes late); ``{"tool": {"id", "name",
    "input"}}`` runs the call through the ``PreToolUse`` hook and, for a call
    the hook neither allows nor denies, the allowed tools and the permission
    host, as Claude Code does, then yields the assistant ``tool_use`` and the
    ``tool_result`` (an error for a denied call; with ``"silent": true`` no
    ``tool_use`` comes first, as when the SDK's control channel delivers the
    hook before the message); ``{"sleep": s}`` waits, interruptibly;
    ``{"spawn": argv}`` starts a process that leaves the helper's process
    group; ``{"raise": "name"}`` raises; ``{"flood": n}`` yields ``n`` system
    notices; ``{"stop": true}`` ends the response without a result;
``result``
    fields merged over a completed result (``"result": "<text>"``);
``after_end``
    strings ``disconnect`` writes, as they are, on the helper's wire, which
    it finds by its class name: after the helper's ``end``, once the
    controller has closed its stdin (a record needs its own LF; a string
    without one is an unterminated tail);
``stall``
    names among ``interrupt`` and ``disconnect`` whose call hangs for five
    minutes instead of returning, as an SDK whose CLI no longer answers does
    (the helper bounds each; the controller's limit kills it first).

An interrupt ends a running step and yields an interrupted result
(``error_during_execution``, ``aborted_tools``). Each call of a hook or the
permission host, the options the session was given and the environment
variables named by ``FAKE_SDK_ENV_NAMES`` are appended as JSON lines to
``.fake-sdk.jsonl`` in the working directory, for the test to read.
``connect`` raises when the prompt's script says ``{"connect": "raise"}``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

LOG_NAME = ".fake-sdk.jsonl"

FAKE_SDK = r'''
"""A fake claude_agent_sdk (AutoForge tests, #151)."""
import asyncio
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any

__version__ = "0.2.163"

_LOG = ".fake-sdk.jsonl"
_STALL = 300
_ENV = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_ENV_FILE",
        "CLAUDE_CODE_SHELL", "CLAUDE_CODE_SHELL_PREFIX",
        "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "ANTHROPIC_API_KEY", "HOME", "GH_TOKEN",
        "OPENAI_API_KEY", "FAKE_SECRET_TOKEN")


def _log(cwd, entry):
    with open(os.path.join(cwd, _LOG), "a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


@dataclass
class TextBlock:
    text: str

@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""

@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict

@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: Any = None

@dataclass
class AssistantMessage:
    content: list
    model: str
    parent_tool_use_id: Any = None
    error: Any = None
    usage: Any = None
    message_id: Any = None
    stop_reason: Any = None
    session_id: Any = None
    uuid: Any = None

@dataclass
class UserMessage:
    content: Any
    uuid: Any = None
    parent_tool_use_id: Any = None
    tool_use_result: Any = None
    origin: Any = None

@dataclass
class SystemMessage:
    subtype: str
    data: dict

@dataclass
class DeferredToolUse:
    id: str
    name: str
    input: dict

@dataclass
class ResultMessage:
    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    stop_reason: Any = None
    total_cost_usd: Any = None
    usage: Any = None
    result: Any = None
    structured_output: Any = None
    model_usage: Any = None
    permission_denials: Any = None
    deferred_tool_use: Any = None
    errors: Any = None
    api_error_status: Any = None
    uuid: Any = None
    terminal_reason: Any = None
    origin: Any = None

@dataclass
class RateLimitInfo:
    status: str

@dataclass
class RateLimitEvent:
    rate_limit_info: Any
    uuid: str = ""
    session_id: str = ""

@dataclass
class StreamEvent:
    uuid: str
    session_id: str
    event: dict

@dataclass
class PermissionResultAllow:
    behavior: str = "allow"
    updated_input: Any = None
    updated_permissions: Any = None

@dataclass
class PermissionResultDeny:
    behavior: str = "deny"
    message: str = ""
    interrupt: bool = False

@dataclass
class ToolPermissionContext:
    signal: Any = None
    suggestions: list = field(default_factory=list)
    tool_use_id: Any = None

@dataclass
class HookMatcher:
    matcher: Any = None
    hooks: list = field(default_factory=list)
    timeout: Any = None


class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Interrupted(Exception):
    pass


class ClaudeSDKClient:
    def __init__(self, options=None, transport=None):
        self.options = options
        self._interrupted = None
        self._script = {}

    async def connect(self, prompt=None):
        o = self.options
        record = {k: v for k, v in o.__dict__.items() if k not in ("can_use_tool", "hooks")}
        record["hooks"] = {k: [{"matcher": m.matcher, "timeout": m.timeout, "n": len(m.hooks)}
                               for m in v] for k, v in (o.hooks or {}).items()}
        record["can_use_tool"] = o.can_use_tool is not None
        env_file = os.environ.get("CLAUDE_ENV_FILE")
        cleaner = open(env_file).read() if env_file and os.path.isfile(env_file) else None
        _log(o.cwd, {"options": record, "env": {k: os.environ.get(k) for k in _ENV},
                     "env_file": cleaner})
        self._interrupted = asyncio.Event()

    async def query(self, prompt, session_id="default"):
        self._script = json.loads(prompt)
        if self._script.get("connect") == "raise":
            raise RuntimeError("the fake SDK failed to start: sk-ant-api03-" + "z" * 40)

    async def interrupt(self):
        if "interrupt" in self._script.get("stall", ()):
            await asyncio.sleep(_STALL)
        self._interrupted.set()

    async def disconnect(self):
        if "disconnect" in self._script.get("stall", ()):
            await asyncio.sleep(_STALL)
        late = self._script.get("after_end")
        if late:
            import gc
            wire = next(o for o in gc.get_objects() if type(o).__name__ == "Wire")
            for text in late:
                wire._write(text.encode())

    def _init(self, extra=None):
        o = self.options
        init = {"type": "system", "subtype": "init", "session_id": o.session_id,
                "model": o.model or "fake-model", "permissionMode": o.permission_mode,
                "apiKeySource": ("ANTHROPIC_API_KEY" if os.environ.get("ANTHROPIC_API_KEY")
                                 else "none"),
                "tools": list(o.tools), "mcp_servers": [], "plugins": [], "cwd": o.cwd}
        extra = self._script.get("init", {}) if extra is None else extra
        if extra is False:
            return None
        init.update(extra)
        return SystemMessage(subtype="init", data=init)

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self._interrupted.wait(), seconds)
        except asyncio.TimeoutError:
            return
        raise _Interrupted()

    def _interrupted_result(self, turns):
        o = self.options
        return ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
                             is_error=True, num_turns=turns, session_id=o.session_id,
                             stop_reason="tool_use", terminal_reason="aborted_tools",
                             result=None, errors=["interrupted"])

    async def _tool(self, spec):
        o = self.options
        name, tool_input, call = spec["name"], spec.get("input", {}), spec.get("id", "toolu_x")
        hook = o.hooks["PreToolUse"][0].hooks[0]
        data = {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": tool_input,
                "tool_use_id": call, "session_id": o.session_id, "cwd": o.cwd,
                "transcript_path": "", **({"agent_id": spec["agent"]} if "agent" in spec else {})}
        out = await hook(data, call, {"signal": None})
        _log(o.cwd, {"hook": name, "tool_use_id": call, "output": out})
        decision = (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")
        used = tool_input
        if decision == "deny":
            return False, out["hookSpecificOutput"].get("permissionDecisionReason", "")
        if decision == "allow":
            used = out["hookSpecificOutput"].get("updatedInput", tool_input)
            if name == "AskUserQuestion" and "answers" not in used:
                decision = None
        if decision is None and name not in o.allowed_tools:
            host = await o.can_use_tool(name, tool_input, ToolPermissionContext(tool_use_id=call))
            _log(o.cwd, {"host": name, "result": asdict(host)})
            if host.behavior != "allow":
                return False, host.message
        return True, json.dumps(used, sort_keys=True)

    def _message(self, spec):
        o = self.options
        spec = dict(spec)
        kind = spec.pop("kind")
        if kind == "assistant":
            blocks = []
            for b in spec.pop("content", [{"type": "text", "text": "thinking aloud"}]):
                if b["type"] == "text":
                    blocks.append(TextBlock(b["text"]))
                elif b["type"] == "tool_use":
                    blocks.append(ToolUseBlock(b["id"], b["name"], b["input"]))
                elif b["type"] == "thinking":
                    blocks.append(ThinkingBlock(b.get("thinking", "")))
            return AssistantMessage(content=blocks, model=o.model or "fake-model",
                                    session_id=o.session_id, **spec)
        if kind == "user":
            blocks = [ToolResultBlock(b["tool_use_id"], b.get("content"), b.get("is_error"))
                      for b in spec.pop("content", [])]
            return UserMessage(content=blocks, **spec)
        if kind == "system":
            return SystemMessage(subtype=spec["subtype"], data={"type": "system", **spec})
        if kind == "rate_limit":
            return RateLimitEvent(RateLimitInfo(spec.get("status", "allowed")))
        if kind == "stream":
            return StreamEvent(uuid="u", session_id=o.session_id, event={})
        raise ValueError(kind)

    async def receive_response(self):
        o = self.options
        script = self._script
        turns = 1
        init = self._init()
        if init is not None:
            yield init
        try:
            for step in script.get("steps", []):
                if "message" in step:
                    yield self._message(step["message"])
                elif "init" in step:
                    yield self._init(step["init"])
                elif "tool" in step:
                    spec = step["tool"]
                    if not spec.get("silent"):
                        yield AssistantMessage(
                            content=[ToolUseBlock(spec.get("id", "toolu_x"), spec["name"],
                                                  spec.get("input", {}))],
                            model=o.model or "fake-model", session_id=o.session_id)
                    ok, content = await self._tool(spec)
                    turns += 1
                    yield UserMessage(content=[ToolResultBlock(spec.get("id", "toolu_x"), content,
                                                               None if ok else True)])
                    await self._wait(0)
                elif "sleep" in step:
                    await self._wait(step["sleep"])
                elif "spawn" in step:
                    subprocess.Popen(step["spawn"], start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif "raise" in step:
                    raise {"runtime": RuntimeError, "value": ValueError}[step["raise"]](
                        "the fake SDK failed: sk-ant-api03-" + "y" * 40)
                elif "flood" in step:
                    for n in range(step["flood"]):
                        yield SystemMessage(subtype="status", data={"type": "system",
                                                                    "subtype": "status", "n": n})
                elif step.get("stop"):
                    return
                elif "print" in step:
                    print(step["print"])
                    os.write(2, (step["print"] + "\n").encode())
        except _Interrupted:
            yield self._interrupted_result(turns)
            return
        if self._interrupted.is_set():
            yield self._interrupted_result(turns)
            return
        result = {"subtype": "success", "duration_ms": 5, "duration_api_ms": 3, "is_error": False,
                  "num_turns": turns, "session_id": o.session_id, "stop_reason": "end_turn",
                  "terminal_reason": "completed", "total_cost_usd": 0.0012,
                  "usage": {"input_tokens": 10, "output_tokens": 4, "service_tier": "standard"},
                  "result": "", "permission_denials": []}
        result.update(script.get("result", {}))
        deferred = result.get("deferred_tool_use")
        if isinstance(deferred, dict):
            result["deferred_tool_use"] = DeferredToolUse(**deferred)
        yield ResultMessage(**result)
'''


def script(**fields: object) -> str:
    """A session's script as the prompt that carries it."""
    return json.dumps(fields)


def make_environment(root: Path, *, version: str = "0.2.163", cli: str = "2.1.286") -> Path:
    """A virtual environment at ``root`` whose ``claude_agent_sdk`` is the fake,
    with its bundled CLI and versions as given; its interpreter's path."""
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(root)],
        check=True,
        capture_output=True,
    )
    lib = next((root / "lib").glob("python3.*")) / "site-packages" / "claude_agent_sdk"
    (lib / "_bundled").mkdir(parents=True)
    (lib / "__init__.py").write_text(
        FAKE_SDK.replace('__version__ = "0.2.163"', f"__version__ = {version!r}"),
        encoding="utf-8",
    )
    (lib / "_cli_version.py").write_text(f"__cli_version__ = {cli!r}\n", encoding="utf-8")
    bundled = lib / "_bundled" / "claude"
    bundled.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bundled.chmod(0o755)
    return root / "bin" / "python"


def log(cwd: Path) -> list[dict]:
    """What the fake recorded in ``cwd``: its options, hook and host calls."""
    path = cwd / LOG_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
