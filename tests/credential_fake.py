"""Fake ``claude``, ``opencode`` and ``pi`` that find their credential as each CLI does (#290).

ADR 0013 §7 asks #290 to prove its routes "with fake credentials and fake
provider CLIs". The mapping (:mod:`autoforge.container_auth`) says which
variables a launch container receives; these fakes say what a CLI started
with them would authenticate with, by their own rules, written here from
each CLI's documented behaviour and not from AutoForge's declarations. The
script imports nothing of AutoForge.

The container's filesystem is a directory of the test's (``root``): a path
the CLI resolves, ``/home/agent/...`` or ``/workspace/...``, is looked up
below it, so a host path the container does not hold is absent there, and
only the container's writable places (the private home, ``/tmp``,
``/workspace``, ``/cache``) can be written. The fake's working directory is
``root/workspace``, the launch's ``/workspace``.

How each fake finds its credential (the first that holds one wins):

- ``claude``: a cloud switch (``CLAUDE_CODE_USE_BEDROCK``, ``_VERTEX``,
  ``_FOUNDRY``), then ``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_API_KEY`` and
  ``CLAUDE_CODE_OAUTH_TOKEN`` from the environment, then the login
  ``.credentials.json`` in its configuration directory
  (``$CLAUDE_CONFIG_DIR``, else ``$HOME/.claude``);
- ``opencode``: an account stored in its database for the model's provider
  first (OpenCode's own documentation: a stored account may take
  precedence over an environment key), then the provider's API-key
  variable. The database is ``$OPENCODE_DB`` (absolute, or relative to the
  data directory; ``:memory:`` is none), else ``opencode.db`` in the data
  directory, ``$XDG_DATA_HOME/opencode`` or ``$HOME/.local/share/opencode``;
- ``pi``: a credential stored for the model's provider in ``auth.json`` of
  its agent directory first (docs/pi.md: Pi uses a stored credential before
  an environment variable), then the provider's API-key variable. The agent
  directory is ``$PI_CODING_AGENT_DIR``, else ``$HOME/.pi/agent``.

``opencode`` also resolves its configuration as 2.x does (read from the
build; ``providers.opencode_isolation_env``, and the configuration-resolving
fake of ``tests/test_providers.py``, here inside the container's
filesystem): the global directory (``$OPENCODE_CONFIG_DIR``, else
``$XDG_CONFIG_HOME/opencode`` or ``$HOME/.config/opencode``), the file
``$OPENCODE_CONFIG`` names, the ``opencode.json(c)`` and ``.opencode/`` of
the launch directory and of every directory above it unless
``OPENCODE_CONFIG_PROJECT_DISABLE`` (else ``OPENCODE_DISABLE_PROJECT_CONFIG``)
is ``1`` or ``true``, and the inline ``$OPENCODE_CONFIG_CONTENT``. It reports
the MCP servers and the plugin files that configuration would load, the
global directory and whether that existed.

Then each writes into its store, as the real CLI writes its login, its
database or ``models-store.json`` at startup, and fails when the store is
not writable inside the container. It writes one JSON report outside the
container's filesystem: its argv, the names of its environment and a digest
of each value, its home, the store it resolved (as the container sees it)
and whether that store existed before it started, where its credential
came from with the value's digest, and ``opencode``'s configuration. Nothing
it prints or reports holds a value, and no network is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

CLIS = ("claude", "opencode", "pi")

_SCRIPT = r'''
import hashlib, json, os, sys

report, root, cli = REPORT, ROOT, CLI
argv = sys.argv[1:]
env = os.environ
# The launch runs in /workspace (exec_argv); the executor starts it in root/workspace.
cwd = "/workspace"
WRITABLE = ("/home/agent", "/tmp", "/workspace", "/cache")


def absolute(path):
    """``path`` as the container resolves it, from the launch's working directory."""
    return os.path.normpath(os.path.join(cwd, path))


def inside(path):
    """Where ``path``, as the container sees it, is in the stand-in filesystem."""
    return os.path.join(root, absolute(path).lstrip("/"))


