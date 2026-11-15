"""AutoForge's Claude Agent SDK helper: envelope ``autoforge-claude-helper``, protocol 1.

A separate program, never a module of AutoForge (ADR 0007 D2.1, D2.2): it
imports the standard library and ``claude_agent_sdk`` only, nothing imports
it, and the controller runs it as ``<interpreter> -I <staged copy>`` in the
operator's Claude helper environment (D2.3), as one duplex invocation
(``autoforge.executor_duplex``) whose process group holds the SDK's Claude
Code process and everything that starts (D3.2). Its stdout is the wire and
nothing else: file descriptor 1 is kept for the records and pointed at
stderr for the rest of the process, so a stray ``print`` cannot corrupt a
record. Diagnostics go to stderr, which the controller drains and bounds.

The envelope (``autoforge.claude_sdk`` is the controller's side and names
every field; ADR 0007 §5): one JSON object per LF-terminated line, UTF-8.

1. ``hello`` (helper): the envelope, protocol, SDK, bundled CLI and Python
   versions, before anything else is read.
2. ``start`` (controller): the session id, the prompt, the working
   directory, model and effort, the policy's flat fields
   (``sdk_policy.ClaudeSdkPolicy.start_fields``), the hook timeout and the
   record bound. Each policy field goes to ``ClaudeAgentOptions`` and
   nothing else is set; the Claude Code binary is the one bundled with the
   SDK (D2.4).
3. ``message`` (helper): each SDK message, re-serialized to the fields the
   controller's reducer reads. No assistant or thinking text, tool result
   content or raw usage object crosses: only the final ``ResultMessage``'s
   ``result`` carries text.
4. ``ask`` / ``decision``: an interaction-eligible call's ``PreToolUse``
   hook asks, and waits for the controller's directive (D9.2).
5. ``interrupt`` (controller) or end of file on stdin: the running turn is
   interrupted, its interrupted result drained within a bound, and the
   session ends.
6. ``end`` (helper): the outcome (``result`` or ``interrupted``) and the
   counts; then, once stdin is closed, ``disconnect()`` and exit 0.
   ``failure`` (helper): a bounded reason, then exit non-zero.

The helper sets one variable of its own, ``CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK``
(D4.1): the controller checks the pair exactly from ``hello``. It reads no
credential file, sets up no containment and installs no signal handler
(D3.2, D3.3). Python 3.10 or newer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import sys
import threading
from pathlib import Path
from typing import Any

ENVELOPE = "autoforge-claude-helper"
PROTOCOL = 1
# What the helper accepts on stdin in one record: a ``start`` carries the
# rendered prompt, every other record is small.
MAX_INBOUND_BYTES = 8 * 1024 * 1024
# The SDK's buffer for one line of Claude Code's own stdout (its default is
# 1 MiB, past which the whole session fails).
SDK_MAX_BUFFER_BYTES = 32 * 1024 * 1024
# How long an interrupted turn is given to deliver its interrupted result,
# and ``interrupt()`` itself to be acknowledged.
INTERRUPT_DRAIN_SECONDS = 10.0
# How long ``disconnect()`` may take: the SDK's own bound is 5 s for stdin,
# 5 s after SIGTERM and 5 s after SIGKILL.
DISCONNECT_SECONDS = 20.0
MAX_REASON_CHARS = 400

# What a PreToolUse hook does with a call (``sdk_policy``: PASS, ASK, DENY).
PASS = "pass"
ASK = "ask"
DENY = "deny"
QUESTION_TOOL = "AskUserQuestion"

_START_KEYS = {
    "type",
    "session_id",
    "mode",
    "prompt",
    "cwd",
    "model",
    "effort",
    "permission_mode",
    "policy",
    "hook_timeout_seconds",
    "interactions",
    "max_record_bytes",
}
_POLICY_LISTS = ("tools", "allowed_tools", "disallowed_tools", "ask_tools")


class HelperError(Exception):
    """A failure the helper reports in its ``failure`` record."""


def canonical_json(value: object) -> bytes:
    """``value`` as AutoForge's canonical JSON (``sdk_policy.canonical_json``)."""
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return text.encode("utf-8")


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 3] + "..."


