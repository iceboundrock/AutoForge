"""The Antigravity CLI (``agy``) headless probe: issue #528's evidence.

No test, and not part of the package. Issue #528 is a spike: it decides
whether AutoForge could drive ``agy`` unattended, and builds no adapter.
This module is how its report (``docs/antigravity-cli-spike.md``) was
measured, and how a maintainer measures again on another ``agy`` build::

    python3 -I tests/antigravity_probe.py record --agy ~/.local/bin/agy \\
        --out tests/fixtures/antigravity/1.5.13

``record`` runs every scenario of :data:`SCENARIOS` once and writes one
JSON record per scenario, normalized (:func:`normalize`), into ``--out``.
``tests/test_antigravity_probe.py`` holds the committed records to the
facts the report states, and with ``AUTOFORGE_AGY_PROBE_BIN`` set runs the
scenarios again against that binary and holds the fresh records to the
same facts.

**No model is called and no credential is read.** Each scenario runs
``agy`` once, inside ``bwrap`` with a network namespace of its own (no
route out, only loopback) and a pid namespace of its own (every process
the run left behind is seen, and killed when the namespace ends), with an
empty temporary ``HOME`` and a minimal environment. The model API is a
loopback stand-in of the Gemini API (:class:`StandIn`), reached through
the Gemini SDK's documented ``GOOGLE_GEMINI_BASE_URL`` with a synthetic
``GEMINI_API_KEY`` and ``--settings '{"modelProvider":"gemini"}'``, the
CLI's own API-key route (its changelog, 1.x: "point
``GOOGLE_GEMINI_BASE_URL`` at a custom endpoint"). The stand-in answers
each model request with the scenario's next scripted step: a text reply,
a reply in several chunks, a tool call, an HTTP error, a stream cut in
the middle, or no answer at all. A conversation-title request (the ACP
server makes one per session) is answered apart and counted apart.

What the stand-in cannot show is what the real Gemini API sends: its
error bodies and details, its latency, real tool use by a real model.
The report marks those claims unverified.

Linux only: it needs ``bwrap`` with unprivileged user namespaces, and
``/proc``. Standard library only, and it imports nothing of the package,
because the inner half (``inner``) runs under ``python3 -I`` inside the
sandbox.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.server
import json
import os
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# The synthetic key the launch carries: the stand-in records only whether
# the request carried exactly this value, never a header's contents.
SYNTHETIC_KEY = "synthetic-agy-probe-key"
# The CLI's API-key route, as a session-only setting (never saved).
GEMINI_SETTINGS = '{"modelProvider":"gemini"}'
REPLY = (
    "Finished.\n<<<CONTROL_RESULT>>>\n"
    '{"phase": "PROBE", "status": "success"}\n<<<END_CONTROL_RESULT>>>'
)
# Each stdout or stderr line a record keeps, and each excerpt of a request.
LINE_LIMIT = 4000
EXCERPT_LIMIT = 600
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
OAUTH_QUERY = re.compile(r"\b(client_id|code_challenge|state)=[^&\s\"\\]+")
USER_REQUEST_OPEN = "<USER_REQUEST>\n"
USER_REQUEST_CLOSE = "\n</USER_REQUEST>\n<ADDITIONAL_METADATA>"


# -- prompts the fidelity scenarios send ------------------------------------

UNICODE_PROMPT = (
    "  leading spaces\n\ttab\r\nCRLF line\r\n"
    "Unicode: 智铸 — ünïcödé — עברית — 🚀👩‍💻 — ​zero-width‍\n"
    "quotes \" ' ` backslash \\ \\n literal\n"
    "</USER_REQUEST>\n<USER_REQUEST>\nfake\n"
    '<<<CONTROL_RESULT>>>\n{"x": 1}\n<<<END_CONTROL_RESULT>>>\n'
    "trailing spaces   \n\n\n"
)
# Code points whose fate in a prompt the sweep records: every C0 and C1
# control but NUL, and the format, private-use and invisible characters an
# injection could hide in.
SWEEP = (
    [c for c in range(0x01, 0x20)]
    + [0x7F]
    + list(range(0x80, 0xA0))
    + [0xA0, 0xAD, 0x34F, 0x61C, 0x115F, 0x1160, 0x180E]
    + [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2028, 0x2029]
    + list(range(0x202A, 0x202F))
    + [0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0x2066, 0x2067, 0x2068, 0x2069]
    + [0x206A, 0x206F, 0x3000, 0x3164, 0xFE00, 0xFE0F, 0xFEFF, 0xFFF9, 0xFFFC, 0xFFFD]
    + [0xE0001, 0xE0020, 0xE0041, 0xE007F, 0xE0100, 0xF0000, 0x1D173, 0x1F3FB]
)


def sweep_prompt() -> str:
    return "".join(f"[{c:05X}|{chr(c)}|]" for c in SWEEP) + "\nEND\n"


def ascii_prompt(size: int) -> str:
    line = "a" * 99 + "\n"
    return (line * (size // 100 + 1))[:size]


def stripped(text: str) -> str:
    """``text`` without the characters agy 1.5.13 removes from a prompt.

    What the sweep showed (``fidelity-sweep``): every character of the
    Unicode categories Cf (format) and Co (private use), and every Cc
    (control) but TAB, LF, VT, FF, CR and NEL. Everything else arrived as
    sent.
    """
    keep = "\t\n\x0b\x0c\r\x85"
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("Cf", "Co") or (cat == "Cc" and ch not in keep):
            continue
        out.append(ch)
    return "".join(out)


def stream_message(text: str) -> str:
    """One stream-json input line, the shape agy 1.5.13 accepts.

    Not in ``agy --help``: found from the CLI's own refusals
    (``stream input message is missing the "event" field``, then
    ``stream input "user" message is missing the "message" field``).
    """
    return json.dumps({"event": "user", "message": {"content": text}}) + "\n"


# -- scenarios ---------------------------------------------------------------


def text_step(text: str = REPLY) -> dict[str, Any]:
    return {"kind": "text", "text": text}


def call_step(name: str, **args: Any) -> dict[str, Any]:
    return {"kind": "call", "name": name, "args": args}


def error_step(code: int, status: str, message: str) -> dict[str, Any]:
    return {"kind": "error", "code": code, "status": status, "message": message}


@dataclass
class Scenario:
    """One run of ``agy``: its argv, its input, and the stand-in's script.

    ``argv`` may name ``{work}`` (the workspace) and ``{root}`` (the
    probe's root). ``stdin`` is the prompt bytes on stdin (``b""`` an empty
    stdin, ``None`` ``/dev/null``); ``feed`` instead writes on a pipe kept
    open, one item at a time (``{"data"}``, ``{"delay"}``, ``{"close"}``,
    and for ACP ``{"acp_prompt"}`` and ``{"acp_permission"}``).
    ``workspace`` names a setup in :data:`WORKSPACES`; ``share`` names a
    home and workspace scenarios of one group reuse, in order.
    """

    name: str
    argv: list[str]
    steps: list[dict[str, Any]] = field(default_factory=lambda: [text_step()])
    stdin: bytes | None = None
    feed: list[dict[str, Any]] | None = None
    timeout: float = 30.0
    signal_after: float | None = None
    signal_name: str = "SIGINT"
    key: bool = True
    base_url: bool = True
    workspace: str = ""
    share: str = ""
    group: str = ""
    sent_prompt: str | None = None  # what fidelity is measured against
    note: str = ""


def p_run(*extra: str, fmt: str = "stream-json", prompt: str = "do it") -> list[str]:
    return ["-p", prompt, "--output-format", fmt, "--settings", GEMINI_SETTINGS, *extra]


def stdin_run(*extra: str, fmt: str = "stream-json") -> list[str]:
    return ["--output-format", fmt, "--settings", GEMINI_SETTINGS, *extra]


def sj_run(*extra: str) -> list[str]:
    return [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--settings",
        GEMINI_SETTINGS,
        *extra,
    ]


SKIP = "--dangerously-skip-permissions"
CMD_IN_WORKSPACE = call_step(
    "run_command",
    CommandLine="echo hi > in-workspace.txt",
    toolSummary="Write file",
    WaitMsBeforeAsync=5000,
)


def _acp_feed(*tail: dict[str, Any]) -> list[dict[str, Any]]:
    def rq(i: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message = {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
        return {"data": json.dumps(message) + "\n"}

    caps = {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
    return [
        rq(1, "initialize", {"protocolVersion": 1, "clientCapabilities": caps}),
        {"delay": 0.5, **rq(2, "authenticate", {"methodId": "gemini-api-key"})},
        {"delay": 0.5, **rq(3, "session/new", {"cwd": "{work}", "mcpServers": []})},
        {"delay": 0.5, "acp_prompt": "do it", "id": 4},
        *tail,
    ]


def scenarios() -> list[Scenario]:
    s: list[Scenario] = []
    add = s.append
    # discovery
    add(Scenario("version", ["--version"], steps=[]))
    add(Scenario("help", ["--help"], steps=[]))
    add(Scenario("settings-catalogue", p_run(fmt="json", prompt="/config"), steps=[]))
    add(Scenario("settings-unknown-key", ["-p", "hi", "--settings", '{"noSuchKey": true}']))
    add(Scenario("acp-help", ["help", "acp"], steps=[]))
    add(Scenario("acp-flag-in-print-mode", p_run("--gemini-dir=/nonexistent", fmt="json")))
    add(Scenario("model-list", ["models"], steps=[], workspace="persisted-gemini"))
    # the success path and the three output formats
    add(Scenario("success-stream-json", p_run()))
    add(Scenario("success-json", p_run(fmt="json")))
    add(Scenario("success-text", p_run(fmt="text")))
    add(
        Scenario(
            "success-chunks",
            p_run(),
            steps=[
                {
                    "kind": "chunks",
                    "texts": [
                        "Part one, ",
                        "part two.\n<<<CONTROL_RESULT>>>\n",
                        '{"status": "success"}\n<<<END_CONTROL_RESULT>>>',
                    ],
                    "delay": 0.4,
                }
            ],
        )
    )
    # where the prompt travels
    add(Scenario("stdin-text", stdin_run(), stdin=b"prompt on stdin\n"))
    add(
        Scenario(
            "stdin-stream-json", sj_run(), stdin=stream_message("prompt as one message").encode()
        )
    )
    add(Scenario("stdin-and-argv", p_run(prompt="argv prompt"), stdin=b"stdin prompt\n"))
    add(Scenario("stdin-empty", stdin_run(), stdin=b""))
    add(Scenario("stdin-whitespace", stdin_run(), stdin=b"   \n\n"))
    add(Scenario("stdin-stream-json-empty", sj_run(), stdin=b""))
    add(
        Scenario(
            "argv-leading-dash", p_run(prompt="-n leading dash"), sent_prompt="-n leading dash"
        )
    )
    add(
        Scenario(
            "argv-flag-name",
            ["-p=--version", "--output-format", "json", "--settings", GEMINI_SETTINGS],
        )
    )
    add(Scenario("argv-after-double-dash", ["--output-format", "json", "--", "-p", "hi"]))
    add(Scenario("fidelity-unicode-argv", p_run(prompt=UNICODE_PROMPT), sent_prompt=UNICODE_PROMPT))
    add(
        Scenario(
            "fidelity-unicode-stdin",
            stdin_run(),
            stdin=UNICODE_PROMPT.encode(),
            sent_prompt=UNICODE_PROMPT,
        )
    )
    add(
        Scenario(
            "fidelity-unicode-stream-json",
            sj_run(),
            stdin=stream_message(UNICODE_PROMPT).encode(),
            sent_prompt=UNICODE_PROMPT,
        )
    )
    add(
        Scenario(
            "fidelity-sweep", stdin_run(), stdin=sweep_prompt().encode(), sent_prompt=sweep_prompt()
        )
    )
    add(
        Scenario(
            "fidelity-invalid-utf8",
            stdin_run(),
            stdin=b"valid start \xff\xfe invalid \xc3( end\n",
            sent_prompt=b"valid start \xff\xfe invalid \xc3( end\n".decode("utf-8", "replace"),
        )
    )
    for size in (190_000, 196_608, 262_144):
        text = ascii_prompt(size)
        add(Scenario(f"fidelity-size-{size}", stdin_run(), stdin=text.encode(), sent_prompt=text))
    big = ascii_prompt(262_144)
    add(
        Scenario(
            "fidelity-size-262144-stream-json",
            sj_run(),
            stdin=stream_message(big).encode(),
            sent_prompt=big,
        )
    )
    # tools, permissions and questions
    add(Scenario("deny-command", p_run(), steps=[CMD_IN_WORKSPACE, text_step()]))
    add(Scenario("deny-command-json", p_run(fmt="json"), steps=[CMD_IN_WORKSPACE, text_step()]))
    add(Scenario("allow-command-skip", p_run(SKIP), steps=[CMD_IN_WORKSPACE, text_step()]))
    add(
        Scenario(
            "write-outside-default",
            p_run(),
            steps=[
                call_step("write_file", Path="{work}/../outside-written-by-agent", Content="x"),
                text_step(),
            ],
        )
    )
    add(
        Scenario(
            "ask-question",
            p_run(SKIP),
            steps=[
                call_step(
                    "ask_question",
                    questions=[
                        {"question": "Which approach?", "options": ["(Recommended) A", "B"]}
                    ],
                ),
                text_step(),
            ],
        )
    )
    # background work and the process tree
    add(
        Scenario(
            "background-task",
            p_run(SKIP),
            timeout=60,
            steps=[
                call_step(
                    "run_command",
                    CommandLine="sleep 15; echo bg-done",
                    toolSummary="Background sleep",
                    WaitMsBeforeAsync=500,
                ),
                text_step(),
            ],
        )
    )
    add(
        Scenario(
            "daemon-task",
            p_run(SKIP),
            steps=[
                call_step(
                    "run_command",
                    CommandLine="sleep 300",
                    toolSummary="Daemon sleep",
                    WaitMsBeforeAsync=500,
                    IsDaemon=True,
                ),
                text_step(),
            ],
        )
    )
    add(
        Scenario(
            "detached-child",
            p_run(SKIP),
            steps=[
                call_step(
                    "run_command",
                    CommandLine="setsid -f sleep 300 >/dev/null 2>&1; echo started",
                    toolSummary="Detached sleep",
                    WaitMsBeforeAsync=5000,
                ),
                text_step(),
            ],
        )
    )
    add(
        Scenario(
            "timer",
            p_run(SKIP),
            timeout=60,
            steps=[call_step("schedule", Prompt="timer fired", DurationSeconds=8), text_step()],
        )
    )
    add(
        Scenario(
            "sigterm-background-task",
            p_run(SKIP),
            timeout=60,
            signal_after=3,
            signal_name="SIGTERM",
            steps=[
                call_step(
                    "run_command",
                    CommandLine="sleep 15; echo bg-done",
                    toolSummary="Background sleep",
                    WaitMsBeforeAsync=500,
                ),
                text_step(),
            ],
        )
    )
    add(
        Scenario(
            "print-timeout-background",
            p_run(SKIP, "--print-timeout", "5s"),
            timeout=60,
            steps=[
                call_step(
                    "run_command",
                    CommandLine="sleep 15; echo bg-done",
                    toolSummary="Background sleep",
                    WaitMsBeforeAsync=500,
                ),
                text_step(),
            ],
        )
    )
    # a model request that never answers
    hang = [{"kind": "hang", "seconds": 3600}]
    add(
        Scenario(
            "hang-no-limit",
            p_run(SKIP),
            steps=hang,
            timeout=120,
            note="the probe kills it at its timeout",
        )
    )
    add(
        Scenario(
            "hang-print-timeout", p_run(SKIP, "--print-timeout", "10s"), steps=hang, timeout=60
        )
    )
    add(Scenario("sigint-during-request", p_run(SKIP), steps=hang, timeout=60, signal_after=3))
    add(
        Scenario(
            "sigterm-during-request",
            p_run(SKIP),
            steps=hang,
            timeout=60,
            signal_after=3,
            signal_name="SIGTERM",
        )
    )
    add(
        Scenario(
            "sigint-during-request-json",
            p_run(SKIP, fmt="json"),
            steps=hang,
            timeout=60,
            signal_after=3,
        )
    )
    # the model API's failures
    for code, status, message in (
        (400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
        (401, "UNAUTHENTICATED", "Request had invalid authentication credentials."),
        (403, "PERMISSION_DENIED", "The caller does not have permission"),
        (404, "NOT_FOUND", "models/x is not found for API version v1beta"),
        (429, "RESOURCE_EXHAUSTED", "Resource has been exhausted (e.g. check quota)."),
        (500, "INTERNAL", "An internal error has occurred."),
        (503, "UNAVAILABLE", "The model is overloaded. Please try again later."),
    ):
        add(
            Scenario(
                f"api-{code}", p_run(SKIP), steps=[error_step(code, status, message)], timeout=240
            )
        )
    add(
        Scenario(
            "api-503-then-success",
            p_run(SKIP),
            timeout=60,
            steps=[error_step(503, "UNAVAILABLE", "overloaded"), text_step()],
        )
    )
    add(
        Scenario(
            "cut-stream-then-success",
            p_run(SKIP),
            timeout=60,
            steps=[{"kind": "partial", "text": "partial "}, text_step()],
        )
    )
    add(
        Scenario(
            "cut-stream-always",
            p_run(SKIP),
            timeout=240,
            steps=[{"kind": "partial", "text": "partial "}],
        )
    )
    # credentials
    add(Scenario("api-key-route-without-key", p_run(fmt="json"), key=False))
    add(
        Scenario(
            "no-login-stdin-prompt",
            ["--output-format", "json"],
            key=False,
            base_url=False,
            stdin=b"hello\n",
        )
    )
    add(
        Scenario(
            "no-login-argv-prompt",
            ["-p", "hello", "--output-format", "stream-json"],
            key=False,
            base_url=False,
            timeout=90,
        )
    )
    # model and effort selection
    add(Scenario("model-default", p_run(fmt="json")))
    add(Scenario("model-with-effort-suffix", p_run("--model", "gemini-4-argon-medium", fmt="json")))
    add(
        Scenario(
            "model-base-and-effort",
            p_run("--model", "gemini-4-argon", "--effort", "high", fmt="json"),
        )
    )
    add(
        Scenario(
            "model-effort-conflict",
            p_run("--model", "gemini-4-argon-low", "--effort", "high", fmt="json"),
        )
    )
    add(
        Scenario(
            "model-effort-unsupported",
            p_run("--model", "gemini-4-argon", "--effort", "max", fmt="json"),
        )
    )
    add(Scenario("model-unknown", p_run("--model", "no-such-model", fmt="json")))
    # stream-json input sessions
    two = [
        {"data": stream_message("first turn")},
        {"delay": 2, "data": stream_message("second")},
        {"delay": 2, "close": True},
    ]
    add(Scenario("stream-input-two-turns", sj_run(SKIP), feed=two, timeout=20))
    bad = [
        {"data": stream_message("first turn")},
        {"delay": 2, "data": "garbage line\n"},
        {"delay": 2, "close": True},
    ]
    add(Scenario("stream-input-malformed-after-turn", sj_run(SKIP), feed=bad, timeout=20))
    add(
        Scenario(
            "stream-input-left-open",
            sj_run(SKIP),
            feed=[{"data": stream_message("only")}],
            timeout=15,
            note="the probe kills it at its timeout",
        )
    )
    add(
        Scenario(
            "stream-input-cut",
            sj_run(SKIP),
            timeout=20,
            feed=[{"data": stream_message("first turn")[:20]}, {"delay": 1, "close": True}],
        )
    )
    claude_shaped = (
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
    )
    add(Scenario("stream-input-wrong-shape", sj_run(SKIP), stdin=claude_shaped.encode()))
    # what a launch inherits
    add(Scenario("repository-customizations", p_run(fmt="json"), workspace="customized"))
    # the settings that might have gated them: neither does
    untrusted = '{"modelProvider":"gemini","trustedWorkspaces":[]}'
    add(
        Scenario(
            "repository-customizations-untrusted",
            ["-p", "do it", "--output-format", "json", "--settings", untrusted],
            workspace="customized",
        )
    )
    add(
        Scenario(
            "repository-customizations-sandbox",
            p_run("--sandbox", fmt="json"),
            workspace="customized",
        )
    )
    add(
        Scenario(
            "fresh-first",
            p_run(SKIP, fmt="json", prompt="first MARKER-ALPHA-123"),
            share="fresh",
            group="fresh",
        )
    )
    add(
        Scenario(
            "fresh-second", p_run(SKIP, fmt="json", prompt="second"), share="fresh", group="fresh"
        )
    )
    add(
        Scenario(
            "continue-latest",
            p_run(SKIP, "-c", fmt="json", prompt="continued"),
            share="fresh",
            group="fresh",
        )
    )
    slow = [{"kind": "chunks", "texts": ["slow ", "reply"], "delay": 1.0}]
    for i in (1, 2, 3):
        add(
            Scenario(
                f"concurrent-{i}",
                p_run(SKIP, fmt="json", prompt=f"concurrent prompt {i}"),
                steps=slow,
                share="concurrent",
                group="concurrent",
            )
        )
    # the ACP stdio server
    add(Scenario("acp-text", ["acp"], feed=_acp_feed({"delay": 3, "close": True}), timeout=20))
    add(
        Scenario(
            "acp-permission-rejected",
            ["acp"],
            steps=[CMD_IN_WORKSPACE, text_step()],
            feed=_acp_feed(
                {"delay": 1, "acp_permission": "reject_once"}, {"delay": 3, "close": True}
            ),
            timeout=20,
        )
    )
    return s


SCENARIOS = scenarios()


# -- workspaces --------------------------------------------------------------


def _customized(work: Path, home: Path) -> None:
    """A repository that ships agy customizations: rules, hooks, an MCP server."""
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "AGENTS.md").write_text("AGENTS-RULE-MARKER\n")
    (work / "GEMINI.md").write_text("GEMINI-RULE-MARKER\n")
    agents = work / ".agents"
    (agents / "rules").mkdir(parents=True)
    (agents / "rules" / "r.md").write_text("---\ntrigger: always_on\n---\nDOTAGENTS-RULE-MARKER\n")
    key_check = 'if [ -n "$GEMINI_API_KEY" ]; then echo key-visible; else echo key-absent; fi'
    hooks = {
        "probe": {
            "SessionStart": [
                {"type": "command", "command": f"{key_check} > {work}/hook-session-start"}
            ],
            "PreInvocation": [{"type": "command", "command": f"touch {work}/hook-pre-invocation"}],
            "Stop": [{"type": "command", "command": f"touch {work}/hook-stop"}],
        }
    }
    (agents / "hooks.json").write_text(json.dumps(hooks))
    mcp = {"mcpServers": {"probe": {"command": "sh", "args": ["-c", f"touch {work}/mcp-started"]}}}
    (agents / "mcp_config.json").write_text(json.dumps(mcp))


def _persisted_gemini(work: Path, home: Path) -> None:
    """A home whose saved settings name the API-key route (``models`` reads no --settings)."""
    target = home / ".gemini" / "antigravity-cli"
    target.mkdir(parents=True, exist_ok=True)
    (target / "settings.json").write_text(GEMINI_SETTINGS)


WORKSPACES: dict[str, Callable[[Path, Path], None]] = {
    "customized": _customized,
    "persisted-gemini": _persisted_gemini,
}
MARKERS = ("hook-session-start", "hook-pre-invocation", "hook-stop", "mcp-started")


# -- the stand-in of the Gemini API -------------------------------------------


class StandIn:
    """The scripted answers and the record of every request."""

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = steps
        self.requests: list[dict[str, Any]] = []
        self.titles = 0
        self.others: list[str] = []
        self.lock = threading.Lock()

    def next_step(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            index = len(self.requests)
            self.requests.append(record)
        if not self.steps:
            return text_step()
        return self.steps[min(index, len(self.steps) - 1)]


def _sse(handler: http.server.BaseHTTPRequestHandler, chunks: Iterable[bytes]) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Connection", "close")
    handler.end_headers()
    for chunk in chunks:
        handler.wfile.write(chunk)
        handler.wfile.flush()
    handler.close_connection = True


def _candidate(parts: list[dict[str, Any]], finish: str | None = "STOP") -> bytes:
    cand: dict[str, Any] = {"content": {"role": "model", "parts": parts}, "index": 0}
    body: dict[str, Any] = {"candidates": [cand], "modelVersion": "stand-in"}
    if finish:
        cand["finishReason"] = finish
        body["usageMetadata"] = {
            "promptTokenCount": 11,
            "candidatesTokenCount": 7,
            "totalTokenCount": 18,
        }
    return ("data: " + json.dumps(body, ensure_ascii=False) + "\r\n\r\n").encode()


def make_handler(stand_in: StandIn) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # quiet
            pass

        def do_GET(self) -> None:  # noqa: N802
            stand_in.others.append("GET " + self.path.split("?")[0])
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(size) if size else b""
            path = self.path.split("?")[0]
            if "GenerateContent" not in path and "generateContent" not in path:
                stand_in.others.append("POST " + path)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            system = "".join(
                p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts", [])
            )
            if "conversation title generator" in system:
                with stand_in.lock:
                    stand_in.titles += 1
                _sse(self, [_candidate([{"text": "Stand-in title"}])])
                return
            record = {
                "model_path": path,
                "key": "synthetic"
                if self.headers.get("x-goog-api-key") == SYNTHETIC_KEY
                else ("other" if self.headers.get("x-goog-api-key") else "absent"),
                "body": body,
            }
            self.answer(stand_in.next_step(record))

        def answer(self, step: dict[str, Any]) -> None:
            kind = step["kind"]
            if kind == "hang":
                time.sleep(step.get("seconds", 3600))
                return
            if kind == "error":
                payload = json.dumps(
                    {
                        "error": {
                            "code": step["code"],
                            "message": step["message"],
                            "status": step["status"],
                        }
                    }
                ).encode()
                self.send_response(step["code"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if kind == "partial":
                first = _candidate([{"text": step.get("text", "partial ")}], finish=None)
                _sse(self, [first, b'data: {"candidates": [{"cont'])
                return
            if kind == "chunks":
                texts = step["texts"]

                def chunks() -> Iterable[bytes]:
                    for i, text in enumerate(texts):
                        yield _candidate([{"text": text}], "STOP" if i == len(texts) - 1 else None)
                        time.sleep(step.get("delay", 0.3))

                _sse(self, chunks())
                return
            if kind == "call":
                parts = [{"functionCall": {"name": step["name"], "args": step["args"]}}]
            else:
                parts = [{"text": step["text"]}]
            _sse(self, [_candidate(parts)])

    return Handler


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# -- the inner half: one run inside the sandbox ------------------------------


def _processes(exclude: set[int]) -> list[dict[str, Any]]:
    rows = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in exclude:
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        rest = stat[stat.rindex(")") + 2 :].split()
        rows.append(
            {
                "pid": int(entry),
                "ppid": int(rest[1]),
                "pgid": int(rest[2]),
                "sid": int(rest[3]),
                "cmd": " ".join(c.decode("utf-8", "replace") for c in cmdline if c)[:120],
            }
        )
    return rows


def _feeder(
    proc: subprocess.Popen[bytes], plan: list[dict[str, Any]], stdout: list[list[Any]], work: str
) -> None:
    def wait_for(pred: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        for _ in range(200):
            for _t, line in list(stdout):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and pred(obj):
                    return obj
            time.sleep(0.05)
        return None

    assert proc.stdin is not None
    for item in plan:
        time.sleep(item.get("delay", 0))
        if item.get("close"):
            proc.stdin.close()
            return
        data = item.get("data")
        if "acp_prompt" in item:
            new = wait_for(
                lambda o: isinstance(o.get("result"), dict) and "sessionId" in o["result"]
            )
            sid = (new or {}).get("result", {}).get("sessionId", "")
            prompt = [{"type": "text", "text": item["acp_prompt"]}]
            message = {
                "jsonrpc": "2.0",
                "id": item["id"],
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": prompt},
            }
            data = json.dumps(message) + "\n"
        elif "acp_permission" in item:
            asked = wait_for(lambda o: o.get("method") == "session/request_permission")
            if asked is None:
                continue
            options = asked["params"].get("options", [])
            pick = next(
                (o["optionId"] for o in options if o.get("kind") == item["acp_permission"]), None
            )
            outcome = (
                {"outcome": "selected", "optionId": pick} if pick else {"outcome": "cancelled"}
            )
            data = json.dumps({"jsonrpc": "2.0", "id": asked["id"], "result": {"outcome": outcome}})
            data += "\n"
        if data is None:
            continue
        data = data.replace("{work}", work)
        try:
            proc.stdin.write(data.encode("utf-8"))
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return


def inner(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text())
    sc = Scenario(**{k: v for k, v in spec["scenario"].items() if k != "stdin"})
    stdin_bytes = bytes.fromhex(spec["stdin_hex"]) if spec["stdin_hex"] is not None else None
    stand_in = StandIn(sc.steps)
    server = _Server(("127.0.0.1", 0), make_handler(stand_in))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home, work, root = spec["home"], spec["work"], spec["root"]
    env = {
        "HOME": home,
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "XDG_CONFIG_HOME": home + "/.config",
        "XDG_CACHE_HOME": home + "/.cache",
        "XDG_DATA_HOME": home + "/.local/share",
        "XDG_STATE_HOME": home + "/.local/state",
    }
    if sc.key:
        env["GEMINI_API_KEY"] = SYNTHETIC_KEY
    if sc.base_url:
        env["GOOGLE_GEMINI_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
    argv = [a.replace("{work}", work).replace("{root}", root) for a in sc.argv]
    steps = json.loads(json.dumps(sc.steps).replace("{work}", work).replace("{root}", root))
    stand_in.steps = steps
    stdin: Any = subprocess.DEVNULL
    if sc.feed is not None or stdin_bytes is not None:
        stdin = subprocess.PIPE
    started = time.monotonic()
    proc = subprocess.Popen(
        [spec["agy"], *argv],
        cwd=work,
        env=env,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    lines: dict[str, list[list[Any]]] = {"stdout": [], "stderr": []}

    def reader(name: str) -> None:
        stream = getattr(proc, name)
        for line in iter(stream.readline, b""):
            lines[name].append(
                [round(time.monotonic() - started, 2), line.decode("utf-8", "replace")]
            )

    readers = [threading.Thread(target=reader, args=(n,), daemon=True) for n in lines]
    for t in readers:
        t.start()
    if sc.feed is not None:
        threading.Thread(
            target=_feeder, args=(proc, sc.feed, lines["stdout"], work), daemon=True
        ).start()
    elif stdin_bytes is not None:

        def write() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(stdin_bytes)
                proc.stdin.close()
            except BrokenPipeError:
                pass

        threading.Thread(target=write, daemon=True).start()
    me = {1, os.getpid()}
    during: list[dict[str, Any]] | None = None
    signalled = None
    if sc.signal_after is not None:
        try:
            proc.wait(timeout=sc.signal_after)
        except subprocess.TimeoutExpired:
            during = _processes(me)
            proc.send_signal(getattr(signal, sc.signal_name))
            signalled = sc.signal_name
    killed = False
    try:
        proc.wait(timeout=max(0.1, started + sc.timeout - time.monotonic()))
    except subprocess.TimeoutExpired:
        killed = True
        during = during or _processes(me)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    seconds = round(time.monotonic() - started, 1)
    for t in readers:
        t.join(timeout=5)
    time.sleep(1.0)
    left = [p for p in _processes(me) if p["pid"] != proc.pid]
    server.shutdown()
    record = {
        "argv": argv,
        "exit": proc.returncode,
        "killed_by_probe": killed,
        "signalled": signalled,
        "seconds": seconds,
        "stdout": lines["stdout"],
        "stderr": lines["stderr"],
        "requests": [_request_summary(r) for r in stand_in.requests],
        "title_requests": stand_in.titles,
        "other_requests": stand_in.others,
        "processes_during": [p["cmd"] for p in during or []],
        "processes_left": [
            {"cmd": p["cmd"], "own_session": p["sid"] == p["pid"], "reparented": p["ppid"] == 1}
            for p in left
        ],
        "markers": {
            m: (Path(work, m).read_text().strip() or "present")
            for m in MARKERS
            if Path(work, m).exists()
        },
        "outside_file_written": Path(work).parent.joinpath("outside-written-by-agent").exists(),
        "facts": _facts(sc, stand_in.requests, root, home),
        "home_files": _home_files(Path(home)),
    }
    Path(spec["result"]).write_text(json.dumps(record, ensure_ascii=False))
    for p in left:
        try:
            os.kill(p["pid"], signal.SIGKILL)
        except OSError:
            pass
    return 0


def _user_text(request: dict[str, Any]) -> str:
    contents = request["body"].get("contents") or [{}]
    return "".join(p.get("text", "") for p in contents[0].get("parts", []))


def _request_summary(request: dict[str, Any]) -> dict[str, Any]:
    body = request["body"]
    contents = body.get("contents") or []
    last = contents[-1] if contents else {}
    tool_results = [
        json.dumps(p["functionResponse"].get("response"), ensure_ascii=False)[:EXCERPT_LIMIT]
        for c in contents
        for p in c.get("parts", [])
        if "functionResponse" in p
    ]
    return {
        "model_path": request["model_path"],
        "key": request["key"],
        "thinking": (body.get("generationConfig") or {}).get("thinkingConfig"),
        "tools": [
            d.get("name")
            for t in body.get("tools") or []
            for d in t.get("functionDeclarations", [])
        ],
        "contents": len(contents),
        "first_user_text": _user_text(request)[:EXCERPT_LIMIT],
        "last_role": last.get("role"),
        "tool_results": tool_results,
    }


def _home_files(home: Path) -> list[str]:
    """What the run left in its home, ids spelt <id>; not the bundled skills."""
    found = set()
    for path in home.rglob("*"):
        rel = path.relative_to(home).as_posix()
        if path.is_file() and "/builtin/" not in f"/{rel}/":
            found.add(UUID.sub("<id>", re.sub(r"\d{8}_\d{6}", "<time>", rel)))
    return sorted(found)[:80]


def _facts(sc: Scenario, requests: list[dict[str, Any]], root: str, home: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if requests:
        system = "".join(
            p.get("text", "")
            for p in (requests[0]["body"].get("systemInstruction") or {}).get("parts", [])
        )
        normal = UUID.sub("UUID", system).replace(root, "<root>")
        facts["system_prompt_sha256"] = hashlib.sha256(normal.encode()).hexdigest()
        facts["system_prompt_chars"] = len(system)
        joined = system + json.dumps(requests[0]["body"].get("contents"), ensure_ascii=False)
        facts["names_app_data_dir"] = f"{home}/.gemini/antigravity-cli" in system
        facts["context_markers"] = [
            m
            for m in (
                "AGENTS-RULE-MARKER",
                "GEMINI-RULE-MARKER",
                "DOTAGENTS-RULE-MARKER",
                "MARKER-ALPHA-123",
            )
            if m in joined
        ]
    if sc.sent_prompt is not None and requests:
        text = _user_text(requests[0])
        close = text.rfind(USER_REQUEST_CLOSE)
        # A cut message keeps its head only: the closing tag and the
        # metadata after it go with the rest (fidelity-size-*).
        got = (
            text[len(USER_REQUEST_OPEN) : close if close >= 0 else len(text)]
            if text.startswith(USER_REQUEST_OPEN)
            else None
        )
        want = sc.sent_prompt
        cut = re.search(r"<truncated (\d+) bytes>$", got or "")
        facts["fidelity"] = {
            "wrapped": got is not None,
            "closing_tag_kept": close >= 0,
            "message_bytes": len(text.encode()),
            "sent_bytes": len(want.encode()),
            "received_bytes": len(got.encode()) if got is not None else None,
            "identical": got == want,
            "identical_after_strip": got == stripped(want),
            "truncated_bytes": int(cut.group(1)) if cut else None,
            "kept_before_cut_bytes": len(got[: cut.start()].encode()) if cut and got else None,
        }
        if sc.name == "fidelity-sweep" and got is not None:
            dropped, kept = [], []
            for c in SWEEP:
                m = re.search(rf"\[{c:05X}\|(.*?)\|\]", got, re.S)
                (kept if m and m.group(1) == chr(c) else dropped).append(f"U+{c:04X}")
            facts["sweep"] = {"dropped": dropped, "kept": kept}
    return facts


# -- a prototype of the reducer an adapter would need -------------------------
#
# Not the adapter: what the report's suggested contract (its section on the
# adapter) says a one-shot ``--output-format stream-json`` run means, written
# down so the committed records can be held to it. A G1 adapter would put
# its own, reviewed, in the provider layer (as claude_stream.py is).

EVENTS = ("init", "step_update", "result")


@dataclass
class Reduction:
    """What one run's stdout and stderr say, record by record."""

    records: int = 0
    malformed: int = 0  # a line that is not a JSON object with a known event
    init: bool = False
    results: list[dict[str, Any]] = field(default_factory=list)
    result_last: bool = True
    # each agent_response step's text, by step index, from its deltas
    agent_text: dict[int, str] = field(default_factory=dict)
    # each tool step's last state, by step index
    tools: dict[int, str] = field(default_factory=dict)
    agy_error: dict[str, Any] | None = None