def writable(path):
    path = absolute(path)
    return any(path == top or path.startswith(top + "/") for top in WRITABLE)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def option(*names):
    for i, word in enumerate(argv[:-1]):
        if word in names:
            return argv[i + 1]
    return ""


def stored(path, provider):
    try:
        with open(inside(path), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    entry = data.get(provider) if isinstance(data, dict) else None
    if isinstance(entry, dict):
        entry = entry.get("key", "")
    return entry if isinstance(entry, str) else ""


def from_env(*names):
    for name in names:
        if env.get(name):
            return "env:" + name, env[name]
    return "", ""


home = env.get("HOME", "/")
source, value = "", ""
configuration = None
if cli == "claude":
    store = absolute(env.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude"))
    login = os.path.join(store, ".credentials.json")
    existed = os.path.exists(inside(store))
    cloud = [n for n in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                         "CLAUDE_CODE_USE_FOUNDRY") if env.get(n)]
    if cloud:
        source, value = "cloud:" + cloud[0], env[cloud[0]]
    else:
        source, value = from_env("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                                 "CLAUDE_CODE_OAUTH_TOKEN")
    if not source:
        value = stored(login, "claudeAiOauth")
        source = "store:" + login if value else ""
    written, directory = os.path.join(store, ".claude.json"), store
elif cli == "opencode":
    provider = option("-m", "--model").split("/", 1)[0]
    keys = {"anthropic": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
            "google": "GOOGLE_GENERATIVE_AI_API_KEY", "openai": "OPENAI_API_KEY"}
    data = os.path.join(env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"),
                        "opencode")
    named = env.get("OPENCODE_DB")
    if named == ":memory:":
        store = ""
    elif named:
        store = absolute(os.path.join(data, named))
    else:
        store = absolute(os.path.join(data, "opencode.db"))
    existed = bool(store) and os.path.exists(inside(store))
    value = stored(store, provider) if store else ""
    source = "store:" + store if value else ""
    if not source and provider in keys:
        source, value = from_env(keys[provider])
    written, directory = store, os.path.dirname(store) if store else ""
    # Its configuration, as 2.x resolves it (providers.py, opencode_isolation_env):
    # the global directory, the explicit file, the launch directory's and every
    # one above it unless disabled (the first name decides, else the second),
    # then the inline document.
    config_dir = absolute(env.get("OPENCODE_CONFIG_DIR") or os.path.join(
        env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "opencode"))
    config_existed = os.path.exists(inside(config_dir))
    documents, directories = [], [config_dir]

    def document(path):
        try:
            with open(inside(path), encoding="utf-8") as f:
                documents.append(json.load(f))
        except (OSError, ValueError):
            pass

    for name in ("opencode.json", "opencode.jsonc"):
        document(os.path.join(config_dir, name))
    if env.get("OPENCODE_CONFIG"):
        document(env["OPENCODE_CONFIG"])
    disabled = env.get("OPENCODE_CONFIG_PROJECT_DISABLE",
                       env.get("OPENCODE_DISABLE_PROJECT_CONFIG"))
    if disabled not in ("1", "true"):
        here = cwd
        while True:
            for name in ("opencode.jsonc", "opencode.json"):
                document(os.path.join(here, name))
            directories.append(os.path.join(here, ".opencode"))
            if here == "/":
                break
            here = os.path.dirname(here)
    if env.get("OPENCODE_CONFIG_CONTENT"):
        try:
            documents.append(json.loads(env["OPENCODE_CONFIG_CONTENT"]))
        except ValueError:
            pass
    mcp = sorted({name for d in documents if isinstance(d, dict)
                  for name in (d.get("mcp") or {})})
    plugins = []
    for top in directories:
        for sub in ("plugin", "plugins"):
            try:
                plugins += [os.path.join(top, sub, p) for p in os.listdir(inside(
                    os.path.join(top, sub)))]
            except OSError:
                pass
    configuration = {"dir": config_dir, "dir_existed": config_existed,
                     "mcp": mcp, "plugins": sorted(plugins)}
elif cli == "pi":
    provider = option("--model").split("/", 1)[0]
    keys = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY"}
    store = absolute(env.get("PI_CODING_AGENT_DIR") or os.path.join(home, ".pi", "agent"))
    existed = os.path.exists(inside(store))
    value = stored(os.path.join(store, "auth.json"), provider)
    source = "store:" + os.path.join(store, "auth.json") if value else ""
    if not source and provider in keys:
        source, value = from_env(keys[provider])
    written, directory = os.path.join(store, "models-store.json"), store
else:
    sys.exit(f"credential_fake: unknown CLI {cli!r}")

wrote = False
if written:
    if writable(directory):
        os.makedirs(inside(directory), exist_ok=True)
        if not os.path.exists(inside(written)):
            with open(inside(written), "w", encoding="utf-8") as f:
                json.dump({"written_by": cli}, f)
        wrote = True
with open(report, "w", encoding="utf-8") as f:
    json.dump(
        {
            "cli": cli,
            "argv": argv,
            "cwd": cwd,
            "env": {name: digest(env[name]) for name in sorted(env)},
            "home": home,
            "store": store,
            "store_existed": existed,
            "source": source,
            "digest": digest(value) if value else "",
            "wrote": wrote,
            "configuration": configuration,
        },
        f,
    )
if written and not wrote:
    print(f"{cli}: error: cannot write its store {store}", file=sys.stderr)
    sys.exit(4)
if not source:
    print(f"{cli}: error: no credential for this model", file=sys.stderr)
    sys.exit(3)
print(f"{cli}: authenticated by {source}")
'''


def digest(value: str) -> str:
    """The digest the fake reports for ``value``."""
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Container:
    """A stand-in for one launch container's filesystem, and the fake CLIs' place.

    ``root`` holds ``home/agent`` (empty, mode 0700, as the create's tmpfs
    is), ``tmp``, ``workspace`` and ``cache``; ``bin`` the fake executables,
    each writing its report to ``report``. A replacement launch is a new
    ``Container``, which may share ``workspace`` and ``cache`` with an earlier
    one to stand for what outlives a container.
    """

    root: Path
    bin: Path
    report: Path

    @classmethod
    def make(
        cls, base: Path, *, workspace: Path | None = None, cache: Path | None = None
    ) -> Container:
        base.mkdir(parents=True)
        root = base / "root"
        (root / "home" / "agent").mkdir(parents=True, mode=0o700)
        (root / "tmp").mkdir()
        for name, shared in (("workspace", workspace), ("cache", cache)):
            if shared is None:
                (root / name).mkdir()
            else:
                (root / name).symlink_to(shared, target_is_directory=True)
        bindir = base / "bin"
        bindir.mkdir()
        report = base / "report.json"
        for cli in CLIS:
            # The interpreter straight from the ``#!`` line, with no shell in
            # between, so the fake's environment is exactly the launch's.
            exe = bindir / cli
            exe.write_text(
                f"#!{sys.executable} -I\n"
                f"REPORT, ROOT, CLI = {str(report)!r}, {str(root)!r}, {cli!r}\n{_SCRIPT}",
                encoding="utf-8",
            )
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        return cls(root, bindir, report)

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    def path(self, inside: str) -> Path:
        """Where ``inside``, an absolute path in the container, is on the test's host."""
        return self.root / inside.lstrip("/")

    def argv(self, command: list[str]) -> list[str]:
        """``command``, an adapter's argv, with the fake in place of the CLI."""
        cli = os.path.basename(command[0])
        assert cli in CLIS, command[0]
        return [str(self.bin / cli), *command[1:]]

    def read_report(self) -> dict[str, object] | None:
        if not self.report.exists():
            return None
        return json.loads(self.report.read_text(encoding="utf-8"))


def store_login(container_path: Path, cli: str, provider: str, value: str) -> None:
    """Leave a login of ``cli`` for ``provider`` holding ``value`` at ``container_path``,
    in the shape that fake reads: a directory for ``claude`` and ``pi``, a file for
    ``opencode``."""
    if cli == "claude":
        target, data = container_path / ".credentials.json", {"claudeAiOauth": value}
    elif cli == "pi":
        target, data = container_path / "auth.json", {provider: {"type": "api_key", "key": value}}
    else:
        target, data = container_path, {provider: value}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data), encoding="utf-8")
