"""A loopback stand-in for an OpenAI-compatible model API, for the opt-in gate probe (#152).

The pinned OpenCode runtime calls it as the provider ``standin`` (an
``@ai-sdk/openai-compatible`` provider the probe adds to the policy's inline
document), so the probe runs a real turn with no paid call and no operator
credential, as ADR 0007 §3 asks of a probe. What it answers depends on the
first user message: ``GATE:question`` calls the ``question`` tool, then
answers with what the tool returned (``GATE:questions`` asks two, one
taking several picks and one an answer of the operator's own words);
``GATE:slow`` streams text for a minute
unless the client goes away; anything else answers :data:`ANSWER` at once.
A request without tools (the runtime's title) gets a short title.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = 'Probed.\n<<<CONTROL_RESULT>>>\n{"gate": true}\n<<<END_CONTROL_RESULT>>>'
QUESTIONS = {
    "questions": [
        {
            "question": "Which colours?",
            "header": "Colours",
            "multiple": True,
            "options": [
                {"label": "red", "description": "warm"},
                {"label": "blue", "description": "cool"},
            ],
        },
        {
            "question": "Which name?",
            "header": "Name",
            "custom": True,
            "options": [{"label": "alpha", "description": "the first"}],
        },
    ]
}
QUESTION = {
    "questions": [
        {
            "question": "Which colour?",
            "header": "Colour",
            "options": [
                {"label": "red", "description": "warm"},
                {"label": "blue", "description": "cool"},
            ],
        }
    ]
}


def _chunk(delta: dict, finish: str | None = None) -> bytes:
    body = {
        "id": "chatcmpl-standin",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "m1",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return b"data: " + json.dumps(body).encode() + b"\n\n"


class StandIn:
    """The stand-in, served on a thread; ``requests`` records what each call carried."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        standin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                messages = body.get("messages", [])
                tools = [tool["function"]["name"] for tool in body.get("tools", [])]
                standin.requests.append({"tools": tools, "roles": [m["role"] for m in messages]})
                first = next((m for m in messages if m["role"] == "user"), {})
                text = first.get("content")
                if isinstance(text, list):
                    text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
                last = messages[-1] if messages else {}
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                try:
                    for chunk in standin.answer(str(text or ""), tools, last):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self) -> StandIn:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, text: str, tools: list[str], last: dict):
        if not tools:
            yield _chunk({"role": "assistant", "content": "Gate probe"})
            yield _chunk({}, "stop")
            return
        if last.get("role") == "tool":
            result = last.get("content")
            yield _chunk({"role": "assistant", "content": f"tool said: {json.dumps(result)}\n"})
            yield _chunk({"content": ANSWER})
            yield _chunk({}, "stop")
            return
        if "GATE:question" in text:
            asked = QUESTIONS if "GATE:questions" in text else QUESTION
            call = {
                "index": 0,
                "id": "call_question",
                "type": "function",
                "function": {"name": "question", "arguments": json.dumps(asked)},
            }
            yield _chunk({"role": "assistant", "tool_calls": [call]})
            yield _chunk({}, "tool_calls")
            return
        if "GATE:slow" in text:
            for n in range(240):
                yield _chunk({"role": "assistant", "content": f"tick {n} "})
                time.sleep(0.25)
            yield _chunk({}, "stop")
            return
        yield _chunk({"role": "assistant", "content": ANSWER})
        yield _chunk({}, "stop")