def reduce_stream(stdout: Iterable[str], stderr: Iterable[str]) -> Reduction:
    red = Reduction()
    for line in stdout:
        if not line.strip():
            continue
        red.records += 1
        try:
            obj = json.loads(line)
        except ValueError:
            red.malformed += 1
            continue
        event = obj.get("event") if isinstance(obj, dict) else None
        if event not in EVENTS:
            red.malformed += 1
            continue
        if red.results:
            red.result_last = False  # anything after a result
        if event == "init":
            red.init = True
        elif event == "result":
            red.results.append(obj.get("result") or {})
            red.result_last = True
        else:
            step = obj.get("step_update") or {}
            index = step.get("step_index", -1)
            if step.get("step_type") == "agent_response":
                red.agent_text[index] = red.agent_text.get(index, "") + step.get("text_delta", "")
            elif step.get("step_type") == "tool":
                red.tools[index] = step.get("state", "")
    for line in stderr:
        if line.startswith("AGY_ERROR: "):
            try:
                red.agy_error = json.loads(line[len("AGY_ERROR: ") :])
            except ValueError:
                red.agy_error = {"unparsed": True}
    return red


@dataclass(frozen=True)
class Outcome:
    """``kind`` is ``completed`` only for a run whose reply may be read."""

    kind: str
    reason: str = ""
    reply: str = ""


