"""Issue #529's evidence: the Antigravity CLI security probe's records, held to the report.

``tests/antigravity_security_probe.py`` runs ``agy`` against a loopback stand-in
of the Gemini API in a private-root ``bwrap`` sandbox with its own anonymous
session keyring and writes one JSON record per scenario. The 50 records of
``agy`` 1.5.13 are committed under ``tests/fixtures/antigravity-security/1.5.13/``,
and ``docs/antigravity-cli-security-spike.md`` states what they show.

Offline (every run, every platform):
- holds every committed record to :data:`EXPECTED` (auth routes, disk-write vs
  zero-write subcommands, XDG vs ``$HOME/.gemini``, ``child_env.py`` preservation
  and shell startup, full customization canary matrix, permission modes and
  bypasses);
- checks build identity, private-root + own-keyring isolation, absence of host
  paths, and that the report cites every scenario;
- verifies ``autoforge.child_env`` rules, ``autoforge.sandbox_policy``
  compilation of the proposed ``ProviderRequirements``, and
  ``autoforge.redaction`` behaviour on synthetic Google/``agy`` secret shapes;
- verifies ``src/autoforge/`` never imports the security probe.

Opt-in (Linux with ``bwrap``, ``keyctl(2)``, Landlock ABI 6+, and
``AUTOFORGE_AGY_PROBE_BIN`` naming an installed ``agy`` binary; no model call):
- re-records every scenario against that binary and holds fresh records to
  :data:`EXPECTED`;
- runs ``agy`` inside AutoForge's real ``sandbox_policy`` + ``sandbox_bwrap`` +
  ``executor`` with escape canaries, a local ``git commit`` into the overlay
  upper layer, and a detached ``setsid -f`` child;
- runs ``agy`` unsandboxed under AutoForge's real Linux child subreaper
  (``executor.execute(..., contain_orphans=True)``) and proves detached child
  cleanup.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from autoforge.child_env import (
    LaunchIdentity,
    credential_looking_entry,
    forbidden_env_entry,
    isolated_overrides,
    overrides_directory,
)
from autoforge.executor import ExecutionRequest, execute
from autoforge.redaction import credential_classes, redact
from autoforge.sandbox import Access, NetworkPosition, compile_launch
from autoforge.sandbox_bwrap import probe as probe_bwrap
from autoforge.sandbox_policy import (
    AuthItem,
    AuthMode,
    LaunchClass,
    LaunchFacts,
    ProviderRequirements,
    describe_launch,
    executable_requirements,
    provider_request,
)

from . import antigravity_probe as base_probe
from . import antigravity_security_probe as sec_probe

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "antigravity-security" / "1.5.13"
REPORT = REPO / "docs" / "antigravity-cli-security-spike.md"
PINNED_VERSION = "1.5.13"
PINNED_SHA256 = "c3fd2c848abf88a243936f33e25bf5970802a7651840b5199318fcd8eb3fdb65"
PROBE_BIN = os.environ.get("AUTOFORGE_AGY_PROBE_BIN", "")
HOST_PATH = re.compile(r"(?<![\w.>-])/(?:home|Users|root|tmp|var|private)/")


def _result_of(record: dict[str, Any]) -> dict[str, Any]:
    for _, line in reversed(record["stdout"]):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("event") == "result":
            return obj.get("result") or {}
        if isinstance(obj, dict) and "status" in obj and "conversation_id" in obj:
            return obj
    return {}


def _text(record: dict[str, Any], stream: str) -> str:
    return "".join(line for _, line in record[stream])


def _derive(record: dict[str, Any]) -> dict[str, Any]:
    result = _result_of(record)
    requests = record["requests"]
    all_tool_results = "\n".join(tr for req in requests for tr in (req.get("tool_results") or []))
    return {
        "exit": record["exit"],
        "killed": record["killed_by_probe"],
        "requests": len(requests),
        "other_requests": len(record["other_requests"]),
        "home_files": len(record["home_files"]),
        "denied": sorted({d["action"] for d in (result.get("denied_actions") or [])}),
        "markers": sorted(record["markers"]),
        "sys_markers": requests[0]["system_markers"] if requests else [],
        "tool_results": all_tool_results,
    }


EXPECTED: dict[str, dict[str, Any]] = {
    # 1. Read-only vs disk-writing subcommands, auth routes, XDG
    "ro-version": {"exit": 0, "requests": 0, "home_files": 0, "stdout": PINNED_VERSION},
    "ro-help": {"exit": 0, "requests": 0, "home_files": 0, "stderr": "--disable-slash-commands"},
    "ro-mcp-list": {"exit": 0, "requests": 0, "home_files": 0, "stdout": "No MCP servers"},
    "ro-plugin-list": {"exit": 0, "requests": 0, "home_files": 0, "stdout": "No plugins"},
    "ro-remote-control-status": {
        "exit": 0,
        "requests": 0,
        "home_files": 0,
        "stderr": "antigravity-cli-daemon.service",
    },
    "rw-agent-list": {"exit": 0, "requests": 0, "min_home_files": 10},
    "rw-config-catalogue": {
        "exit": 0,
        "requests": 0,
        "min_home_files": 10,
        "stdout": "toolPermission",
    },
    "auth-google-api-key-only": {
        "exit": 1,
        "requests": 0,
        "stderr": "GEMINI_API_KEY environment variable is not set",
    },
    "auth-gemini-key-without-provider": {
        "exit": 1,
        "requests": 0,
        "stderr": "authentication required",
    },
    "auth-adc-synthetic": {
        "exit": 1,
        "requests": 0,
        "stderr": "authentication required",
    },
    "auth-models-no-key": {
        "exit": 1,
        "requests": 0,
        "stderr": "GEMINI_API_KEY environment variable is not set",
    },
    "auth-models-bogus-key-offline": {
        "exit": 0,
        "requests": 0,
        "min_home_files": 10,
        "stdout": "gemini-4-argon",
    },
    "state-xdg-vs-home": {
        "exit": 0,
        "requests": 2,
        "markers": ["{work}/xdg-files.txt"],
        "marker_equals": {"{work}/xdg-files.txt": "present"},
    },
    # 2. Environment inheritance, child_env.py & shell startup
    "env-child-overrides-and-shell-startup": {
        "exit": 0,
        "requests": 2,
        "markers": ["{work}/env-dump.json", "{work}/shell-info.txt"],
        "marker_contains": {"{work}/shell-info.txt": "FLAGS=hBc ARG0=bash"},
    },
    "env-hook-and-mcp-inherit-key": {
        "exit": 0,
        "requests": 1,
        "markers": ["{work}/m-hook-env", "{work}/m-mcp-env"],
        "marker_contains": {
            "{work}/m-hook-env": f"KEY={sec_probe.SYNTHETIC_KEY} GH=autoforge-no-github-credential",
            "{work}/m-mcp-env": f"KEY={sec_probe.SYNTHETIC_KEY} GH=autoforge-no-github-credential",
        },
    },
    # 3. Integration isolation canaries
    "custom-dir-dot-agents": {
        "exit": 0,
        "requests": 1,
        "markers": ["{work}/m-hook", "{work}/m-mcp"],
        "sys_markers": ["RULE-dot-agents"],
    },
    "custom-dir-dot-agent": {
        "exit": 0,
        "requests": 1,
        "markers": ["{work}/m-hook", "{work}/m-mcp"],
        "sys_markers": ["RULE-dot-agent"],
    },
    "custom-dir-under-agents": {
        "exit": 0,
        "requests": 1,
        "markers": ["{work}/m-hook", "{work}/m-mcp"],
        "sys_markers": ["RULE-under-agents"],
    },
    "custom-dir-under-agent": {
        "exit": 0,
        "requests": 1,
        "markers": ["{work}/m-hook", "{work}/m-mcp"],
        "sys_markers": ["RULE-under-agent"],
    },
    "custom-plugins-manifests-and-http-mcp": {
        "exit": 0,
        "requests": 1,
        "min_other_requests": 1,
        "markers": [
            "{work}/m-ext-plugin-hook",
            "{work}/m-plugin-hook",
            "{work}/m-plugin-mcp",
        ],
        "sys_markers": [
            "PLUGIN-RULE-MARKER",
            "EXT-SKILL-MARKER",
            "EXT-RULE-MARKER",
            "CUSTOM-AGENT-MARKER",
        ],
    },
    "custom-traversal-cwd-below-repo-root": {
        "exit": 0,
        "requests": 1,
        "markers": ["{work}/m-mid-hook", "{work}/m-root-hook", "{work}/m-root-mcp"],
        "sys_markers": ["SUB-ROOT-RULE", "SUB-ROOT-AGENTS", "SUB-MID-AGENTS"],
    },
    "custom-traversal-above-repo-root": {
        "exit": 0,
        "requests": 1,
        "markers": [],
        "sys_markers": [],
    },
    "custom-traversal-below-cwd": {
        "exit": 0,
        "requests": 1,
        "markers": [],
        "sys_markers": [],
    },
    "custom-traversal-git-worktree": {
        "exit": 0,
        "requests": 1,
        "markers": [],
        "sys_markers": [],
    },
    "custom-symlink-inside-workspace": {
        "exit": 0,
        "requests": 1,
        "markers": ["{work}/m-symin-hook", "{work}/m-symin-mcp"],
    },
    "custom-symlink-file-inside-workspace": {
        "exit": 0,
        "requests": 1,
        "markers": ["{work}/m-symfile-hook"],
    },
    "custom-symlink-outside-workspace": {
        "exit": 0,
        "requests": 1,
        "markers": [],
    },
    "custom-global-home": {
        "exit": 0,
        "requests": 1,
        "markers": ["{work}/m-global-hook", "{work}/m-global-mcp"],
        "sys_markers": ["GLOBAL-AGENTS-MARKER", "HOME-GEMINI-MD-MARKER"],
    },
    "custom-add-dir-widening": {
        "exit": 0,
        "requests": 1,
        "markers": ["{work}/m-adddir-hook"],
        "sys_markers": ["ADDDIR-AGENTS-MARKER"],
    },
    "custom-mid-session-write-hooks": {
        "exit": 0,
        "requests": 3,
        "markers": ["{work}/.agents/hooks.json", "{work}/m-step2"],
    },
    "custom-foreign-configs-ignored": {
        "exit": 0,
        "requests": 1,
        "markers": [],
        "sys_markers": [],
    },
    "custom-slash-config-without-disable": {
        "exit": 2,
        "requests": 0,
        "stderr": "unavailable with --input-format stream-json",
    },
    "custom-slash-config-with-disable": {
        "exit": 0,
        "requests": 1,
    },
    # 4. Tools, permission modes & explicit rules
    "perm-default-read-outside": {
        "exit": 0,
        "requests": 2,
        "denied": [],
        "tool_results_contains": "OUTSIDE-SECRET",
    },
    "perm-default-write-outside": {
        "exit": 0,
        "requests": 2,
        "denied": [],
        "markers": ["{base}/written-outside.txt"],
    },
    "perm-default-edit-outside": {
        "exit": 0,
        "requests": 2,
        "denied": [],
        "markers": ["{base}/outside.txt"],
        "marker_contains": {"{base}/outside.txt": "EDITED-OUTSIDE"},
    },
    "perm-default-cmd-denied": {
        "exit": 0,
        "requests": 1,
        "denied": ["command"],
    },
    "perm-mode-plan-write-outside": {
        "exit": 0,
        "requests": 2,
        "denied": [],
        "markers": ["{base}/written-outside.txt"],
    },
    "perm-mode-bogus-warns-and-runs": {
        "exit": 0,
        "requests": 1,
        "stderr": 'unrecognized --mode value "bogus"',
    },
    "perm-strict-honors-allow": {
        "exit": 0,
        "requests": 2,
        "denied": [],
    },
    "perm-strict-denies-write": {
        "exit": 0,
        "requests": 1,
        "denied": ["write_file"],
        "markers": [],
    },
    "perm-rr-allow-git-local": {
        "exit": 0,
        "requests": 2,
        "denied": [],
    },
    "perm-rr-deny-unlisted-git-push": {
        "exit": 0,
        "requests": 1,
        "denied": ["command"],
    },
    "perm-rr-blocks-shell-chain": {
        "exit": 0,
        "requests": 1,
        "denied": ["command"],
        "markers": [],
    },
    "perm-always-proceed-honors-deny": {
        "exit": 0,
        "requests": 2,
        "denied": [],
        "tool_results_contains": "Matches user-configured deny rule",
    },
    "perm-skip-flag-honors-deny": {
        "exit": 0,
        "requests": 2,
        "denied": [],
        "tool_results_contains": "Matches user-configured deny rule",
    },
    "perm-deny-bypassed-by-subshell": {
        "exit": 0,
        "requests": 2,
        "denied": [],
        "markers": ["{work}/bypass.txt"],
    },
    "perm-invalid-re2-regex-silently-ignored": {
        "exit": 0,
        "requests": 2,
        "denied": [],
        "markers": ["{base}/written-outside.txt"],
    },
    "perm-unknown-nested-key-silently-ignored": {
        "exit": 0,
        "requests": 1,
        "denied": [],
    },
    "perm-native-sandbox-flag": {
        "exit": 0,
        "requests": 2,
        "denied": [],
        "markers": ["{work}/sbx-ran.txt"],
    },
}


def _assert_matches(record: dict[str, Any], want: dict[str, Any], *, pinned: bool) -> None:
    got = _derive(record)
    assert record["isolation"] == sec_probe.ISOLATION
    assert record["session_keyring"] == "own"
    assert not got["killed"]
    if pinned:
        assert record["agy_version"] == PINNED_VERSION
        assert record["agy_sha256"] == PINNED_SHA256
    for key in ("exit", "requests", "home_files", "denied", "markers", "sys_markers"):
        if key in want:
            assert got[key] == want[key], (
                f"{record['scenario']}.{key}: {got[key]!r} != {want[key]!r}"
            )
    if "min_home_files" in want:
        assert got["home_files"] >= want["min_home_files"]
    if "min_other_requests" in want:
        assert got["other_requests"] >= want["min_other_requests"]
    for stream in ("stdout", "stderr"):
        if stream in want:
            if pinned or record["scenario"] != "ro-version":
                assert want[stream] in _text(record, stream)
    if "tool_results_contains" in want:
        assert want["tool_results_contains"] in got["tool_results"]
    for mk, val in want.get("marker_equals", {}).items():
        assert record["markers"].get(mk) == val
    for mk, needle in want.get("marker_contains", {}).items():
        assert needle in record["markers"].get(mk, "")
    for req in record["requests"]:
        assert req["key"] == "synthetic"


def _load_committed() -> dict[str, dict[str, Any]]:
    return {
        p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob("*.json"))
    }


def test_committed_inventory_matches_scenarios_and_expected() -> None:
    records = _load_committed()
    scenario_names = {sc.name for sc in sec_probe.SCENARIOS}
    assert set(records) == scenario_names == set(EXPECTED)
    assert len(records) == 50


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_committed_security_record(name: str) -> None:
    record = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert record["scenario"] == name
    _assert_matches(record, EXPECTED[name], pinned=True)


def test_committed_records_contain_no_host_paths_or_real_secrets() -> None:
    for path in sorted(FIXTURES.glob("*.json")):
        raw = path.read_text(encoding="utf-8")
        assert not HOST_PATH.findall(raw), f"{path.name} leaked a host path"
        assert "AIza" not in raw
        assert "ya29." not in raw
        assert "ghp_" not in raw


def test_child_env_preservation_and_injected_vars_in_recorded_dump() -> None:
    rec = json.loads((FIXTURES / "env-child-overrides-and-shell-startup.json").read_text())
    dump = json.loads(rec["markers"]["{work}/env-dump.json"])
    vals = dump["vals"]
    # Every child_env.py override survived verbatim
    assert vals["GH_TOKEN"] == "autoforge-no-github-credential"
    assert vals["GH_ENTERPRISE_TOKEN"] == "autoforge-no-github-credential"
    assert vals["GIT_TERMINAL_PROMPT"] == "0"
    assert vals["GIT_CONFIG_COUNT"] == "4"
    assert vals["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert vals["GIT_CONFIG_VALUE_0"] == ""
    assert vals["GIT_CONFIG_KEY_2"] == "core.sshCommand"
    assert vals["SHELL"] == "/bin/bash"
    for path_var in (
        "GH_CONFIG_DIR",
        "GIT_CONFIG_GLOBAL",
        "CURL_HOME",
        "WGETRC",
        "WGET2RC",
        "ZDOTDIR",
    ):
        assert vals[path_var].startswith("<root>/")
    # Shell startup canaries did not execute
    for canary in ("CANARY_BASHRC", "CANARY_BASH_PROFILE", "CANARY_PROFILE", "CANARY_ZSHENV"):
        assert canary not in vals
    # Unavoidable provider key and agy internal IPC vars are documented
    assert vals["GEMINI_API_KEY"] == sec_probe.SYNTHETIC_KEY
    assert "ANTIGRAVITY_LS_ADDRESS" in vals
    assert "ANTIGRAVITY_CSRF_TOKEN" in vals


def test_report_cites_every_security_scenario_and_build_identity() -> None:
    text = REPORT.read_text(encoding="utf-8")
    assert PINNED_VERSION in text
    assert PINNED_SHA256 in text
    missing = [name for name in sorted(EXPECTED) if f"`{name}`" not in text]
    assert not missing, f"report does not cite scenario(s): {missing}"


def test_proposed_argv_matches_report_and_recorded_canaries() -> None:
    """The §8.3 fail-closed argv is the exact argv run by every FIXED_ARGV
    scenario (customization canaries, env checks, state checks) and listed in
    the doctor required-flags row."""
    report = REPORT.read_text(encoding="utf-8")
    section = report[report.index("### Fail-closed launch policy") :]
    start = section.index("```sh\n") + 6
    block = section[start : section.index("```", start)]
    argv = shlex.split(block.replace("\\\n", " "))
    assert argv == [
        "agy",
        *sec_probe.proposed_argv("<allowlisted-id>", "<low|medium|high|xhigh|max>", "<seconds>s"),
    ]
    doctor_line = next(
        line for line in report.splitlines() if line.startswith("| `antigravity required flags` |")
    )
    for flag in (
        "--input-format",
        "--output-format",
        "--settings",
        "--model",
        "--effort",
        "--print-timeout",
        "--disable-slash-commands",
    ):
        assert f"`{flag}`" in doctor_line, f"doctor required-flags row missing {flag}"

    fixed_scenarios = [sc.name for sc in sec_probe.SCENARIOS if sc.argv == sec_probe.FIXED_ARGV]
    assert len(fixed_scenarios) >= 19
    for name in fixed_scenarios:
        rec = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        assert rec["argv"] == sec_probe.proposed_argv(), name
        if rec["requests"]:
            init = json.loads(rec["stdout"][0][1])
            assert init["event"] == "init", name
            assert init["init"]["model"] == base_probe.FIXED_MODEL, name
            assert init["init"]["permission_mode"] == "always-proceed", name


def test_package_never_imports_security_probe() -> None:
    for path in sorted((REPO / "src" / "autoforge").rglob("*.py")):
        body = path.read_text(encoding="utf-8")
        assert "antigravity_security_probe" not in body, f"{path} imports the security probe"


def test_child_env_rules_for_antigravity_variables() -> None:
    assert forbidden_env_entry("GEMINI_API_KEY") is None
    assert credential_looking_entry("GEMINI_API_KEY", ["GEMINI_API_KEY"]) == ""
    assert "TOKEN" in credential_looking_entry("ANTIGRAVITY_CSRF_TOKEN", ["ANTIGRAVITY_CSRF_TOKEN"])
    # Existing GitHub/IPC/shell variables remain forbidden
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "ZDOTDIR",
    ):
        assert forbidden_env_entry(name) is not None


def test_redaction_baseline_and_documented_gaps_on_synthetic_canaries() -> None:
    aiza = "AIza" + "AbCdEfGhIjKlMnOpQrStUvWxYz012345678"
    non_aiza = "synthetic-non-aiza-key-1234567890"
    ya29 = "ya29.a0SyntheticAccessToken1234567890"
    refresh = "1//0gSyntheticRefreshToken1234567890abcdef"
    client_secret = "GOCSPX-SyntheticClientSecret1234567890"
    gh_pat = "ghp_SyntheticGitHubPat1234567890abcdef"

    # Shapes already redacted by baseline
    for sample, secret, expected_class in (
        (aiza, aiza, "google-api-key"),
        (f"GEMINI_API_KEY={aiza}", aiza, "google-api-key"),
        (f"x-goog-api-key: {non_aiza}", non_aiza, "secret-assignment"),
        (json.dumps({"access_token": ya29}), ya29, "oauth-field"),
        (json.dumps({"refresh_token": refresh}), refresh, "oauth-field"),
        (f"Authorization: Bearer {ya29}", ya29, "authorization-header"),
        (
            json.dumps({"event": "step_update", "text_delta": f'GH_TOKEN="{gh_pat}"'}),
            gh_pat,
            "env-assignment",
        ),
        (f'AGY_ERROR: {{"short_error":"key {aiza}"}}', aiza, "google-api-key"),
    ):
        assert secret not in redact(sample)
        assert expected_class in credential_classes(sample)

    # Documented G1 redaction gaps (§7 of docs/antigravity-cli-security-spike.md):
    # if a future change closes one, update §7 and this audit test.
    for sample, secret in (
        (f"GEMINI_API_KEY={non_aiza}", non_aiza),
        (f"GOOGLE_API_KEY={non_aiza}", non_aiza),
        (f"ANTIGRAVITY_CSRF_TOKEN={non_aiza}", non_aiza),
        (f"bare {ya29}", ya29),
        (f"bare {refresh}", refresh),
        (f"bare {client_secret}", client_secret),
    ):
        assert secret in redact(sample), f"gap closed; update §7 report for {sample!r}"


def test_proposed_provider_requirements_compile_under_sandbox_policy(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_bin = tmp_path / "bin" / "agy"
    work = tmp_path / "work"
    fake_home.mkdir()
    fake_bin.parent.mkdir()
    work.mkdir()
    fake_bin.write_bytes(b"\x7fELFsynthetic")
    fake_bin.chmod(0o755)

    exe, roots, path_dirs, spelled = executable_requirements(
        str(fake_bin), home=str(fake_home), search_path="/usr/bin:/bin"
    )
    needs = ProviderRequirements(
        executable=exe,
        install_roots=roots,
        path_dirs=path_dirs,
        spelled_paths=spelled,
        auth=(AuthItem(AuthMode.ENV, "GEMINI_API_KEY"),),
        operator_config_roots=(str(fake_home / ".gemini"),),
    )
    req = provider_request(
        needs,
        home=str(fake_home),
        controller_env={"HOME": str(fake_home)},
        workspace=str(work),
    )
    desc = compile_launch(req)
    assert desc.network is NetworkPosition.HOST
    assert any(
        getattr(op, "source", "") in (str(fake_bin.resolve()), str(fake_bin.parent.resolve()))
        and getattr(op, "access", None) is Access.READ_ONLY
        for op in desc.mounts
    )
    assert not any(
        str(fake_home / ".gemini") in str(getattr(op, "source", "")) for op in desc.mounts
    )


# -- Opt-in live-binary verification (no model call, synthetic key only) ------


@pytest.mark.skipif(not PROBE_BIN, reason="AUTOFORGE_AGY_PROBE_BIN names no agy binary")
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_installed_agy_security_probe_matches_expected(tmp_path: Path) -> None:
    agy = str(Path(PROBE_BIN).expanduser().resolve())
    sec_probe.record_all(agy, tmp_path)
    for name, want in EXPECTED.items():
        record = json.loads((tmp_path / f"{name}.json").read_text(encoding="utf-8"))
        _assert_matches(record, want, pinned=False)


@pytest.mark.skipif(not PROBE_BIN, reason="AUTOFORGE_AGY_PROBE_BIN names no agy binary")
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_installed_agy_inside_autoforge_sandbox_and_subreaper(tmp_path: Path) -> None:
    pr = probe_bwrap()
    if not pr.supported:
        pytest.skip(f"outer sandbox unsupported on this host: {pr.describe()}")
    agy = str(Path(PROBE_BIN).expanduser().resolve())

    steps_box: list[list[dict[str, Any]]] = [[]]
    req_count = [0]

    def candidate(parts: list[dict[str, Any]]) -> bytes:
        body = {
            "candidates": [
                {"content": {"role": "model", "parts": parts}, "index": 0, "finishReason": "STOP"}
            ],
            "modelVersion": "stand-in",
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "totalTokenCount": 15,
            },
        }
        return ("data: " + json.dumps(body) + "\r\n\r\n").encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else {}
            tools = [
                d.get("name")
                for t in body.get("tools") or []
                for d in t.get("functionDeclarations", [])
            ]
            if not tools:
                parts: list[dict[str, Any]] = [{"text": "aux"}]
            else:
                idx = req_count[0]
                req_count[0] += 1
                step = steps_box[0][min(idx, len(steps_box[0]) - 1)]
                parts = (
                    [{"functionCall": {"name": step["name"], "args": step["args"]}}]
                    if step["kind"] == "call"
                    else [{"text": step["text"]}]
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(candidate(parts))
            self.wfile.flush()
            self.close_connection = True

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    abs_sock_name = f"\0autoforge-agy-sec-test-{os.getpid()}"
    abs_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    abs_sock.bind(abs_sock_name)
    abs_sock.listen(1)

    try:
        fake_home = tmp_path / "fake_home"
        (fake_home / ".ssh").mkdir(parents=True)
        (fake_home / ".ssh" / "id_rsa").write_text("SYNTHETIC-SSH-CANARY\n")
        (fake_home / ".config" / "gh").mkdir(parents=True)
        (fake_home / ".config" / "gh" / "hosts.yml").write_text(
            "github.com:\n  oauth_token: synthetic\n"
        )
        (fake_home / ".netrc").write_text("machine github.com login u password synthetic\n")
        (fake_home / ".gemini" / "config").mkdir(parents=True)
        (fake_home / ".gemini" / "config" / "hooks.json").write_text(
            json.dumps(
                {
                    "h": {
                        "SessionStart": [
                            {"type": "command", "command": f"touch {tmp_path}/host-hook-ran"}
                        ]
                    }
                }
            )
        )

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        git_bin = shutil.which("git") or "/usr/bin/git"
        git_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(fake_home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@e.invalid",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@e.invalid",
        }
        subprocess.run(
            [git_bin, "init", "-q", "--template=", str(checkout)], env=git_env, check=True
        )
        (checkout / "a.txt").write_text("hello\n")
        subprocess.run([git_bin, "-C", str(checkout), "add", "a.txt"], env=git_env, check=True)
        subprocess.run(
            [git_bin, "-C", str(checkout), "commit", "-q", "-m", "init"], env=git_env, check=True
        )

        common = checkout / ".git"
        wt = common / "autoforge" / "worktrees" / "1"
        wt.parent.mkdir(parents=True)
        subprocess.run(
            [git_bin, "-C", str(checkout), "worktree", "add", "-q", "--detach", str(wt)],
            env=git_env,
            check=True,
        )
        admin_raw = subprocess.check_output(
            [git_bin, "-C", str(wt), "rev-parse", "--git-dir"], env=git_env, text=True
        ).strip()
        admin_dir = Path(admin_raw) if Path(admin_raw).is_absolute() else (wt / admin_raw).resolve()

        sbx_dir = common / "autoforge" / "sandbox" / "1"
        priv_obj = sbx_dir / "objects"
        ov_work = sbx_dir / "overlay-work"
        priv_obj.mkdir(parents=True)
        ov_work.mkdir(parents=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "state.json").write_text("{}\n")
        config_file = tmp_path / "autoforge.yaml"
        config_file.write_text("sandbox:\n  mode: required\n")

        (wt / "run_canary.py").write_text(
            f"import json, os, socket\n"
            f"res = {{k: os.path.exists(p) for k, p in [\n"
            f"  ('ssh', {str(fake_home / '.ssh/id_rsa')!r}),\n"
            f"  ('gh', {str(fake_home / '.config/gh/hosts.yml')!r}),\n"
            f"  ('netrc', {str(fake_home / '.netrc')!r}),\n"
            f"  ('state', {str(state_dir / 'state.json')!r}),\n"
            f"  ('config', {str(config_file)!r}),\n"
            f"  ('checkout', {str(checkout / 'a.txt')!r}),\n"
            f"  ('host_gemini', {str(fake_home / '.gemini/config/hooks.json')!r}),\n"
            f"]}}\n"
            f"try:\n"
            f"  s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            f"  s.connect({abs_sock_name!r})\n"
            f"  res['abstract_sock'] = 'CONNECTED'\n"
            f"except OSError as e:\n"
            f"  res['abstract_sock'] = f'{{type(e).__name__}}:{{e.errno}}'\n"
            f"try:\n"
            f"  open({str(common / 'HEAD')!r}, 'a').write('x')\n"
            f"  res['common_write'] = 'WRITABLE'\n"
            f"except OSError as e:\n"
            f"  res['common_write'] = f'{{type(e).__name__}}:{{e.errno}}'\n"
            f"try:\n"
            f"  open({str(wt / '.git')!r}, 'a').write('x')\n"
            f"  res['dotgit_write'] = 'WRITABLE'\n"
            f"except OSError as e:\n"
            f"  res['dotgit_write'] = f'{{type(e).__name__}}:{{e.errno}}'\n"
            f"res['gh_token'] = os.environ.get('GH_TOKEN')\n"
            f"open({str(wt / 'sbx-canary.json')!r}, 'w').write(json.dumps(res))\n"
        )

        with isolated_overrides(
            LaunchIdentity((("user.name", "T"), ("user.email", "t@e.invalid")))
        ) as ov:
            launch_env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(fake_home),
                "LANG": "C.UTF-8",
                "TERM": "dumb",
                "GEMINI_API_KEY": sec_probe.SYNTHETIC_KEY,
                "GOOGLE_GEMINI_BASE_URL": f"http://127.0.0.1:{port}",
                **ov,
            }
            exe, roots, path_dirs, spelled = executable_requirements(
                agy, home=str(fake_home), search_path="/usr/bin:/bin"
            )
            needs = ProviderRequirements(
                executable=exe,
                install_roots=roots,
                path_dirs=path_dirs,
                spelled_paths=spelled,
                auth=(AuthItem(AuthMode.ENV, "GEMINI_API_KEY"),),
                operator_config_roots=(str(fake_home / ".gemini"),),
            )
            facts = LaunchFacts(
                launch_class=LaunchClass.REMOTE_AGENT,
                workspace=str(wt),
                home=str(fake_home),
                controller_env={"HOME": str(fake_home)},
                git_entry=str(wt / ".git"),
                git_dir=str(admin_dir),
                common_dir=str(common),
                private_objects=str(priv_obj),
                overlay_work=str(ov_work),
                controller_config=overrides_directory(ov),
                denied_roots=(str(state_dir), str(checkout), str(config_file)),
                network=NetworkPosition.HOST,
            )
            desc = describe_launch(facts, needs, launch_env)
            req_count[0] = 0
            steps_box[0] = [
                {
                    "kind": "call",
                    "name": "run_command",
                    "args": {
                        "CommandLine": (
                            f"python3 {wt}/run_canary.py && echo changed >> {wt}/a.txt && "
                            "git add a.txt && git commit -q -m 'agent commit' && "
                            "setsid -f sleep 297 >/dev/null 2>&1"
                        ),
                        "toolSummary": "Canary and commit",
                        "WaitMsBeforeAsync": 5000,
                    },
                },
                {"kind": "text", "text": "DONE"},
            ]
            res = execute(
                ExecutionRequest(
                    command=[needs.executable, *sec_probe.proposed_argv()],
                    cwd=str(wt),
                    env_allowlist=(),
                    env=launch_env,
                    stdin_data=base_probe.stream_message("do it").encode(),
                    timeout_seconds=30,
                    sandbox=desc,
                )
            )
            assert res.ok and res.sandbox is not None and res.sandbox.enforced
            assert not (tmp_path / "host-hook-ran").exists()
            canary = json.loads((wt / "sbx-canary.json").read_text())
            for k in ("ssh", "gh", "netrc", "state", "config", "checkout", "host_gemini"):
                assert canary[k] is False
            assert canary["abstract_sock"].startswith("PermissionError:")
            assert canary["common_write"].startswith("OSError:30")
            assert canary["dotgit_write"].startswith("OSError:30")
            assert canary["gh_token"] == "autoforge-no-github-credential"
            assert any(p.is_file() for p in priv_obj.rglob("*"))

        # Unsandboxed Linux child subreaper test (ADR 0002)
        with tempfile.TemporaryDirectory(prefix="af-agy-orphan.") as otmp:
            ohome = Path(otmp) / "home"
            owork = Path(otmp) / "work"
            ohome.mkdir()
            owork.mkdir()
            req_count[0] = 0
            steps_box[0] = [
                {
                    "kind": "call",
                    "name": "run_command",
                    "args": {
                        "CommandLine": "setsid -f sleep 296 >/dev/null 2>&1; echo launched",
                        "toolSummary": "Detached sleep",
                        "WaitMsBeforeAsync": 5000,
                    },
                },
                {"kind": "text", "text": "DONE"},
            ]
            ores = execute(
                ExecutionRequest(
                    command=[agy, *sec_probe.proposed_argv()],
                    cwd=str(owork),
                    env_allowlist=(),
                    env={
                        "HOME": str(ohome),
                        "PATH": "/usr/bin:/bin",
                        "LANG": "C.UTF-8",
                        "TERM": "dumb",
                        "GEMINI_API_KEY": sec_probe.SYNTHETIC_KEY,
                        "GOOGLE_GEMINI_BASE_URL": f"http://127.0.0.1:{port}",
                    },
                    stdin_data=base_probe.stream_message("do it").encode(),
                    timeout_seconds=30,
                    contain_orphans=True,
                )
            )
            assert ores.ok
            assert ores.orphans_killed is True
            assert ores.orphan_survived_kill is False
            assert ores.orphans_unchecked is False
    finally:
        abs_sock.close()
        srv.shutdown()
