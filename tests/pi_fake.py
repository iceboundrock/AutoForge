"""A fake ``pi`` executable and a scripted wrapper around the real :class:`PiProvider`.

Engine-level Pi tests (#133) drive the real adapter and the real duplex
handle against a real child process, so nothing between the engine and the
RPC wire is replaced. Only the agent's *work* is scripted, exactly as for
:class:`ScriptedProvider`:

* :class:`PiFake` is a small Python script behind an executable ``pi`` in a
  directory of its own (never inside the repository under test, so it cannot
  change a LOCAL workspace fingerprint). It answers ``pi auth check`` and
  plays the RPC subset #131's fixtures use: ``get_state`` and
  ``get_available_models`` answered from its own ``--model`` /
  ``--thinking``, ``prompt``, the event stream up to ``agent_settled``,
  ``get_last_assistant_text`` and ``abort``. Every launch, its argv, cwd,
  environment *names* and every stdin record is appended to ``log.jsonl``,
  so a test can count spawns and read the prompt that travelled over RPC.
* :class:`ScriptedPi` is a :class:`PiProvider` whose ``execute`` first runs a
  ``ScriptedProvider``-style handler (``(AgentRequest) -> str``) that mutates
  ``FakeGitHub`` or the LOCAL tree the way the agent would, then arms the fake
  with the returned final assistant text and runs the real ``execute``. A
  handler may return a :class:`PiTurn` instead of a string to script a failure.

No network, no real Pi and no credential is involved. Every record is LF
terminated and every wait is bounded by the profile's deadline.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from autoforge.config import AutoForgeConfig
from autoforge.pi_rpc import js_trim
from autoforge.providers import AgentExecutionResult, AgentRequest, PiProvider

_SCRIPT = r"""
import json, os, subprocess, sys, time

home = sys.argv[1]
argv = sys.argv[2:]
with open(os.path.join(home, "turn.json"), encoding="utf-8") as f:
    turn = json.load(f)
log = open(os.path.join(home, "log.jsonl"), "a", encoding="utf-8")


def note(**entry):
    log.write(json.dumps({"call": turn["call"], **entry}) + "\n")
    log.flush()


note(argv=argv, cwd=os.getcwd(), env=sorted(os.environ))


def flag(name):
    return argv[argv.index(name) + 1]


model = flag("--model")
provider, model_id = model.split("/", 1)
if argv[:2] == ["auth", "check"]:
    auth = turn["auth"] or {"status": "ready", "provider": provider, "authType": "oauth"}
    print(json.dumps(auth))
    sys.exit(turn["auth_exit"])

if turn["stderr"]:
    sys.stderr.write(turn["stderr"])
    sys.stderr.flush()
out = sys.stdout.buffer
text = turn["text"]
outcome = turn["outcome"]


def emit(obj):
    out.write(json.dumps(obj).encode("utf-8") + b"\n")
    out.flush()


def respond(command, rid, data=None, success=True, error=None):
    record = {"type": "response", "id": rid, "command": command, "success": success}
    if data is not None:
        record["data"] = data
    if error is not None:
        record["error"] = error
    emit(record)


def assistant(stop, **extra):
    content = [{"type": "text", "text": text}] if text else []
    return {"role": "assistant", "content": content, "stopReason": stop, **extra}


def settle(message):
    emit({"type": "message_end", "message": message})
    emit({"type": "agent_end", "messages": [], "willRetry": False})
    emit({"type": "agent_settled"})


for line in sys.stdin.buffer:
    note(stdin=line.decode("utf-8"))
    command = json.loads(line)
    kind, rid = command["type"], command.get("id")
    if kind == "get_state":
        resolved = model_id + "-other" if outcome == "model_mismatch" else model_id
        state = {
            "model": {"provider": provider, "id": resolved, "name": "fake", "contextWindow": 1},
            "thinkingLevel": flag("--thinking"),
            "isStreaming": False,
        }
        respond(kind, rid, state)
    elif kind == "get_available_models":
        respond(kind, rid, {"models": [{"provider": provider, "id": model_id}]})
    elif kind == "prompt":
        if outcome == "reject":
            respond(kind, rid, success=False, error=turn["error"])
            continue
        respond(kind, rid, {"disposition": "started"})
        emit({"type": "agent_start"})
        for rel, content in turn["writes"].items():
            # What an agent's edit tool does: write into its working directory.
            with open(os.path.join(os.getcwd(), rel), "w", encoding="utf-8") as f:
                f.write(content)
        if turn["detach"]:
            # As Pi's bash tool starts a command: a session of its own, no pipes.
            subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(%d)" % turn["detach"]],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if outcome == "exit_early":
            sys.exit(turn["exit_code"])
        if outcome == "hang":
            continue
        if outcome == "stop_error":
            settle(assistant("error", errorMessage=turn["error"]))
        else:
            settle(assistant("stop"))
    elif kind == "get_last_assistant_text":
        respond(kind, rid, {"text": turn["final"]})
    elif kind == "abort":
        respond(kind, rid)
        emit({"type": "agent_settled"})