def classify(red: Reduction, exit_code: int) -> Outcome:
    """A one-shot run's outcome, by the suggested contract; never from exit 0 alone."""
    if red.malformed:
        return Outcome("protocol", f"{red.malformed} line(s) not a known event")
    if not red.results:
        return Outcome("no_result", "no result record" + (" after init" if red.init else ""))
    if len(red.results) > 1:
        return Outcome("protocol", f"{len(red.results)} result records in a one-shot run")
    if not red.result_last:
        return Outcome("protocol", "a record after the result")
    result = red.results[0]
    reason = result.get("finished_reason", "")
    if reason == "interrupted":
        return Outcome("interrupted", "finished_reason interrupted")
    if reason == "print_timeout":
        return Outcome("print_timeout", "finished_reason print_timeout")
    if result.get("status") != "SUCCESS" or reason or exit_code != 0:
        err = red.agy_error or {}
        code = err.get("error_code")
        if err.get("code_kind") == "http" and code == 429:
            return Outcome("usage_limit", "model API 429")
        if (
            err.get("code_kind") == "http"
            and isinstance(code, int)
            and (code == 408 or code >= 500)
        ):
            return Outcome("transient", f"model API {code}")
        return Outcome("error", f"status {result.get('status')}, exit {exit_code}")
    denied = result.get("denied_actions") or []
    if denied:
        names = ", ".join(sorted({str(d.get("action")) for d in denied}))
        return Outcome("denied", f"refused without a prompt: {names}")
    if any(state not in ("DONE", "ERROR") for state in red.tools.values()):
        return Outcome("protocol", "a tool step never ended")
    if not red.agent_text:
        return Outcome("empty_reply", "no agent response")
    reply = red.agent_text[max(red.agent_text)]
    if not reply.strip():
        return Outcome("empty_reply", "the last agent response is empty")
    return Outcome("completed", reply=reply)