class Wire:
    """The records the helper writes: whole lines on the original stdout only."""

    def __init__(self) -> None:
        # The wire keeps a private copy of fd 1; fd 1 becomes stderr, so
        # anything else this process prints cannot reach the controller as a
        # record (the SDK starts Claude Code with pipes of its own).
        self._fd = os.dup(1)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        self._lock = threading.Lock()
        self.limit = MAX_INBOUND_BYTES

    def fits(self, record: dict[str, Any]) -> bytes | None:
        # ASCII with escapes: every line is valid UTF-8, a lone surrogate in a
        # tool's input included, which the controller then refuses itself.
        try:
            line = json.dumps(record, separators=(",", ":"), allow_nan=False).encode("ascii")
        except (TypeError, ValueError):
            return None
        if b"\n" in line or len(line) > self.limit:
            return None
        return line

    def send(self, record: dict[str, Any]) -> None:
        line = self.fits(record)
        if line is None:
            raise HelperError(f"a {record.get('type')} record does not fit one line")
        self._write(line + b"\n")

    def failure(self, reason: str) -> None:
        line = self.fits({"type": "failure", "reason": _short(reason)})
        if line is not None:
            try:
                self._write(line + b"\n")
            except OSError:
                pass

    def _write(self, data: bytes) -> None:
        with self._lock:
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                view = view[written:]