sys.exit(0)
"""


@dataclass
class PiTurn:
    """What the fake ``pi`` does for one launch.

    ``outcome`` is one of ``settle`` (the run ends with ``text`` as the final
    assistant text), ``reject`` (the prompt is refused with ``error``, as Pi
    does without a usable credential), ``stop_error`` (accepted, then a model
    error ``error``), ``exit_early`` (accepted, then ``exit_code`` before
    ``agent_settled``), ``model_mismatch`` (``get_state`` reports another
    model id) and ``hang`` (accepted, then silence until the deadline).
    ``stderr`` is written when the RPC child starts; once the prompt is
    accepted, ``writes`` (paths relative to the cwd) are written into the
    agent's working directory and ``detach`` starts a detached sleeper for
    that many seconds. ``auth`` and ``auth_exit`` script the ``pi auth
    check`` preflight.
    """

    text: str = ""
    outcome: str = "settle"
    error: str = ""
    exit_code: int = 1
    stderr: str = ""
    writes: dict[str, str] = field(default_factory=dict)
    detach: int = 0
    auth: dict | None = None
    auth_exit: int = 0


class PiFake:
    """The fake ``pi`` executable and the log of everything launched through it."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.log = self.home / "log.jsonl"
        script = self.home / "fake_pi.py"
        script.write_text(_SCRIPT, encoding="utf-8")
        self.command = self.home / "pi"
        self.command.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{self.home}" "$@"\n',
            encoding="utf-8",
        )
        self.command.chmod(0o755)

    def arm(self, turn: PiTurn, call: int) -> None:
        """Script the next launch(es): the auth preflight and the RPC child."""
        # Pi returns the text blocks of the last assistant message, trimmed
        # as JavaScript trims; the adapter cross-checks the two.
        payload = {"call": call, "final": js_trim(turn.text), **asdict(turn)}
        (self.home / "turn.json").write_text(json.dumps(payload), encoding="utf-8")

    def spawned(self) -> bool:
        """Whether anything (preflight or RPC child) was ever started."""
        return self.log.exists()

    def entries(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def launches(self) -> list[dict]:
        """One entry per RPC child (``argv``, ``cwd``, ``env``), in order."""
        return [e for e in self.entries() if e.get("argv", [""])[0] == "--mode"]

    def auth_checks(self) -> list[dict]:
        return [e for e in self.entries() if e.get("argv", [""])[:2] == ["auth", "check"]]

    def records(self, call: int) -> list[dict]:
        """The stdin records the RPC child of ``call`` received, decoded."""
        return [
            json.loads(e["stdin"]) for e in self.entries() if e["call"] == call and "stdin" in e
        ]

    def prompts(self) -> list[str]:
        """The ``prompt`` message of every launch that got that far, in order."""
        calls = sorted({e["call"] for e in self.launches()})
        return [r["message"] for c in calls for r in self.records(c) if r["type"] == "prompt"]


class ScriptedPi(PiProvider):
    """The real :class:`PiProvider`, preceded by a scripted agent's work.

    ``script`` takes the same shapes :class:`ScriptedProvider` does (a list
    consumed in order, or a handler) and may yield a :class:`PiTurn`. Every
    request is recorded in ``calls`` and every result in ``results``.
    """

    def __init__(
        self,
        fake: PiFake,
        script: list[str | PiTurn] | Callable[[AgentRequest], str | PiTurn] | None = None,
    ) -> None:
        super().__init__(round_trip_seconds=5, abort_seconds=1)
        self.fake = fake
        self._queue: list[str | PiTurn] = list(script) if isinstance(script, list) else []
        self._handler = script if callable(script) else None
        self.calls: list[AgentRequest] = []
        self.results: list[AgentExecutionResult] = []

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        self.calls.append(req)
        if self._handler is not None:
            turn = self._handler(req)
        elif self._queue:
            turn = self._queue.pop(0)
        else:
            turn = ""
        self.fake.arm(turn if isinstance(turn, PiTurn) else PiTurn(text=turn), len(self.calls))
        result = super().execute(req)
        self.results.append(result)
        return result


# Every profile a run can route through, each with its own model and thinking
# level so a launch's argv names the profile it came from.
PI_PROFILES: dict[str, tuple[str, str]] = {
    "analyze_execute": ("openai/pi-analyze", "high"),
    "fix": ("openai/pi-fix", "medium"),
    "review_round_1": ("openai/pi-review-1", "low"),
    "review_round_2_5": ("openai/pi-review-2-5", "xhigh"),
    "review_round_6_plus": ("openai/pi-review-6", "max"),
    "replan_reexecute": ("openai/pi-replan", "minimal"),
    "update_epic": ("openai/pi-epic", "off"),
}


def route_to_pi(cfg: AutoForgeConfig, fake: PiFake, names=None, **profile_fields) -> None:
    """Route ``names`` (default: every profile) through ``provider: pi`` on ``fake``."""
    for name in names if names is not None else PI_PROFILES:
        model, effort = PI_PROFILES[name]
        cfg.profiles[name] = replace(
            cfg.profile(name),
            provider="pi",
            model=model,
            effort=effort,
            command=str(fake.command),
            extra_args=[],
            options={},
            **profile_fields,
        )


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def make_pi_engine(state_dir, fake: PiFake, script, github=None, cfg=None, names=None):
    """``make_engine`` with ``names`` (default: every profile) routed to Pi on ``fake``.

    The same ``script`` drives both providers: the profiles left on Claude
    Code run it through the engine's ``ScriptedProvider`` (``eng.provider``),
    the Pi ones through a :class:`ScriptedPi` (``eng.pi``).
    """
    from autoforge.config import default_config
    from tests.conftest import make_engine

    cfg = cfg or default_config()
    route_to_pi(cfg, fake, names)
    eng = make_engine(state_dir, script, github=github, cfg=cfg)
    eng.pi = ScriptedPi(fake, script)
    eng.providers._overrides["pi"] = eng.pi
    return eng