def outcome_of(record: dict[str, Any]) -> Outcome:
    """:func:`classify` of a committed or fresh record."""
    red = reduce_stream(
        (x[1] for x in record["stdout"]), (x[1].rstrip("\n") for x in record["stderr"])
    )
    return classify(red, record["exit"])


# -- the outer half: sandbox, normalize, write ---------------------------------


Shared = dict[str, tuple[Path, Path]]


def prepare(sc: Scenario, root: Path, shared: Shared) -> tuple[Path, Path]:
    """The scenario's home and workspace: its own, or its group's."""
    if sc.share and sc.share in shared:
        return shared[sc.share]
    base = Path(tempfile.mkdtemp(prefix=f"{sc.name}.", dir=root))
    home, work = base / "home", base / "work"
    home.mkdir()
    work.mkdir()
    if sc.workspace:
        WORKSPACES[sc.workspace](work, home)
    if sc.share:
        shared[sc.share] = (home, work)
    return home, work


def run_one(agy: str, sc: Scenario, root: Path, shared: Shared) -> dict[str, Any]:
    home, work = prepare(sc, root, shared)
    spec_dir = Path(tempfile.mkdtemp(prefix=f"spec-{sc.name}.", dir=root))
    spec = {
        "scenario": {k: v for k, v in asdict(sc).items() if k != "stdin"},
        "stdin_hex": sc.stdin.hex() if sc.stdin is not None else None,
        "agy": agy,
        "home": str(home),
        "work": str(work),
        "root": str(root),
        "result": str(spec_dir / "result.json"),
    }
    (spec_dir / "spec.json").write_text(json.dumps(spec))
    me = Path(__file__).resolve()
    agy_dir = str(Path(agy).resolve().parent)
    cmd = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(root),
        str(root),
        "--ro-bind",
        str(me.parent),
        str(me.parent),
        "--ro-bind",
        agy_dir,
        agy_dir,
        "--unshare-net",
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
        "--",
        sys.executable,
        "-I",
        str(me),
        "inner",
        str(spec_dir / "spec.json"),
    ]
    subprocess.run(cmd, check=True, timeout=sc.timeout + 60)
    record = json.loads((spec_dir / "result.json").read_text())
    return normalize(record, root, sc, agy)