class Inbox:
    """The controller's records, read from stdin on a thread and handed to
    the event loop; ``None`` is end of file."""

    def __init__(self) -> None:
        self._buffer = b""
        self._eof = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._early: list[bytes | None] = []
        self._lock = threading.Lock()

    def first(self) -> bytes | None:
        """The first record, read synchronously before the event loop runs."""
        while b"\n" not in self._buffer and not self._eof:
            self._fill()
        return self._take()

    def attach(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[bytes | None]:
        self._loop = loop
        self._queue = asyncio.Queue()
        threading.Thread(target=self._run, name="autoforge-helper-stdin", daemon=True).start()
        return self._queue

    def _fill(self) -> None:
        chunk = os.read(0, 65536)
        if not chunk:
            self._eof = True
            return
        self._buffer += chunk
        if b"\n" not in self._buffer and len(self._buffer) > MAX_INBOUND_BYTES:
            raise HelperError("a controller record is longer than the helper accepts")

    def _take(self) -> bytes | None:
        if b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            return line[:-1] if line.endswith(b"\r") else line
        return None

    def _run(self) -> None:
        assert self._loop is not None and self._queue is not None
        try:
            while True:
                line = self._take()
                if line is not None:
                    self._put(line)
                    continue
                if self._eof:
                    break
                self._fill()
        except (OSError, HelperError):
            pass
        self._put(None)

    def _put(self, item: bytes | None) -> None:
        assert self._loop is not None and self._queue is not None
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            pass  # the loop has closed: the helper is exiting


def _decode(line: bytes | None, what: str) -> dict[str, Any]:
    if line is None:
        raise HelperError(f"stdin ended before the {what} record")
    try:
        record = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HelperError(f"the {what} record is not JSON") from None
    if not isinstance(record, dict) or not isinstance(record.get("type"), str):
        raise HelperError(f"the {what} record is not a JSON object with a type")
    return record


def _strings(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise HelperError(f"start: {what} is not a list of names")
    return list(value)


def check_start(record: dict[str, Any]) -> dict[str, Any]:
    """The ``start`` record, refused unless it is exactly protocol 1's."""
    if record.get("type") != "start" or set(record) != _START_KEYS:
        raise HelperError("the first controller record is not protocol 1's start")
    if record["mode"] != "new":
        raise HelperError("start: only a new session is supported (a continuation is #126's)")
    for key in ("session_id", "prompt", "cwd", "permission_mode"):
        if not isinstance(record[key], str) or not record[key]:
            raise HelperError(f"start: {key} is not a non-empty string")
    for key in ("model", "effort"):
        if not isinstance(record[key], str):
            raise HelperError(f"start: {key} is not a string")
    for key in ("hook_timeout_seconds", "max_record_bytes"):
        if type(record[key]) is not int or record[key] < 1:
            raise HelperError(f"start: {key} is not a positive whole number")
    interactions = record["interactions"]
    if not isinstance(interactions, dict) or set(interactions) != {"live", "defer"}:
        raise HelperError("start: interactions must hold exactly live and defer")
    if interactions["defer"] is not False or not isinstance(interactions["live"], bool):
        raise HelperError("start: a deferral is not supported (it is #126's)")
    policy = record["policy"]
    if not isinstance(policy, dict) or policy.get("provider") != "claude-sdk":
        raise HelperError("start: policy is not a claude-sdk policy")
    for key in _POLICY_LISTS:
        _strings(policy.get(key), key)
    for key, wanted in (
        ("setting_sources", []),
        ("strict_mcp_config", True),
        ("mcp_servers", {}),
        ("plugins", []),
        ("agents", {}),
        ("extra_args", {}),
        ("session_approval", False),
        ("verbatim_prompts", True),
        ("hooks", ["PreToolUse"]),
        ("can_use_tool", DENY),
    ):
        if policy.get(key) != wanted:
            raise HelperError(f"start: policy {key} is not the fixed value")
    if policy.get("permission_mode") != record["permission_mode"]:
        raise HelperError("start: the permission mode differs from the policy's")
    for key in ("settings", "can_use_tool_message"):
        if not isinstance(policy.get(key), str):
            raise HelperError(f"start: policy {key} is not a string")
    if not isinstance(policy.get("system_prompt"), dict):
        raise HelperError("start: policy system_prompt is not an object")
    return record


def _bundled_cli(sdk: Any) -> str:
    """The Claude Code executable bundled in the SDK's platform wheel (D2.4)."""
    path = Path(sdk.__file__).resolve().parent / "_bundled" / "claude"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise HelperError(
            "the SDK's bundled Claude Code executable is missing: the helper environment "
            "must be installed from the platform wheel (ADR 0007 D2.4)"
        )
    return str(path)


def _cli_version() -> str | None:
    try:
        from claude_agent_sdk._cli_version import __cli_version__
    except ImportError:
        return None
    return __cli_version__ if isinstance(__cli_version__, str) else None


# -- what crosses the wire of each SDK message -----------------------------------
def _digest(value: object) -> str:
    try:
        return hashlib.sha256(canonical_json(value)).hexdigest()
    except (TypeError, ValueError, UnicodeEncodeError):
        return hashlib.sha256(repr(value).encode("utf-8", "replace")).hexdigest()


def _block(sdk: Any, block: object) -> dict[str, Any]:
    if isinstance(block, sdk.TextBlock):
        return {"type": "text"}
    if isinstance(block, sdk.ThinkingBlock):
        return {"type": "thinking"}
    if isinstance(block, sdk.ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, sdk.ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "is_error": block.is_error,
            "content_sha256": _digest(block.content),
        }
    return {"type": "other_block"}


def _flat_usage(usage: object) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    return {
        key: value
        for key, value in usage.items()
        if isinstance(key, str) and type(value) is int and value >= 0
    }


# What a result's ``origin`` becomes when it is no object with a non-empty
# string ``kind``: never ``None``, which says the CLI attributed nothing, so
# the controller fails a provenance it cannot read (R3-F2 of PR #489).
MALFORMED_ORIGIN = {"malformed": True}


def _origin(origin: object) -> object:
    """A result's ``origin`` as the reducer reads it: ``None`` when absent,
    its ``kind`` when it is an object with a non-empty string kind, and
    :data:`MALFORMED_ORIGIN` for anything else."""
    if origin is None:
        return None
    if isinstance(origin, dict):
        kind = origin.get("kind")
        if isinstance(kind, str) and kind:
            return kind
    return dict(MALFORMED_ORIGIN)


def message_record(sdk: Any, message: object) -> dict[str, Any] | None:
    """The record of one SDK message, or ``None`` for a type this helper does
    not know (counted, D10.4)."""
    if isinstance(message, sdk.ResultMessage):
        deferred = message.deferred_tool_use
        denials = message.permission_denials if isinstance(message.permission_denials, list) else []
        return {
            "type": "result",
            "subtype": message.subtype,
            "is_error": message.is_error,
            "num_turns": message.num_turns,
            "duration_ms": message.duration_ms,
            "duration_api_ms": message.duration_api_ms,
            "session_id": message.session_id,
            "stop_reason": message.stop_reason,
            "terminal_reason": message.terminal_reason,
            "total_cost_usd": message.total_cost_usd,
            "usage": _flat_usage(message.usage),
            "result": message.result,
            "api_error_status": message.api_error_status,
            "permission_denials": [
                {"tool_name": d.get("tool_name"), "tool_use_id": d.get("tool_use_id")}
                for d in denials
                if isinstance(d, dict)
            ],
            "deferred_tool_use": None
            if deferred is None
            else {
                "id": deferred.id,
                "name": deferred.name,
                "input_sha256": _digest(deferred.input),
            },
            "structured_output": message.structured_output is not None,
            "errors": len(message.errors) if isinstance(message.errors, list) else 0,
            "origin": _origin(message.origin),
        }
    if isinstance(message, sdk.AssistantMessage):
        return {
            "type": "assistant",
            "message": {
                "model": message.model,
                "stop_reason": message.stop_reason,
                "content": [_block(sdk, block) for block in message.content],
            },
            "parent_tool_use_id": message.parent_tool_use_id,
            "session_id": message.session_id,
            "error": message.error,
        }
    if isinstance(message, sdk.UserMessage):
        content = message.content
        blocks = [_block(sdk, block) for block in content] if isinstance(content, list) else []
        return {
            "type": "user",
            "message": {"content": blocks},
            "parent_tool_use_id": message.parent_tool_use_id,
        }
    if isinstance(message, sdk.SystemMessage):
        data = message.data if isinstance(message.data, dict) else {}
        return {**data, "type": "system", "subtype": message.subtype}
    rate_limit = getattr(sdk, "RateLimitEvent", None)
    if rate_limit is not None and isinstance(message, rate_limit):
        info = message.rate_limit_info
        return {"type": "rate_limit_event", "status": getattr(info, "status", None)}
    return None


def _without_inputs(record: dict[str, Any]) -> dict[str, Any]:
    """``record`` with each tool call's input replaced by its digest, for a
    record that does not fit one line with them."""
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return record
    content = []
    for block in message["content"]:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            block = {**block, "input": {"autoforge_omitted_sha256": _digest(block.get("input"))}}
        content.append(block)
    return {**record, "message": {**message, "content": content}}


# -- the session ------------------------------------------------------------------
class Session:
    """One ``ClaudeSDKClient`` session: connect, one query, the response to
    its result, and an orderly end (D1.1, D3.3)."""

    def __init__(self, sdk: Any, wire: Wire, inbox: Inbox, start: dict[str, Any]) -> None:
        self.sdk = sdk
        self.wire = wire
        self.inbox = inbox
        self.start = start
        self.policy = start["policy"]
        self.allowed = set(self.policy["allowed_tools"])
        self.ask_tools = set(self.policy["ask_tools"])
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.asks = 0
        self.hook_denials = 0
        self.host_denials = 0
        self.skipped_unknown = 0
        self.interrupted = False
        self.stdin_closed = asyncio.Event()
        self.interrupt_requested = asyncio.Event()
        self.result_seen = False
        self.client: Any = None
        self.fatal: str | None = None

    def options(self) -> Any:
        sdk = self.sdk
        policy = self.policy
        return sdk.ClaudeAgentOptions(
            tools=list(policy["tools"]),
            allowed_tools=list(policy["allowed_tools"]),
            disallowed_tools=list(policy["disallowed_tools"]),
            system_prompt=dict(policy["system_prompt"]),
            mcp_servers={},
            strict_mcp_config=True,
            permission_mode=policy["permission_mode"],
            session_id=self.start["session_id"],
            model=self.start["model"] or None,
            effort=self.start["effort"] or None,
            cwd=self.start["cwd"],
            cli_path=_bundled_cli(sdk),
            settings=policy["settings"],
            setting_sources=[],
            plugins=[],
            extra_args={},
            env={},
            max_buffer_size=SDK_MAX_BUFFER_BYTES,
            can_use_tool=self.can_use_tool,
            hooks={
                "PreToolUse": [
                    sdk.HookMatcher(
                        matcher=None,
                        hooks=[self.pre_tool_use],
                        timeout=float(self.start["hook_timeout_seconds"]),
                    )
                ]
            },
            verbatim_prompts=True,
        )

    # -- the callbacks --------------------------------------------------------
    async def can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        """The permission host: every call that reaches it is denied (D9.2)."""
        self.host_denials += 1
        return self.sdk.PermissionResultDeny(message=self.policy["can_use_tool_message"])

    @staticmethod
    def _decision(decision: str, reason: str = "", updated: object = None) -> dict[str, Any]:
        out: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": decision}
        if reason:
            out["permissionDecisionReason"] = reason
        if isinstance(updated, dict):
            out["updatedInput"] = updated
        return {"hookSpecificOutput": out}

    async def pre_tool_use(self, data: Any, tool_use_id: str | None, context: Any) -> Any:
        """Every call's policy check (``sdk_policy.ClaudeSdkPolicy.disposition``):
        pass an allowed tool, deny every other but an ask, and send an ask to
        the controller and wait for its directive."""
        name = data.get("tool_name") if isinstance(data, dict) else None
        if not isinstance(name, str):
            self.hook_denials += 1
            return self._decision("deny", self.policy["can_use_tool_message"])
        if name in self.ask_tools:
            return await self._ask(name, data, tool_use_id)
        if name in self.allowed:
            return {}
        self.hook_denials += 1
        return self._decision("deny", self.policy["can_use_tool_message"])

    async def _ask(self, name: str, data: dict[str, Any], tool_use_id: str | None) -> Any:
        if self.interrupted or self.stdin_closed.is_set() or self.fatal is not None:
            self.hook_denials += 1
            return self._decision("deny", "AutoForge is ending this run")
        call_id = data.get("tool_use_id") or tool_use_id
        tool_input = data.get("tool_input")
        agent = data.get("agent_id")
        self.asks += 1
        request_id = f"ask-{self.asks}"
        record: dict[str, Any] = {
            "type": "ask",
            "request_id": request_id,
            "kind": "question" if name == QUESTION_TOOL else "permission",
            "tool_name": name,
            "tool_use_id": call_id if isinstance(call_id, str) else None,
            "input": tool_input,
            "input_sha256": _digest(tool_input),
            "agent": agent if isinstance(agent, str) and agent else None,
        }
        if self.wire.fits(record) is None:
            record["input"] = None
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.wire.send(record)
            decision = await future
        finally:
            self.pending.pop(request_id, None)
        directive = decision.get("directive")
        if directive == "allow":
            return self._decision("allow", "", decision.get("updated_input"))
        self.hook_denials += 1
        reason = decision.get("guidance") or ""
        if not isinstance(reason, str) or not reason:
            reason = "AutoForge's operator declined this call"
        return self._decision("deny", reason)

    # -- the controller's records ------------------------------------------------
    async def control(self, queue: asyncio.Queue[bytes | None]) -> None:
        while True:
            line = await queue.get()
            if line is None:
                self.stdin_closed.set()
                self._settle_pending("AutoForge is ending this run")
                self.interrupt_requested.set()
                return
            try:
                record = _decode(line, "controller")
                self._take(record)
            except HelperError as exc:
                self.fatal = str(exc)
                self._settle_pending("AutoForge is ending this run")
                self.interrupt_requested.set()

    def _take(self, record: dict[str, Any]) -> None:
        kind = record["type"]
        if kind == "interrupt":
            if set(record) != {"type"}:
                raise HelperError("an interrupt record carries fields")
            self._settle_pending("AutoForge is ending this run")
            self.interrupt_requested.set()
            return
        if kind != "decision":
            raise HelperError(f"the controller sent a {kind} record this helper does not take")
        if set(record) != {"type", "request_id", "directive", "updated_input", "guidance"}:
            raise HelperError("a decision record does not hold exactly protocol 1's fields")
        future = self.pending.get(record["request_id"])
        if future is None or future.done():
            raise HelperError("a decision for a request that is not waiting")
        if record["directive"] not in ("allow", "deny", "fail"):
            raise HelperError("a decision carries an unknown directive")
        if record["updated_input"] is not None and not isinstance(record["updated_input"], dict):
            raise HelperError("a decision's updated input is not an object")
        if record["guidance"] is not None and not isinstance(record["guidance"], str):
            raise HelperError("a decision's guidance is not a string")
        future.set_result(record)

    def _settle_pending(self, reason: str) -> None:
        for future in list(self.pending.values()):
            if not future.done():
                future.set_result({"directive": "deny", "guidance": reason})

    # -- the run --------------------------------------------------------------------
    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        control = asyncio.ensure_future(self.control(self.inbox.attach(loop)))
        try:
            self.client = self.sdk.ClaudeSDKClient(options=self.options())
            await self.client.connect()
            if self.interrupt_requested.is_set():
                self.interrupted = True
            else:
                await self.client.query(self.start["prompt"])
                await self._respond()
            if self.fatal is not None:
                raise HelperError(self.fatal)
            self.wire.send(
                {
                    "type": "end",
                    "outcome": "interrupted" if self.interrupted else "result",
                    "skipped_unknown": self.skipped_unknown,
                    "asks": self.asks,
                    "hook_denials": self.hook_denials,
                    "host_denials": self.host_denials,
                }
            )
            # The controller closes stdin once it has read the outcome (D3.3).
            await self.stdin_closed.wait()
            return 0
        finally:
            control.cancel()
            await self._disconnect()

    async def _respond(self) -> None:
        """Every message up to the query's result; on an interrupt request,
        the turn is interrupted and its interrupted result drained, bounded."""
        stream = self.client.receive_response().__aiter__()
        waiter = asyncio.ensure_future(self.interrupt_requested.wait())
        drain_until: float | None = None
        try:
            while True:
                step = asyncio.ensure_future(stream.__anext__())
                timeout = None
                if drain_until is not None:
                    timeout = max(0.0, drain_until - asyncio.get_running_loop().time())
                done, _ = await asyncio.wait(
                    {step, waiter} if drain_until is None else {step},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if step not in done:
                    if drain_until is not None:
                        step.cancel()
                        return  # the interrupted turn delivered no result in time
                    # An interrupt was requested while the turn ran.
                    self.interrupted = True
                    drain_until = asyncio.get_running_loop().time() + INTERRUPT_DRAIN_SECONDS
                    await self._interrupt()
                    done, _ = await asyncio.wait(
                        {step},
                        timeout=max(0.0, drain_until - asyncio.get_running_loop().time()),
                    )
                    if step not in done:
                        step.cancel()
                        return
                try:
                    message = step.result()
                except StopAsyncIteration:
                    if not self.result_seen and not self.interrupted:
                        raise HelperError("the SDK's response ended without a result") from None
                    return
                self._forward(message)
                if isinstance(message, self.sdk.ResultMessage):
                    self.result_seen = True
                    return
        finally:
            waiter.cancel()

    async def _interrupt(self) -> None:
        try:
            await asyncio.wait_for(self.client.interrupt(), INTERRUPT_DRAIN_SECONDS)
        except Exception as exc:  # the drain below is bounded either way
            print(f"autoforge helper: interrupt failed: {type(exc).__name__}", file=sys.stderr)

    def _forward(self, message: object) -> None:
        record = message_record(self.sdk, message)
        if record is None:
            self.skipped_unknown += 1
            return
        wrapped = {"type": "message", "data": record}
        if self.wire.fits(wrapped) is None:
            if record["type"] == "result":
                raise HelperError("the final result does not fit one record")
            wrapped = {"type": "message", "data": _without_inputs(record)}
            if self.wire.fits(wrapped) is None:
                wrapped = {"type": "message", "data": {"type": record["type"], "oversize": True}}
        self.wire.send(wrapped)

    async def _disconnect(self) -> None:
        if self.client is None:
            return
        try:
            await asyncio.wait_for(self.client.disconnect(), DISCONNECT_SECONDS)
        except Exception as exc:  # the controller's group kill is the backstop
            print(f"autoforge helper: disconnect failed: {type(exc).__name__}", file=sys.stderr)


def main() -> int:
    wire = Wire()
    inbox = Inbox()
    try:
        try:
            import claude_agent_sdk as sdk
        except Exception as exc:
            raise HelperError(
                f"the Claude Agent SDK cannot be imported in this environment "
                f"({type(exc).__name__})"
            ) from None
        wire.send(
            {
                "type": "hello",
                "envelope": ENVELOPE,
                "protocol": PROTOCOL,
                "sdk_version": getattr(sdk, "__version__", None),
                "cli_version": _cli_version(),
                "python_version": platform.python_version(),
            }
        )
        start = check_start(_decode(inbox.first(), "start"))
        wire.limit = start["max_record_bytes"]
        # D4.1: the controller checked the pair exactly from hello.
        os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] = "1"
        return asyncio.run(Session(sdk, wire, inbox, start).run())
    except HelperError as exc:
        wire.failure(str(exc))
        return 1
    except Exception as exc:
        # A bounded account of what failed, never the exception's text: an
        # SDK error can quote the CLI's output.
        wire.failure(f"the helper failed: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
