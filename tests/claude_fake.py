"""A fake ``claude`` that prints a scripted stream-json transcript (#192).

The real :class:`autoforge.providers.ClaudeCodeProvider` launches it under
the duplex handle exactly as it launches the CLI. It lives in ``tmp_path``,
outside the repository, and logs its argv, its cwd and whether its stdin is
``/dev/null``; then it prints each scripted line (``$SENTINEL`` replaced by
the value of ``CLAUDE_TEST_SENTINEL`` from its environment, ``$CWD`` by its
working directory, JSON-escaped), sleeps where
the script says so, writes the scripted stderr and exits with the scripted
status. A ``spawn_holder`` step starts a helper in its process group that
inherits its stdout, the way a server an agent left running holds it: the
helper sleeps, writes its ``then`` text (raw, so its LF is the caller's) and
exits, and its pid is appended to ``holders`` in the fake's home. No real
agent runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from autoforge.config import ProfileConfig

_HOLDER = "import sys, time; time.sleep(float(sys.argv[1])); sys.stdout.write(sys.argv[2])"

_SCRIPT = r"""
import json, os, subprocess, sys, time

home = sys.argv[1]
with open(os.path.join(home, "log.json"), "w", encoding="utf-8") as f:
    json.dump(
        {
            "argv": sys.argv[2:],
            "cwd": os.getcwd(),
            "stdin_is_devnull": os.path.samestat(os.fstat(0), os.stat(os.devnull)),
        },
        f,
    )
with open(os.path.join(home, "script.json"), encoding="utf-8") as f:
    script = json.load(f)
sentinel = os.environ.get("CLAUDE_TEST_SENTINEL", "")
cwd = json.dumps(os.getcwd())[1:-1]
for step in script["lines"]:
    if isinstance(step, dict) and "spawn_holder" in step:
        holder = subprocess.Popen(
            [sys.executable, "-c", script["holder"], str(step["spawn_holder"]),
             step.get("then", "")]
        )
        with open(os.path.join(home, "holders"), "a", encoding="utf-8") as f:
            f.write(f"{holder.pid}\n")
        continue
    if isinstance(step, dict):
        time.sleep(step["sleep"])
        continue
    sys.stdout.write(step.replace("$SENTINEL", sentinel).replace("$CWD", cwd) + "\n")
    sys.stdout.flush()
sys.stderr.write(script["stderr"])
sys.exit(script["exit"])
"""


def line(record: dict) -> str:
    return json.dumps(record)


def init(model: str = "claude-fable-5-1") -> str:
    return line({"type": "system", "subtype": "init", "model": model, "tools": ["Bash"]})


def tool_use(tool_id: str, name: str, **tool_input: object) -> str:
    block = {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
    return line({"type": "assistant", "message": {"content": [block]}})


def tool_result(tool_id: str, content: str = "ok", **extra: object) -> str:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content, **extra}
    return line({"type": "user", "message": {"content": [block]}})


def thinking(text: str) -> str:
    return line(
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": text}]}}
    )


def result(text: str, **over: object) -> str:
    record: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "num_turns": 3,
        "duration_ms": 1234,
        "total_cost_usd": 0.0125,
        "result": text,
        "usage": {"input_tokens": 1},
    }
    record.update(over)
    return line(record)


def fake_claude(
    tmp_path: Path,
    lines: list[str | dict],
    *,
    exit_code: int = 0,
    stderr: str = "",
    profile_name: str = "fix",
) -> tuple[Path, ProfileConfig]:
    """Write the fake and its script; return its home and a stream-json profile."""
    home = tmp_path / "fake-claude"
    home.mkdir()
    (home / "fake.py").write_text(_SCRIPT, encoding="utf-8")
    (home / "script.json").write_text(
        json.dumps({"lines": lines, "exit": exit_code, "stderr": stderr, "holder": _HOLDER}),
        encoding="utf-8",
    )
    command = home / "claude"
    command.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{home / "fake.py"}" "{home}" "$@"\n',
        encoding="utf-8",
    )
    command.chmod(0o755)
    profile = ProfileConfig(
        name=profile_name,
        provider="claude",
        model="fable",
        effort="high",
        command=str(command),
        options={"permission_mode": "bypassPermissions"},
    )
    return home, profile


def log(home: Path) -> dict:
    return json.loads((home / "log.json").read_text(encoding="utf-8"))


def holders(home: Path) -> list[int]:
    """The pids of the helpers ``spawn_holder`` started."""
    path = home / "holders"
    if not path.exists():
        return []
    return [int(pid) for pid in path.read_text(encoding="utf-8").split()]
