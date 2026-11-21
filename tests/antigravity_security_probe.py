"""The Antigravity CLI (``agy``) security isolation probe: issue #529's evidence.

No test, and not part of the package. Issue #529 is a G0 security feasibility
spike (EPIC #527): it measures authentication routes, configuration and state
surfaces, environment inheritance, shell startup, repository and home
customizations, tool permissions, and sandbox isolation on a pinned ``agy``
binary, and builds no production adapter.

Re-measure on another ``agy`` build::

    python3 -I tests/antigravity_security_probe.py record --agy ~/.local/bin/agy \\
        --out tests/fixtures/antigravity-security/1.5.13

**No model is called, and ``agy`` is given no real credential.** Every scenario
reuses :mod:`tests.antigravity_probe`'s private-root ``bwrap`` sandbox
(:func:`~tests.antigravity_probe.sandbox_argv`), own anonymous session keyring
(:data:`~tests.antigravity_probe.KEYRING_LAUNCHER`), bounded stdout record pipe
(:func:`~tests.antigravity_probe.run_sandboxed`), and loopback Gemini API
stand-in reached through a synthetic ``GEMINI_API_KEY`` and
``GOOGLE_GEMINI_BASE_URL``. The binary's SHA-256 is checked before the first
run and after the last; nothing is written if it changes.

Linux only (``bwrap`` with unprivileged user namespaces, ``keyctl(2)``,
``/proc``, ``/usr/bin/python3``). Standard library plus
:mod:`tests.antigravity_probe` only; imports nothing of ``src/autoforge/``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.server
import json
import os
import re
import shutil
import signal
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Support direct execution (`python3 -I tests/antigravity_security_probe.py ...`)
# as well as package import (`from tests import antigravity_security_probe`).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import antigravity_probe as base_probe
else:
    from . import antigravity_probe as base_probe

ISOLATION = base_probe.ISOLATION
SYNTHETIC_KEY = base_probe.SYNTHETIC_KEY
GEMINI_SETTINGS = base_probe.GEMINI_SETTINGS
SANDBOX_PYTHON = base_probe.SANDBOX_PYTHON
SANDBOX_AGY = base_probe.SANDBOX_AGY
SANDBOX_SEC_PROBE = base_probe.SANDBOX_DIR + "/antigravity_security_probe.py"
UUID = base_probe.UUID
OAUTH_QUERY = base_probe.OAUTH_QUERY
LINE_LIMIT = base_probe.LINE_LIMIT
EXCERPT_LIMIT = 800
EPHEMERAL_LOCALHOST = re.compile(r"(?:127\.0\.0\.1|localhost):\d+")
ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

GIT = base_probe.GIT
GIT_ENV = {
    **base_probe.GIT_ENV,
    "GIT_AUTHOR_NAME": "Probe",
    "GIT_AUTHOR_EMAIL": "probe@example.invalid",
    "GIT_COMMITTER_NAME": "Probe",
    "GIT_COMMITTER_EMAIL": "probe@example.invalid",
}

FIXED_ARGV = base_probe.fixed_argv()
SJ_PROMPT = base_probe.stream_message("do it")


@dataclass(frozen=True)
class SecurityScenario:
    """One isolated security-probe run of ``agy``."""

    name: str
    argv: list[str]
    steps: list[dict[str, Any]] = field(default_factory=lambda: [{"kind": "text", "text": "DONE"}])
    stdin: str | None = None
    cwd: str = "{work}"
    git_inits: list[str] = field(default_factory=list)
    make_worktree: bool = False
    files: list[dict[str, str]] = field(default_factory=list)
    symlinks: list[dict[str, str]] = field(default_factory=list)
    env_extra: dict[str, str] = field(default_factory=dict)
    key: bool = True
    base_url: bool = True
    set_xdg: bool = True
    watch_files: list[str] = field(default_factory=list)
    watch_system: list[str] = field(default_factory=list)
    timeout: float = 25.0
    note: str = ""


def _sj_settings(settings: dict[str, Any], *extra: str) -> list[str]:
    return [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--settings",
        json.dumps(settings, separators=(",", ":")),
        *extra,
    ]


def _hook(marker: str, cmd: str | None = None) -> str:
    body = cmd or f"echo ran > {{work}}/{marker}"
    return json.dumps({"p": {"SessionStart": [{"type": "command", "command": body}]}})


def _mcp(marker: str, cmd: str | None = None) -> str:
    body = cmd or f"echo ran > {{work}}/{marker}"
    return json.dumps({"mcpServers": {"p": {"command": "sh", "args": ["-c", body]}}})


def _rule(marker: str) -> str:
    return f"---\ntrigger: always_on\n---\n{marker}\n"


def _cmd_step(command_line: str, summary: str = "Run") -> dict[str, Any]:
    return {
        "kind": "call",
        "name": "run_command",
        "args": {
            "CommandLine": command_line,
            "toolSummary": summary,
            "WaitMsBeforeAsync": 5000,
        },
    }


def _write_step(path: str, content: str, *, overwrite: bool = False) -> dict[str, Any]:
    args: dict[str, Any] = {"Path": path, "Content": content}
    if overwrite:
        args["Overwrite"] = True
    return {"kind": "call", "name": "write_file", "args": args}


def _edit_step(path: str, target: str, replacement: str) -> dict[str, Any]:
    return {
        "kind": "call",
        "name": "edit_file",
        "args": {"Path": path, "Target": target, "Replacement": replacement},
    }


def _read_step(path: str) -> dict[str, Any]:
    return {"kind": "call", "name": "read_file", "args": {"Path": path}}


def scenarios() -> list[SecurityScenario]:
    s: list[SecurityScenario] = []
    add = s.append

    # -- 1. Discovery, disk writes, XDG, and auth routes ----------------------
    add(SecurityScenario("ro-version", ["--version"], steps=[], key=False, base_url=False))
    add(SecurityScenario("ro-help", ["--help"], steps=[], key=False, base_url=False))
    add(SecurityScenario("ro-mcp-list", ["mcp", "list"], steps=[], key=False, base_url=False))
    add(SecurityScenario("ro-plugin-list", ["plugin", "list"], steps=[], key=False, base_url=False))
    add(
        SecurityScenario(
            "ro-remote-control-status",
            ["remote-control", "status"],
            steps=[],
            key=False,
            base_url=False,
        )
    )
    add(SecurityScenario("rw-agent-list", ["agent"], steps=[], key=False, base_url=False))
    add(
        SecurityScenario(
            "rw-config-catalogue",
            ["-p", "/config", "--output-format", "json", "--settings", GEMINI_SETTINGS],
            steps=[],
            key=True,
            base_url=False,
        )
    )
    add(
        SecurityScenario(
            "auth-google-api-key-only",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            key=False,
            env_extra={"GOOGLE_API_KEY": "synthetic-google-api-key"},
        )
    )
    add(
        SecurityScenario(
            "auth-gemini-key-without-provider",
            [
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--dangerously-skip-permissions",
            ],
            stdin=SJ_PROMPT,
            key=True,
            base_url=True,
        )
    )
    add(
        SecurityScenario(
            "auth-adc-synthetic",
            [
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--dangerously-skip-permissions",
            ],
            stdin=SJ_PROMPT,
            key=False,
            base_url=False,
            files=[
                {
                    "path": "{base}/adc.json",
                    "content": json.dumps(
                        {
                            "type": "authorized_user",
                            "client_id": "synthetic-client-id",
                            "client_secret": "GOCSPX-SyntheticNotRealSecret1234",
                            "refresh_token": "1//synthetic-not-real-refresh",
                        }
                    ),
                }
            ],
            env_extra={
                "AGY_ADC_AUTH": "true",
                "GOOGLE_APPLICATION_CREDENTIALS": "{base}/adc.json",
            },
        )
    )
    add(
        SecurityScenario(
            "auth-models-no-key",
            ["models"],
            steps=[],
            key=False,
            base_url=False,
            files=[
                {
                    "path": "{home}/.gemini/antigravity-cli/settings.json",
                    "content": GEMINI_SETTINGS,
                }
            ],
        )
    )
    add(
        SecurityScenario(
            "auth-models-bogus-key-offline",
            ["models"],
            steps=[],
            key=True,
            base_url=False,
            files=[
                {
                    "path": "{home}/.gemini/antigravity-cli/settings.json",
                    "content": GEMINI_SETTINGS,
                }
            ],
            note="proves `agy models` succeeds offline with a bogus key (false positive)",
        )
    )
    add(
        SecurityScenario(
            "state-xdg-vs-home",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            env_extra={
                "XDG_CONFIG_HOME": "{base}/xdg-cfg",
                "XDG_CACHE_HOME": "{base}/xdg-cache",
                "XDG_DATA_HOME": "{base}/xdg-data",
                "XDG_STATE_HOME": "{base}/xdg-state",
            },
            steps=[
                _cmd_step(
                    "find {base}/xdg-* -type f 2>/dev/null | sort > {work}/xdg-files.txt || true",
                    "Check XDG",
                ),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=["{work}/xdg-files.txt"],
        )
    )

    # -- 2. Environment inheritance, child_env.py & shell startup -------------
    child_env = {
        "GH_CONFIG_DIR": "{base}/empty-gh",
        "GH_TOKEN": "autoforge-no-github-credential",
        "GH_ENTERPRISE_TOKEN": "autoforge-no-github-credential",
        "GIT_CONFIG_GLOBAL": "{base}/child-gitconfig",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "core.askPass",
        "GIT_CONFIG_VALUE_1": "",
        "GIT_CONFIG_KEY_2": "core.sshCommand",
        "GIT_CONFIG_VALUE_2": "ssh -F /dev/null -o IdentityFile=none",
        "GIT_CONFIG_KEY_3": "http.extraHeader",
        "GIT_CONFIG_VALUE_3": "",
        "CURL_HOME": "{base}/curl",
        "WGETRC": "{base}/wgetrc",
        "WGET2RC": "{base}/wgetrc",
        "ZDOTDIR": "{base}/zsh",
        "SHELL": "/bin/bash",
    }
    inspect_py = (
        "import json, os; "
        "d = dict(sorted(os.environ.items())); "
        'open("{work}/env-dump.json","w").write(json.dumps({"keys": list(d), "vals": d}, indent=1))'
    )
    add(
        SecurityScenario(
            "env-child-overrides-and-shell-startup",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            env_extra=child_env,
            files=[
                {
                    "path": "{home}/.bashrc",
                    "content": "export CANARY_BASHRC=1\nexport GH_TOKEN=pwned\n",
                },
                {
                    "path": "{home}/.bash_profile",
                    "content": "export CANARY_BASH_PROFILE=1\nexport GH_TOKEN=pwned\n",
                },
                {
                    "path": "{home}/.profile",
                    "content": "export CANARY_PROFILE=1\nexport GH_TOKEN=pwned\n",
                },
                {
                    "path": "{home}/.zshenv",
                    "content": "export CANARY_ZSHENV=1\nexport GH_TOKEN=pwned\n",
                },
                {
                    "path": "{base}/child-gitconfig",
                    "content": "[user]\n\tname = Probe\n\temail = probe@example.invalid\n",
                },
                {"path": "{base}/curl/.curlrc", "content": "# empty\n"},
                {"path": "{base}/wgetrc", "content": "# empty\n"},
            ],
            steps=[
                _cmd_step(
                    f"python3 -c '{inspect_py}' && "
                    'echo "FLAGS=$- ARG0=$0" > {work}/shell-info.txt',
                    "Inspect child env",
                ),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=["{work}/env-dump.json", "{work}/shell-info.txt"],
        )
    )
    add(
        SecurityScenario(
            "env-hook-and-mcp-inherit-key",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            env_extra={"GH_TOKEN": "autoforge-no-github-credential"},
            files=[
                {
                    "path": "{work}/.agents/hooks.json",
                    "content": _hook(
                        "m-hook-env",
                        'echo "KEY=$GEMINI_API_KEY GH=$GH_TOKEN" > {work}/m-hook-env',
                    ),
                },
                {
                    "path": "{work}/.agents/mcp_config.json",
                    "content": _mcp(
                        "m-mcp-env",
                        'echo "KEY=$GEMINI_API_KEY GH=$GH_TOKEN" > {work}/m-mcp-env',
                    ),
                },
            ],
            watch_files=["{work}/m-hook-env", "{work}/m-mcp-env"],
        )
    )

    # -- 3. Integration isolation canaries (#528 C1 + #529) -------------------
    for dname in (".agents", ".agent", "_agents", "_agent"):
        slug = dname.replace(".", "dot-").replace("_", "under-")
        add(
            SecurityScenario(
                f"custom-dir-{slug}",
                FIXED_ARGV,
                stdin=SJ_PROMPT,
                git_inits=["{work}"],
                files=[
                    {"path": f"{{work}}/{dname}/hooks.json", "content": _hook("m-hook")},
                    {"path": f"{{work}}/{dname}/mcp_config.json", "content": _mcp("m-mcp")},
                    {
                        "path": f"{{work}}/{dname}/rules/r.md",
                        "content": _rule(f"RULE-{slug}"),
                    },
                ],
                watch_files=["{work}/m-hook", "{work}/m-mcp"],
                watch_system=[f"RULE-{slug}"],
            )
        )

    add(
        SecurityScenario(
            "custom-plugins-manifests-and-http-mcp",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=[
                {
                    "path": "{work}/.agents/plugins/p1/plugin.json",
                    "content": json.dumps({"name": "p1"}),
                },
                {
                    "path": "{work}/.agents/plugins/p1/hooks.json",
                    "content": _hook("m-plugin-hook"),
                },
                {
                    "path": "{work}/.agents/plugins/p1/mcp_config.json",
                    "content": _mcp("m-plugin-mcp"),
                },
                {
                    "path": "{work}/.agents/plugins/p1/rules/AGENTS.md",
                    "content": "PLUGIN-RULE-MARKER\n",
                },
                {
                    "path": "{work}/.agents/plugins.json",
                    "content": json.dumps({"entries": [{"path": "ext_plugins"}]}),
                },
                {
                    "path": "{work}/ext_plugins/p2/plugin.json",
                    "content": json.dumps({"name": "p2"}),
                },
                {
                    "path": "{work}/ext_plugins/p2/hooks.json",
                    "content": _hook("m-ext-plugin-hook"),
                },
                {
                    "path": "{work}/.agents/skills.json",
                    "content": json.dumps({"entries": [{"path": "ext_skills"}]}),
                },
                {
                    "path": "{work}/ext_skills/s1/SKILL.md",
                    "content": "---\nname: ext-skill\ndescription: EXT-SKILL-MARKER\n---\nbody\n",
                },
                {
                    "path": "{work}/.agents/rules.json",
                    "content": json.dumps({"entries": [{"path": "ext_rules"}]}),
                },
                {
                    "path": "{work}/ext_rules/r.md",
                    "content": _rule("EXT-RULE-MARKER"),
                },
                {
                    "path": "{work}/.agents/agents/custom.md",
                    "content": (
                        "---\nname: custom-agent\ndescription: CUSTOM-AGENT-MARKER\n---\nbody\n"
                    ),
                },
                {
                    "path": "{work}/.agents/mcp_config.json",
                    "content": json.dumps(
                        {
                            "mcpServers": {
                                "http-canary": {"url": "http://127.0.0.1:{port}/mcp-canary"}
                            }
                        }
                    ),
                },
            ],
            watch_files=[
                "{work}/m-plugin-hook",
                "{work}/m-plugin-mcp",
                "{work}/m-ext-plugin-hook",
            ],
            watch_system=[
                "PLUGIN-RULE-MARKER",
                "EXT-SKILL-MARKER",
                "EXT-RULE-MARKER",
                "CUSTOM-AGENT-MARKER",
            ],
        )
    )

    add(
        SecurityScenario(
            "custom-traversal-cwd-below-repo-root",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            cwd="{work}/a/b",
            git_inits=["{work}"],
            files=[
                {"path": "{work}/.agents/hooks.json", "content": _hook("m-root-hook")},
                {"path": "{work}/.agents/mcp_config.json", "content": _mcp("m-root-mcp")},
                {"path": "{work}/.agents/rules/r.md", "content": _rule("SUB-ROOT-RULE")},
                {"path": "{work}/AGENTS.md", "content": "SUB-ROOT-AGENTS\n"},
                {"path": "{work}/a/.agents/hooks.json", "content": _hook("m-mid-hook")},
                {"path": "{work}/a/AGENTS.md", "content": "SUB-MID-AGENTS\n"},
            ],
            watch_files=["{work}/m-root-hook", "{work}/m-root-mcp", "{work}/m-mid-hook"],
            watch_system=["SUB-ROOT-RULE", "SUB-ROOT-AGENTS", "SUB-MID-AGENTS"],
        )
    )
    add(
        SecurityScenario(
            "custom-traversal-above-repo-root",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=[
                {"path": "{base}/.agents/hooks.json", "content": _hook("m-above-hook")},
                {"path": "{base}/AGENTS.md", "content": "ABOVE-AGENTS-MARKER\n"},
            ],
            watch_files=["{work}/m-above-hook"],
            watch_system=["ABOVE-AGENTS-MARKER"],
        )
    )
    add(
        SecurityScenario(
            "custom-traversal-below-cwd",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=[
                {"path": "{work}/sub/.agents/hooks.json", "content": _hook("m-below-hook")},
                {"path": "{work}/sub/AGENTS.md", "content": "BELOW-AGENTS-MARKER\n"},
            ],
            watch_files=["{work}/m-below-hook"],
            watch_system=["BELOW-AGENTS-MARKER"],
        )
    )
    add(
        SecurityScenario(
            "custom-traversal-git-worktree",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            make_worktree=True,
            files=[
                {
                    "path": "{base}/parent_repo/.agents/hooks.json",
                    "content": _hook("m-wt-hook", "echo ran > {base}/m-wt-hook"),
                },
                {
                    "path": "{base}/parent_repo/AGENTS.md",
                    "content": "WT-PARENT-AGENTS\n",
                },
            ],
            watch_files=["{base}/m-wt-hook"],
            watch_system=["WT-PARENT-AGENTS"],
        )
    )
    add(
        SecurityScenario(
            "custom-symlink-inside-workspace",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=[
                {"path": "{work}/real_agents/hooks.json", "content": _hook("m-symin-hook")},
                {"path": "{work}/real_agents/mcp_config.json", "content": _mcp("m-symin-mcp")},
            ],
            symlinks=[{"link": "{work}/.agents", "target": "real_agents"}],
            watch_files=["{work}/m-symin-hook", "{work}/m-symin-mcp"],
        )
    )
    add(
        SecurityScenario(
            "custom-symlink-file-inside-workspace",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=[{"path": "{work}/real_hooks.json", "content": _hook("m-symfile-hook")}],
            symlinks=[{"link": "{work}/.agents/hooks.json", "target": "../real_hooks.json"}],
            watch_files=["{work}/m-symfile-hook"],
        )
    )
    add(
        SecurityScenario(
            "custom-symlink-outside-workspace",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=[
                {"path": "{base}/elsewhere/hooks.json", "content": _hook("m-symout-hook")},
                {"path": "{base}/elsewhere/mcp_config.json", "content": _mcp("m-symout-mcp")},
            ],
            symlinks=[{"link": "{work}/.agents", "target": "{base}/elsewhere"}],
            watch_files=["{work}/m-symout-hook", "{work}/m-symout-mcp"],
        )
    )
    add(
        SecurityScenario(
            "custom-global-home",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=[
                {"path": "{home}/.gemini/config/hooks.json", "content": _hook("m-global-hook")},
                {
                    "path": "{home}/.gemini/config/mcp_config.json",
                    "content": _mcp("m-global-mcp"),
                },
                {"path": "{home}/.gemini/config/AGENTS.md", "content": "GLOBAL-AGENTS-MARKER\n"},
                {"path": "{home}/.gemini/GEMINI.md", "content": "HOME-GEMINI-MD-MARKER\n"},
                {"path": "{home}/.agents/hooks.json", "content": _hook("m-home-dotagents-hook")},
            ],
            watch_files=[
                "{work}/m-global-hook",
                "{work}/m-global-mcp",
                "{work}/m-home-dotagents-hook",
            ],
            watch_system=["GLOBAL-AGENTS-MARKER", "HOME-GEMINI-MD-MARKER"],
        )
    )
    add(
        SecurityScenario(
            "custom-add-dir-widening",
            [*FIXED_ARGV, "--add-dir", "{base}/extra"],
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=[
                {"path": "{base}/extra/.agents/hooks.json", "content": _hook("m-adddir-hook")},
                {"path": "{base}/extra/AGENTS.md", "content": "ADDDIR-AGENTS-MARKER\n"},
            ],
            watch_files=["{work}/m-adddir-hook"],
            watch_system=["ADDDIR-AGENTS-MARKER"],
        )
    )
    dyn_hooks = json.dumps(
        {
            "dyn": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {"type": "command", "command": "echo ran > {work}/m-dyn-pretool"}
                        ],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {"type": "command", "command": "echo ran > {work}/m-dyn-posttool"}
                        ],
                    }
                ],
                "PreInvocation": [{"type": "command", "command": "echo ran > {work}/m-dyn-preinv"}],
                "PostInvocation": [
                    {"type": "command", "command": "echo ran > {work}/m-dyn-postinv"}
                ],
                "Stop": [{"type": "command", "command": "echo ran > {work}/m-dyn-stop"}],
            }
        }
    )
    add(
        SecurityScenario(
            "custom-mid-session-write-hooks",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[
                _write_step("{work}/.agents/hooks.json", dyn_hooks, overwrite=True),
                _cmd_step("echo step2 > {work}/m-step2", "Step 2"),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=[
                "{work}/.agents/hooks.json",
                "{work}/m-step2",
                "{work}/m-dyn-pretool",
                "{work}/m-dyn-posttool",
                "{work}/m-dyn-preinv",
                "{work}/m-dyn-postinv",
                "{work}/m-dyn-stop",
            ],
        )
    )
    add(
        SecurityScenario(
            "custom-foreign-configs-ignored",
            FIXED_ARGV,
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=[
                {"path": "{work}/.mcp.json", "content": _mcp("m-dotmcp")},
                {"path": "{work}/.gemini/hooks.json", "content": _hook("m-dotgemini-hook")},
                {
                    "path": "{work}/.antigravity/settings.json",
                    "content": json.dumps({"toolPermission": "always-proceed"}),
                },
                {
                    "path": "{work}/.claude/settings.json",
                    "content": json.dumps(
                        {
                            "hooks": {
                                "SessionStart": [
                                    {
                                        "hooks": [
                                            {
                                                "type": "command",
                                                "command": "echo ran > {work}/m-claude-hook",
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    ),
                },
                {"path": "{work}/CLAUDE.md", "content": "CLAUDE-MD-MARKER\n"},
            ],
            watch_files=["{work}/m-dotmcp", "{work}/m-dotgemini-hook", "{work}/m-claude-hook"],
            watch_system=["CLAUDE-MD-MARKER"],
        )
    )
    add(
        SecurityScenario(
            "custom-slash-config-without-disable",
            base_probe.sj_run(),
            stdin=base_probe.stream_message("/config"),
        )
    )
    add(
        SecurityScenario(
            "custom-slash-config-with-disable",
            base_probe.sj_run("--disable-slash-commands"),
            stdin=base_probe.stream_message("/config"),
        )
    )

    # -- 4. Tools, permission modes & explicit rules --------------------------
    base_files = [
        {"path": "{work}/target.txt", "content": "OLD\n"},
        {"path": "{base}/outside.txt", "content": "OUTSIDE-SECRET\n"},
    ]
    add(
        SecurityScenario(
            "perm-default-read-outside",
            base_probe.sj_run(),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=base_files,
            steps=[_read_step("{base}/outside.txt"), {"kind": "text", "text": "DONE"}],
        )
    )
    add(
        SecurityScenario(
            "perm-default-write-outside",
            base_probe.sj_run(),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=base_files,
            steps=[
                _write_step("{base}/written-outside.txt", "outside"),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=["{base}/written-outside.txt"],
        )
    )
    add(
        SecurityScenario(
            "perm-default-edit-outside",
            base_probe.sj_run(),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            files=base_files,
            steps=[
                _edit_step("{base}/outside.txt", "OUTSIDE-SECRET", "EDITED-OUTSIDE"),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=["{base}/outside.txt"],
        )
    )
    add(
        SecurityScenario(
            "perm-default-cmd-denied",
            base_probe.sj_run(),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[_cmd_step("git status --short"), {"kind": "text", "text": "DONE"}],
        )
    )
    add(
        SecurityScenario(
            "perm-mode-plan-write-outside",
            base_probe.sj_run("--mode", "plan"),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[
                _write_step("{base}/written-outside.txt", "plan-write"),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=["{base}/written-outside.txt"],
        )
    )
    add(
        SecurityScenario(
            "perm-mode-bogus-warns-and-runs",
            base_probe.sj_run("--mode", "bogus"),
            stdin=SJ_PROMPT,
        )
    )
    add(
        SecurityScenario(
            "perm-strict-honors-allow",
            _sj_settings(
                {
                    "modelProvider": "gemini",
                    "toolPermission": "strict",
                    "allowNonWorkspaceAccess": False,
                    "permissions": {
                        "allow": ["command(git status --short)", "command(*)"],
                    },
                }
            ),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[_cmd_step("git status --short"), {"kind": "text", "text": "DONE"}],
        )
    )
    add(
        SecurityScenario(
            "perm-strict-denies-write",
            _sj_settings(
                {
                    "modelProvider": "gemini",
                    "toolPermission": "strict",
                    "allowNonWorkspaceAccess": False,
                }
            ),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[
                _write_step("{work}/blocked.txt", "x"),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=["{work}/blocked.txt"],
        )
    )
    add(
        SecurityScenario(
            "perm-rr-allow-git-local",
            _sj_settings(
                {
                    "modelProvider": "gemini",
                    "toolPermission": "request-review",
                    "allowNonWorkspaceAccess": False,
                    "permissions": {"allow": ["command(regex:^git (status|diff|add|commit)\\b)"]},
                }
            ),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[_cmd_step("git status --short"), {"kind": "text", "text": "DONE"}],
        )
    )
    add(
        SecurityScenario(
            "perm-rr-deny-unlisted-git-push",
            _sj_settings(
                {
                    "modelProvider": "gemini",
                    "toolPermission": "request-review",
                    "allowNonWorkspaceAccess": False,
                    "permissions": {"allow": ["command(regex:^git (status|diff|add|commit)\\b)"]},
                }
            ),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[_cmd_step("git push origin main"), {"kind": "text", "text": "DONE"}],
        )
    )
    add(
        SecurityScenario(
            "perm-rr-blocks-shell-chain",
            _sj_settings(
                {
                    "modelProvider": "gemini",
                    "toolPermission": "request-review",
                    "allowNonWorkspaceAccess": False,
                    "permissions": {"allow": ["command(regex:^git (status|diff|add|commit)\\b)"]},
                }
            ),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[
                _cmd_step("git status --short && echo CHAINED > {work}/chained.txt"),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=["{work}/chained.txt"],
        )
    )
    add(
        SecurityScenario(
            "perm-always-proceed-honors-deny",
            _sj_settings(
                {
                    "modelProvider": "gemini",
                    "toolPermission": "always-proceed",
                    "allowNonWorkspaceAccess": False,
                    "permissions": {"deny": ["command(curl)"]},
                }
            ),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[_cmd_step("curl http://127.0.0.1:1/"), {"kind": "text", "text": "DONE"}],
        )
    )
    add(
        SecurityScenario(
            "perm-skip-flag-honors-deny",
            _sj_settings(
                {
                    "modelProvider": "gemini",
                    "toolPermission": "request-review",
                    "allowNonWorkspaceAccess": False,
                    "permissions": {"deny": ["command(curl)"]},
                },
                "--dangerously-skip-permissions",
            ),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[_cmd_step("curl http://127.0.0.1:1/"), {"kind": "text", "text": "DONE"}],
        )
    )
    add(
        SecurityScenario(
            "perm-deny-bypassed-by-subshell",
            _sj_settings(
                {
                    "modelProvider": "gemini",
                    "toolPermission": "always-proceed",
                    "allowNonWorkspaceAccess": False,
                    "permissions": {"deny": ["command(curl)"]},
                }
            ),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[
                _cmd_step("sh -c 'echo BYPASSED > {work}/bypass.txt'"),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=["{work}/bypass.txt"],
        )
    )
    add(
        SecurityScenario(
            "perm-invalid-re2-regex-silently-ignored",
            _sj_settings(
                {
                    "modelProvider": "gemini",
                    "toolPermission": "request-review",
                    "allowNonWorkspaceAccess": False,
                    "permissions": {"deny": ["write_file(regex:^(?!{work}/))"]},
                }
            ),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[
                _write_step("{base}/written-outside.txt", "bypassed-invalid-regex"),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=["{base}/written-outside.txt"],
        )
    )
    add(
        SecurityScenario(
            "perm-unknown-nested-key-silently-ignored",
            _sj_settings(
                {
                    "modelProvider": "gemini",
                    "permissions": {"noSuchRuleList": ["command(*)"]},
                }
            ),
            stdin=SJ_PROMPT,
        )
    )
    add(
        SecurityScenario(
            "perm-native-sandbox-flag",
            base_probe.sj_run("--sandbox", "--dangerously-skip-permissions"),
            stdin=SJ_PROMPT,
            git_inits=["{work}"],
            steps=[
                _cmd_step("echo sandboxed > {work}/sbx-ran.txt"),
                {"kind": "text", "text": "DONE"},
            ],
            watch_files=["{work}/sbx-ran.txt"],
        )
    )
    return s


SCENARIOS = scenarios()
SCENARIO_NAMED = {sc.name: sc for sc in SCENARIOS}


# -- inner half (runs inside private-root bwrap with own session keyring) -----


def _small_text(path: Path, limit: int = 8192) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit).decode("utf-8", "replace")
    finally:
        os.close(fd)


def _git(directory: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(
        [GIT, "-C", str(directory), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=True,
    )


def _candidate(parts: list[dict[str, Any]]) -> bytes:
    body = {
        "candidates": [
            {"content": {"role": "model", "parts": parts}, "index": 0, "finishReason": "STOP"}
        ],
        "modelVersion": "stand-in",
        "usageMetadata": {
            "promptTokenCount": 11,
            "candidatesTokenCount": 7,
            "totalTokenCount": 18,
        },
    }
    return ("data: " + json.dumps(body, ensure_ascii=False) + "\r\n\r\n").encode()


def inner(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    problem = base_probe.own_session_keyring(spec["keyring"])
    if problem:
        sys.stderr.write(f"security-probe: {problem}: refused, nothing started\n")
        return 3

    sc = SecurityScenario(**spec["scenario"])
    home = Path(spec["home"])
    work = Path(spec["work"])
    base = work.parent
    git_env = {**GIT_ENV, "HOME": str(home)}

    def sub(text: str, *, cwd_str: str = "", port: int = 0) -> str:
        out = (
            text.replace("{base}", str(base))
            .replace("{work}", str(work))
            .replace("{home}", str(home))
        )
        if cwd_str:
            out = out.replace("{cwd}", cwd_str)
        if port:
            out = out.replace("{port}", str(port))
        return out

    cwd = Path(sub(sc.cwd))
    for item in sc.git_inits:
        repo_dir = Path(sub(item))
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [GIT, "init", "-q", "--template=", str(repo_dir)],
            env=git_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=True,
        )

    if sc.make_worktree:
        parent_repo = base / "parent_repo"
        parent_repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [GIT, "init", "-q", "--template=", str(parent_repo)],
            env=git_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=True,
        )
        (parent_repo / "README.md").write_text("hi\n", encoding="utf-8")
        _git(parent_repo, git_env, "add", "README.md")
        _git(parent_repo, git_env, "commit", "-q", "-m", "init")
        wt = parent_repo / ".git" / "autoforge" / "worktrees" / "1"
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(parent_repo, git_env, "worktree", "add", "-q", "--detach", str(wt))
        cwd = wt

    cwd.mkdir(parents=True, exist_ok=True)
    cwd_str = str(cwd)

    state: dict[str, Any] = {"requests": [], "other_requests": [], "lock": threading.Lock()}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def _other(self, method: str) -> None:
            size = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(size) if size else b""
            with state["lock"]:
                state["other_requests"].append(
                    {
                        "method": method,
                        "path": self.path.split("?")[0],
                        "body_excerpt": raw.decode("utf-8", "replace")[:200],
                    }
                )
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self._other("GET")

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if "GenerateContent" not in path and "generateContent" not in path:
                self._other("POST")
                return
            size = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(size) if size else b""
            body = json.loads(raw) if raw else {}
            sys_text = "".join(
                p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts", [])
            )
            tools = [
                d.get("name")
                for t in body.get("tools") or []
                for d in t.get("functionDeclarations", [])
            ]
            # Answer auxiliary (tool-less / title) model calls without consuming a main step
            if "conversation title generator" in sys_text or (sc.steps and not tools):
                self._reply([{"text": "aux"}])
                return
            with state["lock"]:
                idx = len(state["requests"])
                contents = body.get("contents") or []
                tool_results = [
                    json.dumps(p["functionResponse"].get("response"), ensure_ascii=False)[
                        :EXCERPT_LIMIT
                    ]
                    for c in contents
                    for p in c.get("parts", [])
                    if "functionResponse" in p
                ]
                user_text = "".join(
                    p.get("text", "") for p in (contents[0].get("parts", []) if contents else [])
                )
                state["requests"].append(
                    {
                        "model_path": path,
                        "key": "synthetic"
                        if self.headers.get("x-goog-api-key") == SYNTHETIC_KEY
                        else ("other" if self.headers.get("x-goog-api-key") else "absent"),
                        "system_markers": [m for m in sc.watch_system if m in sys_text],
                        "user_text_excerpt": user_text[:EXCERPT_LIMIT],
                        "tool_results": tool_results,
                    }
                )
            step = (
                sc.steps[min(idx, len(sc.steps) - 1)]
                if sc.steps
                else {"kind": "text", "text": "DONE"}
            )
            if step["kind"] == "call":
                args = json.loads(sub(json.dumps(step["args"]), cwd_str=cwd_str))
                parts = [{"functionCall": {"name": step["name"], "args": args}}]
            else:
                parts = [{"text": step.get("text", "DONE")}]
            self._reply(parts)

        def _reply(self, parts: list[dict[str, Any]]) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(_candidate(parts))
            self.wfile.flush()
            self.close_connection = True

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    for item in sc.files:
        fp = Path(sub(item["path"], cwd_str=cwd_str, port=port))
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(sub(item["content"], cwd_str=cwd_str, port=port), encoding="utf-8")

    for item in sc.symlinks:
        link = Path(sub(item["link"], cwd_str=cwd_str, port=port))
        target = sub(item["target"], cwd_str=cwd_str, port=port)
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)

    env: dict[str, str] = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }
    if sc.set_xdg:
        env.update(
            {
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "XDG_STATE_HOME": str(home / ".local/state"),
            }
        )
    for k, v in sc.env_extra.items():
        env[k] = sub(v, cwd_str=cwd_str, port=port)
    if sc.key and "GEMINI_API_KEY" not in sc.env_extra:
        env["GEMINI_API_KEY"] = SYNTHETIC_KEY
    if sc.base_url:
        env["GOOGLE_GEMINI_BASE_URL"] = f"http://127.0.0.1:{port}"

    argv = [sub(a, cwd_str=cwd_str, port=port) for a in sc.argv]
    stdin_bytes = sc.stdin.encode("utf-8") if sc.stdin is not None else None

    started = time.monotonic()
    proc = subprocess.Popen(
        [spec["agy"], *argv],
        cwd=cwd_str,
        env=env,
        stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    reads = {"stdout": base_probe.StreamRead(), "stderr": base_probe.StreamRead()}
    readers = {
        n: threading.Thread(
            target=base_probe.read_stream, args=(getattr(proc, n), started, reads[n]), daemon=True
        )
        for n in reads
    }
    for t in readers.values():
        t.start()
    if stdin_bytes is not None:

        def _feed() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(stdin_bytes)
                proc.stdin.close()
            except BrokenPipeError:
                pass

        threading.Thread(target=_feed, daemon=True).start()

    killed = False
    try:
        proc.wait(timeout=sc.timeout)
    except subprocess.TimeoutExpired:
        killed = True
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    seconds = round(time.monotonic() - started, 1)
    for t in readers.values():
        t.join(timeout=5)
    server.shutdown()

    capture: dict[str, bool | str | None] = {}
    for n, got in reads.items():
        capture[f"{n}_eof"] = got.eof
        capture[f"{n}_truncated"] = got.truncated
        capture[f"{n}_error"] = got.error

    markers: dict[str, str] = {}
    for m in sc.watch_files:
        mp = Path(sub(m, cwd_str=cwd_str, port=port))
        if os.path.lexists(mp):
            content = _small_text(mp)
            markers[m] = (content or "").strip() or "present"

    record = {
        "argv": argv,
        "exit": proc.returncode,
        "killed_by_probe": killed,
        "capture": capture,
        "session_keyring": "own",
        "seconds": seconds,
        "stdout": list(reads["stdout"].lines),
        "stderr": list(reads["stderr"].lines),
        "requests": state["requests"],
        "other_requests": state["other_requests"],
        "markers": markers,
        "home_files": base_probe._home_files(home),
    }
    sys.stdout.buffer.write((json.dumps(record) + "\n").encode("ascii"))
    sys.stdout.buffer.flush()
    return 0


# -- outer half: sandbox launch, normalization, recording ---------------------


def sandbox_argv(agy: str, base: Path, spec_dir: Path, home: Path, spec_json: Path) -> list[str]:
    """Private-root bwrap launch binding both base probe and security probe."""
    cmd = base_probe.sandbox_argv(agy, base, spec_dir, home, [])
    idx = cmd.index("--")
    prefix = cmd[:idx] + ["--ro-bind", str(Path(__file__).resolve()), SANDBOX_SEC_PROBE]
    return [*prefix, "--", SANDBOX_PYTHON, "-I", "-S", SANDBOX_SEC_PROBE, "inner", str(spec_json)]


def normalize(record: dict[str, Any], root: Path, sc: SecurityScenario, agy: str) -> dict[str, Any]:
    text = json.dumps(record, ensure_ascii=False)
    text = text.replace(str(root), "<root>").replace(agy, "agy").replace(SANDBOX_AGY, "agy")
    text = OAUTH_QUERY.sub(lambda m: m.group(1) + "=<redacted>", text)
    text = EPHEMERAL_LOCALHOST.sub("127.0.0.1:<port>", text)
    text = ISO_TS.sub("<timestamp>", text)
    text = UUID.sub("<uuid>", text)
    out = json.loads(text)
    for stream in ("stdout", "stderr"):
        for item in out[stream]:
            # Normalize timestamps inside line tuples too; keep only text determinism
            if len(item[1]) > LINE_LIMIT:
                item[1] = item[1][:LINE_LIMIT] + f"{base_probe.PROBE_CUT}{len(item[1])} chars>"
                out["capture"][f"{stream}_truncated"] = True
    out["scenario"] = sc.name
    out["isolation"] = ISOLATION
    if sc.note:
        out["note"] = sc.note
    return out


def run_one(agy: str, sc: SecurityScenario, root: Path) -> dict[str, Any]:
    base = Path(tempfile.mkdtemp(prefix=f"{sc.name}.", dir=root))
    home, work = base / "home", base / "work"
    home.mkdir()
    work.mkdir()
    spec_dir = Path(tempfile.mkdtemp(prefix=f"spec-{sc.name}.", dir=root))
    keyctl = base_probe.keyctl_number()
    spec = {
        "keyring": {"keyctl": keyctl, "caller": base_probe.session_keyring(keyctl)},
        "scenario": asdict(sc),
        "agy": SANDBOX_AGY,
        "home": str(home),
        "work": str(work),
    }
    spec_json = spec_dir / "spec.json"
    spec_json.write_text(json.dumps(spec), encoding="utf-8")
    cmd = sandbox_argv(agy, base, spec_dir, home, spec_json)
    return normalize(base_probe.run_sandboxed(cmd, sc.timeout + 60), root, sc, agy)


def record_all(agy: str, out: Path, only: set[str] | None = None, workers: int = 8) -> list[str]:
    if shutil.which("bwrap") is None:
        raise SystemExit("bwrap is required")
    if not Path(SANDBOX_PYTHON).is_file():
        raise SystemExit(f"{SANDBOX_PYTHON} is required")
    base_probe.keyctl_number()
    chosen = [sc for sc in SCENARIOS if only is None or sc.name in only]
    identity = base_probe.binary_sha256(agy)
    made: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="agy-sec-probe.") as tmp:
        root = Path(tmp).resolve()
        version, _ = base_probe.discover_version(agy, root)
        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            made = list(pool.map(lambda sc: run_one(agy, sc, root), chosen))
    if base_probe.binary_sha256(agy) != identity:
        raise base_probe.ProbeError("the binary changed during recording: no record written")
    out.mkdir(parents=True, exist_ok=True)
    for rec in made:
        rec["agy_version"] = version
        rec["agy_sha256"] = identity
        (out / f"{rec['scenario']}.json").write_text(base_probe.record_text(rec), encoding="utf-8")
    return [rec["scenario"] for rec in made]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--agy", required=True)
    rec.add_argument("--out", required=True, type=Path)
    rec.add_argument("--only", default="")
    inn = sub.add_parser("inner")
    inn.add_argument("spec")
    args = ap.parse_args()
    if args.mode == "inner":
        return inner(args.spec)
    only = {x for x in args.only.split(",") if x} or None
    names = record_all(str(Path(args.agy).expanduser().resolve()), args.out, only)
    print(f"{len(names)} security records in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