def normalize(record: dict[str, Any], root: Path, sc: Scenario, agy: str) -> dict[str, Any]:
    """The record as committed: no probe or binary path, no OAuth query value, bounded lines."""
    text = json.dumps(record, ensure_ascii=False)
    text = text.replace(str(root), "<root>").replace(agy, "agy")
    text = OAUTH_QUERY.sub(lambda m: m.group(1) + "=<redacted>", text)
    out = json.loads(text)
    for stream in ("stdout", "stderr"):
        for item in out[stream]:
            if len(item[1]) > LINE_LIMIT:
                item[1] = item[1][:LINE_LIMIT] + f"<cut by the probe: {len(item[1])} chars>"
    if len(out["argv"]) and any(len(a) > EXCERPT_LIMIT for a in out["argv"]):
        out["argv"] = [
            a if len(a) <= EXCERPT_LIMIT else a[:80] + f"<{len(a)} chars>" for a in out["argv"]
        ]
    out["scenario"] = sc.name
    if sc.note:
        out["note"] = sc.note
    if sc.stdin is not None:
        out["stdin_bytes"] = len(sc.stdin)
    return out


def record_all(agy: str, out: Path, only: set[str] | None = None, workers: int = 8) -> list[str]:
    """Run the scenarios (groups in order, the rest in parallel) and write one record each."""
    if shutil.which("bwrap") is None:
        raise SystemExit("bwrap is required")
    version = subprocess.run([agy, "--version"], capture_output=True, text=True).stdout.strip()
    out.mkdir(parents=True, exist_ok=True)
    chosen = [s for s in SCENARIOS if only is None or s.name in only]
    groups: dict[str, list[Scenario]] = {}
    for sc in chosen:
        groups.setdefault(sc.group or sc.name, []).append(sc)
    written: list[str] = []
    with tempfile.TemporaryDirectory(prefix="agy-probe.") as tmp:
        root = Path(tmp).resolve()
        shared: dict[str, tuple[Path, Path]] = {}

        def run_group(members: list[Scenario]) -> list[dict[str, Any]]:
            if members[0].group == "concurrent":
                prepare(members[0], root, shared)  # one home and workspace, made once
                with concurrent.futures.ThreadPoolExecutor(len(members)) as pool:
                    return list(pool.map(lambda s: run_one(agy, s, root, shared), members))
            return [run_one(agy, s, root, shared) for s in members]

        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            for records in pool.map(run_group, groups.values()):
                for rec in records:
                    rec["agy_version"] = version
                    path = out / f"{rec['scenario']}.json"
                    path.write_text(
                        json.dumps(rec, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
                    )
                    written.append(rec["scenario"])
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--agy", required=True)
    rec.add_argument("--out", required=True, type=Path)
    rec.add_argument("--only", default="")
    inn = sub.add_parser("inner")
    inn.add_argument("spec")
    a = ap.parse_args()
    if a.mode == "inner":
        return inner(a.spec)
    only = {x for x in a.only.split(",") if x} or None
    names = record_all(str(Path(a.agy).expanduser().resolve()), a.out, only)
    print(f"{len(names)} records in {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
