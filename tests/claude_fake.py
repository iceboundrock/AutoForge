"""A fake ``claude`` that prints a scripted stream-json transcript (#192).

The real :class:`autoforge.providers.ClaudeCodeProvider` launches it under
the duplex handle exactly as it launches the CLI. It lives in ``tmp_path``,
outside the repository, and logs its argv, its cwd and whether its stdin is
``/dev/null``; then it prints each scripted line (``$SENTINEL`` replaced by
the value of ``CLAUDE_TEST_SENTINEL`` from its environment, ``$CWD`` by its
working directory, JSON-escaped, and ``$HEAD`` by ``git rev-parse HEAD`` in
it, read once when a line first needs it: a commit made before the run, such
as an implementation's, which no script can know in advance), sleeps where
the script says so, writes the scripted stderr and exits with the scripted
status. A ``spawn_holder`` step starts a helper in its process group that
inherits its stdout, the way a server an agent left running holds it: the
helper sleeps ``spawn_holder`` seconds, writes its ``then`` text (raw, so
its LF is the caller's) and exits, or with ``every`` writes ``then`` every
``every`` seconds for as long, the way a chatty one keeps stdout busy; its
pid is appended to ``holders`` in the fake's home. A ``close_stdout`` step
points the fake's stdout at ``/dev/null`` and goes on with the script, the
way a CLI that closed its stdout but has not exited does (a helper started
after it does not hold stdout either). No real agent runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from autoforge.config import ProfileConfig

_HOLDER = (
    "import sys, time\n"
    "hold, then, every = float(sys.argv[1]), sys.argv[2], float(sys.argv[3])\n"
    "end = time.monotonic() + hold\n"
    "while every and time.monotonic() < end:\n"
    "    sys.stdout.write(then); sys.stdout.flush(); time.sleep(every)\n"
    "time.sleep(max(0.0, end - time.monotonic()))\n"
    "sys.stdout.write('' if every else then)\n"
)

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
head = None
for step in script["lines"]:
    if isinstance(step, dict) and "spawn_holder" in step:
        holder = subprocess.Popen(
            [sys.executable, "-c", script["holder"], str(step["spawn_holder"]),
             step.get("then", ""), str(step.get("every", 0))]
        )
        with open(os.path.join(home, "holders"), "a", encoding="utf-8") as f:
            f.write(f"{holder.pid}\n")
        continue
    if isinstance(step, dict) and step.get("close_stdout"):
        sys.stdout.flush()
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
        continue
    if isinstance(step, dict):
        time.sleep(step["sleep"])
        continue
    if "$HEAD" in step and head is None:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    if head is not None:
        step = step.replace("$HEAD", head)
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


# A fake ``claude`` whose Bash tool runs one command the way 2.1.296 does
# (read from the build; ``autoforge.providers``, ``CLAUDE_SHELL_CLEANER``):
# the ``env`` of the user's, the project's and the local settings, those
# ``--setting-sources`` names (all three by default), is set over its
# environment in that order; the shell is ``CLAUDE_CODE_SHELL`` when its
# path names a bash or a zsh and runs, else ``SHELL`` on the same terms,
# else the zsh or bash on ``PATH``;
# the snapshot is written by that shell as ``-c -l``, sourcing
# ``$HOME/.zshrc`` or ``$HOME/.bashrc`` and dumping its functions (but those
# named ``_x``), options and aliases with the same commands; the command
# sources the snapshot, then runs the trimmed text of ``CLAUDE_ENV_FILE``,
# the extglob reset and ``unsetenv`` removal, then ``eval``s the command, all
# joined by `` && ``, through ``CLAUDE_CODE_SHELL_PREFIX`` when it is set,
# and as ``-c -l`` instead without a snapshot. The command's output and
# status are the run's result text. No real agent runs.
_BASH_TOOL_SCRIPT = r"""
import json, os, shlex, shutil, subprocess, sys

home = sys.argv[1]
with open(os.path.join(home, "script.json"), encoding="utf-8") as f:
    script = json.load(f)
env = dict(os.environ)
q = shlex.quote
sources = "user,project,local"
args = sys.argv[2:]
for index, arg in enumerate(args):
    if arg == "--":
        break
    if arg == "--setting-sources":
        sources = args[index + 1]
    elif arg.startswith("--setting-sources="):
        sources = arg.partition("=")[2]
config = env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME", ""), ".claude")
settings = {
    "user": os.path.join(config, "settings.json"),
    "project": os.path.join(os.getcwd(), ".claude", "settings.json"),
    "local": os.path.join(os.getcwd(), ".claude", "settings.local.json"),
}
for source in ("user", "project", "local"):
    if source in sources.split(",") and os.path.isfile(settings[source]):
        with open(settings[source], encoding="utf-8") as f:
            env.update(json.load(f).get("env", {}))


