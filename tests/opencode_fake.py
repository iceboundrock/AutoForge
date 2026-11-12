"""A fake OpenCode server for the bridge's tests (#152): no runtime, no model, no network.

Started by the real packaged bridge (``sdk_helpers/opencode_bridge.mjs``) in
place of the pinned runtime, as ``<server_command> serve --hostname=127.0.0.1
--port=0``. It binds a loopback port, prints the runtime's readiness line,
and serves the routes the bridge calls (ADR 0007 D3.5) with the shapes the
pinned runtime 1.18.34 answered with when #152 probed it: the version
read-back, the event stream, session get and create under ``/api``, the
classic prompt admission and its read-back, the history, the status, abort,
and question and permission reply, reject and list. Every request must carry
HTTP Basic authentication with the password the bridge put in its
environment, as the runtime requires; one without it gets 401.

``FAKE_OPENCODE_SCENARIO`` picks what happens after an input is admitted;
``FAKE_OPENCODE_LOG`` names a JSON-lines file it records into: its argv,
which variables it got (names, and whether the password was set, never a
value), each request, each reply. Scenarios are listed in :data:`SCENARIOS`.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCENARIOS = (
    "answer",  # one turn, a final answer
    "two-parts",  # a final answer of two text parts and a synthetic one
    "tool",  # a tool runs, then the answer
    "question",  # a question; the answer is folded into the final text
    "questions",  # two questions, one multiple, one custom
    "permission",  # a permission ask; the reply is recorded
    "two-permissions",  # two pending asks; a reject settles both
    "foreign-ask",  # a question from another session
    "v2-ask",  # an ask of the /api family
    "malformed-ask",  # a question without options
    "repeat-ask",  # the same question event twice
    "model-error",  # the answer ends with a retryable 503
    "unknown-finish",  # the answer ends for a reason the bridge does not know
    "tool-calls-only",  # the session settles after a tool call, with no answer
    "later-input",  # the history holds another input after this one
    "slow",  # text forever, until aborted
    "silent",  # busy, then nothing at all
    "unknown-events",  # observations the bridge does not know, then the answer
    "huge-event",  # one event past any sane bound
    "disconnect",  # the event stream closes under the turn
    "unknown-status",  # a session status the bridge does not know
)

SCENARIO = os.environ.get("FAKE_OPENCODE_SCENARIO", "answer")
LOG = os.environ.get("FAKE_OPENCODE_LOG", "")
VERSION = os.environ.get("FAKE_OPENCODE_VERSION", "1.18.34")
PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
ANSWER = os.environ.get(
    "FAKE_OPENCODE_ANSWER",
    'Done.\n<<<CONTROL_RESULT>>>\n{"phase": "REVIEW"}\n<<<END_CONTROL_RESULT>>>',
)
START = os.environ.get("FAKE_OPENCODE_START", "ready")  # ready | never | exit
EXISTING = os.environ.get("FAKE_OPENCODE_EXISTING", "")  # a session id that already exists
CONFLICT = os.environ.get("FAKE_OPENCODE_CONFLICT", "")  # an input id holding other text

_lock = threading.Lock()
_log_lock = threading.Lock()
_subscribers: list[queue.Queue] = []
_sessions: dict[str, dict] = {}
_messages: dict[str, list[dict]] = {}
_pending: dict[str, dict] = {}
_replies: dict[str, threading.Event] = {}
_reply_values: dict[str, dict] = {}
_aborted = threading.Event()
_busy: set[str] = set()
_counter = [0]
_sessions_of: dict[str, str] = {}


def session_of(value: dict, request_id: str) -> str:
    return _sessions_of.get(request_id, "")


# Where the sessions persist, as the runtime's database does: under the data
# root its environment names, so a later launch on the same root finds them.
_STORE = os.path.join(os.environ.get("XDG_DATA_HOME", "."), "fake-opencode.json")


def load() -> None:
    try:
        with open(_STORE, encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return
    _sessions.update(stored["sessions"])
    _messages.update(stored["messages"])
    _counter[0] = stored["counter"]


def save() -> None:
    with _lock:
        data = {"sessions": _sessions, "messages": _messages, "counter": _counter[0]}
        with open(_STORE, "w", encoding="utf-8") as handle:
            json.dump(data, handle)


def log(**entry: object) -> None:
    if not LOG:
        return
    with _log_lock, open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def next_id(prefix: str) -> str:
    with _lock:
        _counter[0] += 1
        return f"{prefix}_fake{_counter[0]:06d}"


def emit(event_type: str, properties: dict) -> None:
    event = {"id": next_id("evt"), "type": event_type, "properties": properties}
    with _lock:
        subscribers = list(_subscribers)
    for subscriber in subscribers:
        subscriber.put(event)


def status(session: str, kind: str) -> None:
    if kind == "idle":
        _busy.discard(session)
    else:
        _busy.add(session)
    emit("session.status", {"sessionID": session, "status": {"type": kind}})
    if kind == "idle":
        emit("session.idle", {"sessionID": session})


def assistant(session: str, parent: str, *, finish: str | None, parts: list[dict], error=None):
    info = {
        "id": next_id("msg"),
        "sessionID": session,
        "role": "assistant",
        "parentID": parent,
        "time": {"created": int(time.time() * 1000), "completed": int(time.time() * 1000)},
        "modelID": "m1",
        "providerID": "fake",
        "mode": "autoforge",
        "agent": "autoforge",
    }
    if finish is not None:
        info["finish"] = finish
    if error is not None:
        info["error"] = error
    message = {"info": info, "parts": parts}
    _messages[session].append(message)
    save()
    emit("message.updated", {"sessionID": session, "info": info})
    return message


def text_part(text: str, **extra: object) -> dict:
    return {"id": next_id("prt"), "type": "text", "text": text, **extra}


def ask_and_wait(session: str, kind: str, request: dict, timeout: float = 60.0) -> dict | None:
    request_id = request["id"]
    _pending[request_id] = {"kind": kind, "sessionID": session, **request}
    done = threading.Event()
    _replies[request_id] = done
    emit(f"{kind}.asked", request)
    done.wait(timeout)
    return _reply_values.get(request_id)


def run_turn(session: str, input_id: str) -> None:
    """What the session does after ``input_id`` is admitted, by scenario."""
    time.sleep(0.05)
    status(session, "busy")
    s = SCENARIO
    if s == "silent":
        return
    if s == "unknown-status":
        emit("session.status", {"sessionID": session, "status": {"type": "hibernating"}})
        return
    if s == "disconnect":
        time.sleep(0.2)
        with _lock:
            for subscriber in _subscribers:
                subscriber.put(None)
        return
    if s == "huge-event":
        emit(
            "message.part.updated",
            {"sessionID": session, "part": {"type": "text", "text": "x" * (256 * 1024)}},
        )
    if s == "unknown-events":
        emit("session.hologram", {"sessionID": session})
        emit("tui.toast.show", {})
    if s == "slow":
        while not _aborted.is_set():
            emit("message.part.delta", {"sessionID": session, "delta": "tick"})
            emit(
                "message.part.updated",
                {"sessionID": session, "part": {"type": "text", "text": "tick"}},
            )
            time.sleep(0.2)
        assistant(
            session,
            input_id,
            finish=None,
            parts=[text_part("tick tick")],
            error={"name": "MessageAbortedError", "data": {"message": "Aborted"}},
        )
        emit("session.error", {"sessionID": session, "error": {"name": "MessageAbortedError"}})
        status(session, "idle")
        return
    if s == "tool":
        for status_name in ("running", "completed"):
            emit(
                "message.part.updated",
                {
                    "sessionID": session,
                    "part": {
                        "type": "tool",
                        "tool": "read",
                        "callID": "call_1",
                        "state": {
                            "status": status_name,
                            "input": {"filePath": os.path.join(os.getcwd(), "README.md")},
                            "output": "file content",
                        },
                    },
                },
            )
    if s in ("question", "questions"):
        questions = [
            {
                "question": "Which colour?",
                "header": "Colour",
                "options": [
                    {"label": "red", "description": "warm"},
                    {"label": "blue", "description": "cool"},
                ],
            }
        ]
        if s == "questions":
            questions[0]["multiple"] = True
            questions.append(
                {
                    "question": "Which name?",
                    "header": "Name",
                    "options": [{"label": "a", "description": ""}],
                    "custom": True,
                }
            )
        reply = ask_and_wait(
            session,
            "question",
            {
                "id": next_id("que"),
                "sessionID": session,
                "questions": questions,
                "tool": {"messageID": "msg_tool", "callID": "call_q"},
            },
        )
        if reply is None or reply.get("rejected"):
            text = "the question was rejected"
        else:
            text = "answers: " + json.dumps(reply.get("answers"))
        finish(session, input_id, f"{text}\n{ANSWER}")
        return
    if s in ("permission", "two-permissions"):
        first = {
            "id": next_id("per"),
            "sessionID": session,
            "permission": "bash",
            "patterns": ["git status"],
            "metadata": {"command": "git status"},
            "always": ["git status"],
            "tool": {"messageID": "msg_tool", "callID": "call_p"},
        }
        if s == "two-permissions":
            # Both pending at once, as two parallel tool calls ask.
            second = dict(
                first, id=next_id("per"), patterns=["ls"], tool={"messageID": "m", "callID": "c2"}
            )
            _pending[second["id"]] = {"kind": "permission", **second}
            _pending[first["id"]] = {"kind": "permission", **first}
            emit("permission.asked", second)
        reply = ask_and_wait(session, "permission", first)
        finish(session, input_id, f"permission: {json.dumps(reply)}\n{ANSWER}")
        return
    if s == "foreign-ask":
        ask_and_wait(
            session,
            "question",
            {
                "id": next_id("que"),
                "sessionID": "ses_someone_else",
                "questions": [
                    {"question": "?", "header": "", "options": [{"label": "a", "description": ""}]}
                ],
            },
            timeout=5,
        )
        return
    if s == "v2-ask":
        emit("question.v2.asked", {"id": next_id("que"), "sessionID": session, "questions": []})
        return
    if s in ("malformed-ask", "repeat-ask"):
        request = {
            "id": next_id("que"),
            "sessionID": session,
            "questions": [
                {
                    "question": "Which?",
                    "header": "H",
                    "options": [{"label": "a", "description": ""}],
                }
            ],
        }
        if s == "malformed-ask":
            del request["questions"][0]["options"]
        _pending[request["id"]] = {"kind": "question", **request}
        emit("question.asked", request)
        if s == "repeat-ask":
            time.sleep(0.05)
            emit("question.asked", request)
        return
    if s == "model-error":
        assistant(
            session,
            input_id,
            finish=None,
            parts=[],
            error={
                "name": "APIError",
                "data": {"message": "overloaded", "statusCode": 503, "isRetryable": True},
            },
        )
        emit("session.error", {"sessionID": session, "error": {"name": "APIError"}})
        status(session, "idle")
        return
    if s == "unknown-finish":
        assistant(session, input_id, finish="content-filter", parts=[text_part("blocked")])
        status(session, "idle")
        return
    if s == "tool-calls-only":
        assistant(session, input_id, finish="tool-calls", parts=[])
        status(session, "idle")
        return
    if s == "later-input":
        finish(session, input_id, ANSWER, idle=False)
        _messages[session].append(
            {
                "info": {"id": "msg_zz_later", "sessionID": session, "role": "user"},
                "parts": [text_part("another input")],
            }
        )
        status(session, "idle")
        return
    if s == "two-parts":
        parts = [text_part("first part"), text_part("hidden", synthetic=True), text_part(ANSWER)]
        emit(
            "message.part.updated",
            {"sessionID": session, "part": {"type": "step-finish", "reason": "stop"}},
        )
        assistant(session, input_id, finish="stop", parts=parts)
        status(session, "idle")
        return
    finish(session, input_id, ANSWER)


def finish(session: str, input_id: str, text: str, *, idle: bool = True) -> None:
    emit("message.part.updated", {"sessionID": session, "part": {"type": "text", "text": text}})
    emit(
        "message.part.updated",
        {"sessionID": session, "part": {"type": "step-finish", "reason": "stop"}},
    )
    assistant(session, input_id, finish="stop", parts=[{"type": "step-start"}, text_part(text)])
    if idle:
        status(session, "idle")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def _authorized(self) -> bool:
        expected = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        return bool(PASSWORD) and self.headers.get("authorization") == expected

    def _json(self, code: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, code: int) -> None:
        self.send_response(code)
        self.send_header("content-length", "0")
        self.end_headers()

    def _body(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def _route(self, method: str) -> None:
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        body = self._body() if method == "POST" else {}
        authorized = self._authorized()
        log(
            request=method,
            path=path,
            authorized=authorized,
            directory=query.get("directory", [""])[0],
        )
        if not authorized:
            return self._json(401, {"name": "Unauthorized"})
        parts = path.strip("/").split("/")
        if path == "/global/health":
            return self._json(200, {"healthy": True, "version": VERSION})
        if path == "/event":
            return self._events()
        if path == "/session/status":
            return self._json(200, {sid: {"type": "busy"} for sid in _busy})
        if parts[:2] == ["api", "session"] and len(parts) == 3 and method == "GET":
            sid = parts[2]
            if sid in _sessions or sid == EXISTING:
                info = _sessions.get(sid) or {"id": sid}
                return self._json(200, {"data": info})
            return self._json(404, {"_tag": "SessionNotFoundError"})
        if path == "/api/session" and method == "POST":
            sid = body["id"]
            info = {
                "id": sid,
                "projectID": "global",
                "agent": body.get("agent"),
                "model": {
                    **body.get("model", {}),
                    "variant": body.get("model", {}).get("variant", "default"),
                },
                "location": body.get("location"),
                "title": "fake",
            }
            _sessions[sid] = info
            _messages.setdefault(sid, [])
            save()
            log(
                created=sid,
                agent=body.get("agent"),
                model=body.get("model"),
                location=body.get("location"),
            )
            return self._json(200, {"data": info})
        if len(parts) == 3 and parts[0] == "session" and parts[2] == "prompt_async":
            sid = parts[1]
            if sid not in _messages:
                return self._json(404, {"name": "NotFound"})
            message_id = body.get("messageID")
            text = "".join(p.get("text", "") for p in body.get("parts", []))
            log(
                prompt=sid,
                message_id=message_id,
                chars=len(text),
                agent=body.get("agent"),
                model=body.get("model"),
                variant=body.get("variant"),
            )
            _messages[sid].append(
                {
                    "info": {"id": message_id, "sessionID": sid, "role": "user"},
                    "parts": [dict(p, id=next_id("prt")) for p in body.get("parts", [])],
                }
            )
            save()
            threading.Thread(target=run_turn, args=(sid, message_id), daemon=True).start()
            return self._empty(204)
        if len(parts) == 4 and parts[0] == "session" and parts[2] == "message":
            sid, mid = parts[1], parts[3]
            if mid == CONFLICT:
                return self._json(
                    200,
                    {
                        "info": {"id": mid, "sessionID": sid, "role": "user"},
                        "parts": [text_part("other")],
                    },
                )
            for message in _messages.get(sid, []):
                if message["info"]["id"] == mid:
                    return self._json(200, message)
            return self._json(404, {"name": "NotFoundError"})
        if len(parts) == 3 and parts[0] == "session" and parts[2] == "message":
            sid = parts[1]
            limit = int(query.get("limit", ["100"])[0])
            return self._json(200, _messages.get(sid, [])[-limit:])
        if len(parts) == 3 and parts[0] == "session" and parts[2] == "abort":
            log(abort=parts[1])
            _aborted.set()
            return self._json(200, True)
        if path in ("/question", "/permission"):
            kind = path[1:]
            return self._json(200, [v for v in _pending.values() if v.get("kind") == kind])
        if len(parts) == 3 and parts[0] in ("question", "permission"):
            request_id, action = parts[1], parts[2]
            if request_id not in _pending:
                return self._json(404, {"_tag": "QuestionNotFoundError"})
            asked = _pending.pop(request_id)
            _sessions_of[request_id] = asked.get("sessionID", "")
            value = dict(body)
            if action == "reject":
                value["rejected"] = True
            log(reply=request_id, kind=parts[0], action=action, body=value)
            settled = [request_id]
            if parts[0] == "permission" and body.get("reply") == "reject":
                # As the runtime does: a reject rejects the session's other
                # pending asks too (ADR 0007 D9.6).
                for other in [
                    k
                    for k, v in _pending.items()
                    if v.get("sessionID") == session_of(value, request_id)
                ]:
                    _pending.pop(other, None)
                    _reply_values[other] = {"rejected": True, "by": request_id}
                    settled.append(other)
            _reply_values[request_id] = value
            for done in settled:
                if done in _replies:
                    _replies[done].set()
            return self._json(200, True)
        return self._json(404, {"name": "NoRoute", "path": path})

    def _events(self) -> None:
        subscriber: queue.Queue = queue.Queue()
        with _lock:
            _subscribers.append(subscriber)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b'data: {"id":"evt_0","type":"server.connected","properties":{}}\n\n')
            self.wfile.flush()
            while True:
                event = subscriber.get()
                if event is None:
                    return
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            with _lock:
                if subscriber in _subscribers:
                    _subscribers.remove(subscriber)

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")


def main() -> None:
    names = sorted(
        name for name in os.environ if name.startswith(("OPENCODE_", "XDG_")) or name in ("HOME",)
    )
    log(
        argv=sys.argv[1:],
        env_names=names,
        password_set=bool(PASSWORD),
        password_length=len(PASSWORD),
        credential_set=bool(os.environ.get("OPENAI_API_KEY")),
        username_set="OPENCODE_SERVER_USERNAME" in os.environ,
        stdin_tty=os.isatty(0) if hasattr(os, "isatty") else False,
        cwd=os.getcwd(),
        pid=os.getpid(),
        pgid=os.getpgid(0),
    )
    secret_file = os.environ.get("FAKE_OPENCODE_SECRET_FILE")
    if secret_file:
        # For the test that proves the password never leaves the server's
        # environment: it compares every output of the launch with this.
        with open(secret_file, "w", encoding="utf-8") as handle:
            handle.write(PASSWORD)
    if START == "exit":
        sys.exit(3)
    load()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    print("Warning: a line before the readiness line", flush=True)
    if START == "never":
        time.sleep(3600)
    print(f"opencode server listening on http://127.0.0.1:{port}", flush=True)
    sys.stderr.write("fake server: ready\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    finally:
        log(stopped=True)


if __name__ == "__main__":
    main()