def runs(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def pick_shell():
    override = env.get("CLAUDE_CODE_SHELL", "")
    if override and ("bash" in override or "zsh" in override) and runs(override):
        return override
    shell = env.get("SHELL", "")
    if shell and ("bash" in shell or "zsh" in shell) and runs(shell):
        return shell
    for name in ("bash", "zsh") if "bash" in shell else ("zsh", "bash"):
        if shutil.which(name):
            return shutil.which(name)
    sys.exit("no suitable shell found")


shell = pick_shell()
zsh = "zsh" in shell and "bash" not in shell
rc = os.path.join(env.get("HOME", ""), ".zshrc" if zsh else ".bashrc")
snapshot = os.path.join(home, "snapshot.sh")
dump = [
    f"SNAPSHOT_FILE={q(snapshot)}",
    f"source {q(rc)} < /dev/null" if os.path.isfile(rc) else "# No user config file to source",
    'echo "# Snapshot file" >| "$SNAPSHOT_FILE"',
    'echo "unalias -a 2>/dev/null || true" >> "$SNAPSHOT_FILE"',
]
if zsh:
    dump += [
        "typeset -f > /dev/null 2>&1",
        "typeset +f | grep -vE '^_[^_]' | while read func; do",
        '  typeset -f "$func" >> "$SNAPSHOT_FILE"',
        "done",
        "setopt | sed 's/^/setopt /' | head -n 1000 >> \"$SNAPSHOT_FILE\"",
    ]
else:
    dump += [
        'shopt -p | head -n 1000 >> "$SNAPSHOT_FILE"',
        "declare -f > /dev/null 2>&1",
        "declare -F | cut -d' ' -f3 | grep -vE '^_[^_]' | while read func; do",
        "  printf 'eval %q > /dev/null 2>&1\\n' \"$(declare -f \"$func\")\" >> \"$SNAPSHOT_FILE\"",
        "done",
        "set -o | grep \"on\" | awk '{print \"set -o \" $1}' | head -n 1000 >> \"$SNAPSHOT_FILE\"",
        'echo "shopt -s expand_aliases" >> "$SNAPSHOT_FILE"',
    ]
dump.append(
    "alias | sed 's/^alias //g' | sed 's/^/alias -- /' | head -n 1000 >> \"$SNAPSHOT_FILE\""
)
if script["snapshot"]:
    subprocess.run(
        [shell, "-c", "-l", "\n".join(dump)], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
    )
snap = snapshot if os.path.isfile(snapshot) else None

parts = [f"source {q(snap)} 2>/dev/null || true"] if snap else []
if env.get("CLAUDE_ENV_FILE"):
    try:
        with open(env["CLAUDE_ENV_FILE"], encoding="utf-8") as f:
            text = f.read().strip()
    except FileNotFoundError:
        text = ""
    if text:
        parts.append(text)
prefix = env.get("CLAUDE_CODE_SHELL_PREFIX")
if prefix:
    parts.append(
        "{ shopt -u extglob || setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL; } >/dev/null 2>&1 || true"
    )
elif zsh:
    parts.append("setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL 2>/dev/null || true")
else:
    parts.append("shopt -u extglob 2>/dev/null || true")
parts.append(
    "{ \\builtin unalias -- 'unsetenv'; \\builtin unset -f -- 'unsetenv'; } >/dev/null 2>&1"
    " || true"
)
parts.append(f"eval {q(script['command'])}")
parts.append(f"pwd -P >| {q(os.path.join(home, 'cwd'))}")
string = " && ".join(parts)
if prefix:
    cut = prefix.rfind(" -")
    if cut > 0:
        string = f"{q(prefix[:cut])} {prefix[cut + 1:]} {q(string)}"
    else:
        string = f"{q(prefix)} {q(string)}"
ran = subprocess.run(
    [shell, "-c", *([] if snap else ["-l"]), string], env=env, stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
)
with open(os.path.join(home, "log.json"), "w", encoding="utf-8") as f:
    json.dump({"argv": sys.argv[2:], "shell": shell, "snapshot": snap is not None}, f)
text = ran.stdout.decode("utf-8", "replace") + f"[exit {ran.returncode}]"
if "stream-json" in sys.argv[2:]:
    record = {"type": "result", "subtype": "success", "is_error": False, "result": text}
    print(json.dumps(record), flush=True)
else:
    print(text, flush=True)
"""


def fake_claude_bash_tool(
    tmp_path: Path, command: str, *, snapshot: bool = True, output_format: str = "stream-json"
) -> tuple[Path, ProfileConfig]:
    """Write a fake ``claude`` whose Bash tool runs ``command`` as 2.1.296
    does (``_BASH_TOOL_SCRIPT``; without a snapshot with ``snapshot=False``,
    as when writing one fails); return its home and a profile launching it.
    """
    home = tmp_path / "fake-claude"
    home.mkdir()
    (home / "fake.py").write_text(_BASH_TOOL_SCRIPT, encoding="utf-8")
    (home / "script.json").write_text(
        json.dumps({"command": command, "snapshot": snapshot}), encoding="utf-8"
    )
    launcher = home / "claude"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -I "{home / "fake.py"}" "{home}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    profile = ProfileConfig(
        name="fix",
        provider="claude",
        model="fable",
        effort="high",
        command=str(launcher),
        options={"permission_mode": "bypassPermissions", "output_format": output_format},
    )
    return home, profile
