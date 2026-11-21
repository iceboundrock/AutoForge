"""Issue #528's evidence: the Antigravity CLI probe's records, held to the report.

``tests/antigravity_probe.py`` runs ``agy`` against a loopback stand-in of
the Gemini API, in a sandbox with no network and an empty home, and writes
one record per scenario. The records of ``agy`` 1.5.13 are committed under
``tests/fixtures/antigravity/1.5.13/``, and ``docs/antigravity-cli-spike.md``
states what they show. :data:`EXPECTED` is that statement in checkable form:
every fact the report rests on, per scenario.

Offline (every run, every platform), the committed records are held to
:data:`EXPECTED`, the prototype classifier (:func:`classify`) is held to
the contract the report suggests, on the records and on faults ``agy``
cannot be made to produce (a malformed line or value, a record without its
LF, a blank or cut line, a member given twice, a contradictory or
incomplete result, a duplicate or misplaced result, a missing init, a
response or tool that never ends, an ambiguous step, a capture of stdout
or stderr that was abandoned or truncated, an exit by a limit or a
signal), the complete measurements of what a prompt loses, as text on
stdin (``fidelity-all-*``) and framed as the recommended transport sends
it (``fidelity-framed-*``), are held to the committed table
``dropped.txt``, the ``table`` command is held to refuse incomplete,
unidentified or inconsistent evidence, and the records are checked to
carry no path of the host, no OAuth query value and no key, and to name
the sandbox they were made in. The recorder is held to its boundary: with
a fake ``agy`` in ``bwrap``, by harmless canaries of the host's files,
environment, sockets and session keyring (a key a helper of the test's
puts in a session keyring of its own), a link and a FIFO planted where
the host could read them, junk written into the record's pipe, and a
caller's ``GIT_DIR``; without ``bwrap``, by its bounded read of the
record, and by its read of agy's streams at their bytes (EOF only when a
read returned it, a byte that is not UTF-8 kept and refused). The
package never imports the probe.

Opt-in, ``AUTOFORGE_AGY_PROBE_BIN`` names an installed ``agy``: the probe
records every scenario again with that binary (Linux and ``bwrap`` only;
about three minutes, most of it the CLI's own retries of a 429, a 503 and a
cut stream) and the fresh records are held to the same :data:`EXPECTED`.
No model is called: the key is synthetic and the sandbox has no route out::

    AUTOFORGE_AGY_PROBE_BIN=~/.local/bin/agy uv run pytest tests/test_antigravity_probe.py

The fresh records are held to the behaviour, not to the label: whatever
version the binary prints (asked in the sandbox, like every scenario) is
the one they name, and only the committed records must say 1.5.13. A
failure there on a new build is the point: the report's claims hold for
1.5.13 and must be measured again before they are trusted for another.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest

import autoforge

from . import antigravity_probe as probe
from .antigravity_probe import (
    ALL_PREFIX,
    ASTRAL,
    AT_THE_BOUND,
    DROPPED_TABLE,
    FRAMED_PREFIX,
    ISOLATION,
    NEAR_LIMIT,
    PROBE_CUT,
    REPLY,
    SANDBOX_PYTHON,
    SCALAR_VALUES,
    SCENARIO_NAMED,
    SCENARIOS,
    SWEEPS,
    SYNTHETIC_KEY,
    VERSION_SCENARIO,
    WHOLE,
    WIDEST,
    Capture,
    Outcome,
    ProbeError,
    build_problems,
    capture_of,
    classify,
    discover_version,
    dropped_of,
    fixed_argv,
    is_dropped,
    measure_all,
    outcome_of,
    read_stream,
    read_table,
    record_all,
    reduce_stream,
    result_flags,
    run_sandboxed,
    sandbox_argv,
    stripped,
    sweep_problems,
    version_printed,
    write_table,
)
from .conftest import bind_unix

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "antigravity" / "1.5.13"
REPORT = REPO / "docs" / "antigravity-cli-spike.md"
PINNED = "1.5.13"
# The binary the committed records were made with (the report's header).
PINNED_SHA256 = "c3fd2c848abf88a243936f33e25bf5970802a7651840b5199318fcd8eb3fdb65"
PROBE_BIN = os.environ.get("AUTOFORGE_AGY_PROBE_BIN", "")
# An absolute path of the host: one not spelt after the probe's <root>.
HOST_PATH = re.compile(r"(?<![\w.>-])/(?:home|Users|root|tmp|var|private)/")
DROPPED_BY_AGY = (
    [f"U+{c:04X}" for c in list(range(0x01, 0x09)) + list(range(0x0E, 0x20))]
    + ["U+007F"]
    + [f"U+{c:04X}" for c in range(0x80, 0xA0) if c != 0x85]
)
# The controls agy keeps: TAB, LF, VT, FF, CR and NEL.
KEPT_CONTROLS = frozenset((0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x85))
# The Unicode version of agy 1.5.13's tables: what the complete measurement
# matches exactly (general category C but KEPT_CONTROLS).
AGY_UNICODE = (17, 0, 0)


def result_of(record: dict[str, Any]) -> dict[str, Any] | None:
    """The run's last result: a stream-json ``result`` event, or a json object."""
    found = None
    for _, line in record["stdout"]:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("event") == "result":
            found = obj["result"]
        elif isinstance(obj, dict) and "status" in obj and "conversation_id" in obj:
            found = obj
    return found


def results_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for _, line in record["stdout"]:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("event") == "result":
            out.append(obj["result"])
    return out


def agy_error(record: dict[str, Any]) -> dict[str, Any]:
    for _, line in record["stderr"]:
        if line.startswith("AGY_ERROR: "):
            return json.loads(line[len("AGY_ERROR: ") :])
    return {}


def text(record: dict[str, Any], stream: str) -> str:
    return "".join(line for _, line in record[stream])


def derive(record: dict[str, Any]) -> dict[str, Any]:
    """The facts :data:`EXPECTED` names, from one record."""
    result = result_of(record) or {}
    error = agy_error(record)
    fidelity = record["facts"].get("fidelity", {})
    return {
        "exit": record["exit"],
        "killed": record["killed_by_probe"],
        "requests": len(record["requests"]),
        "titles": record["title_requests"],
        "results": len(results_of(record)),
        "status": result.get("status"),
        "finished_reason": result.get("finished_reason"),
        "denied": sorted({d["action"] for d in result.get("denied_actions") or []}),
        "response_blocks": result.get("response", "").count("<<<CONTROL_RESULT>>>"),
        "error_status": error.get("status"),
        "error_code": error.get("error_code"),
        "retryable": error.get("retryable"),
        "outcome": outcome_of(record).kind,
        "result_less_exit": result_flags(record)["result_less_exit"],
        "left": len(record["processes_left"]),
        "markers": sorted(record["markers"]),
        # what had run in the workspace when the model was first asked
        "markers_first_request": sorted(
            record["requests"][0]["markers_present"] if record["requests"] else []
        ),
        "outside_file": record["outside_file_written"],
        "identical": fidelity.get("identical"),
        "identical_after_strip": fidelity.get("identical_after_strip"),
        "truncated": fidelity.get("truncated_bytes") is not None,
        "closing_tag_kept": fidelity.get("closing_tag_kept"),
        "context_markers": record["facts"].get("context_markers", []),
    }


OK = {"exit": 0, "status": "SUCCESS", "outcome": "completed", "results": 1}
PROTOCOL_IS_NOT_STREAM = {"outcome": "protocol"}  # not stream-json: the classifier refuses it
API_ERROR = {"exit": 3, "status": "ERROR", "finished_reason": "error", "results": 1}
# The repository's rules are loaded, and its hooks and MCP server run, whatever the
# settings: every one but the Stop hook before the first model request.
CUSTOMIZED = {
    "exit": 0,
    "status": "SUCCESS",
    "markers": ["hook-pre-invocation", "hook-session-start", "hook-stop", "mcp-started"],
    "markers_first_request": ["hook-pre-invocation", "hook-session-start", "mcp-started"],
    "context_markers": ["AGENTS-RULE-MARKER", "GEMINI-RULE-MARKER", "DOTAGENTS-RULE-MARKER"],
}

# What the report states, per scenario. ``stdout``/``stderr``: a text the
# stream must hold; ``no_stdout``: nothing on stdout at all.
EXPECTED: dict[str, dict[str, Any]] = {
    # The version it printed is checked against the records' label, not
    # against 1.5.13: a new build is measured by its behaviour (#528, C3).
    "version": {"exit": 0, "killed": False, "requests": 0},
    "help": {"exit": 0, "stderr": "--input-format", "no_stdout": True, "requests": 0},
    "settings-catalogue": {
        "exit": 0,
        "status": "SUCCESS",
        "requests": 0,
        "stdout": "toolPermission",
    },
    "settings-unknown-key": {"exit": 1, "stderr": 'unknown setting "noSuchKey"', "requests": 0},
    "acp-help": {"exit": 0, "stderr": ["--gemini-dir", "--app-data-dir", "--disable-telemetry"]},
    "acp-flag-in-print-mode": {
        "exit": 2,
        "requests": 0,
        "stderr": "flags provided but not defined",
    },
    "model-list": {"exit": 0, "stdout": "gemini-4-argon-medium", "requests": 0},
    "success-stream-json": {**OK, "requests": 1, "response_blocks": 1, "stdout": '"event":"init"'},
    "success-json": {"exit": 0, "status": "SUCCESS", "requests": 1, **PROTOCOL_IS_NOT_STREAM},
    "success-text": {"exit": 0, "stdout": "<<<CONTROL_RESULT>>>", **PROTOCOL_IS_NOT_STREAM},
    "success-chunks": {**OK, "requests": 1},
    "stdin-text": {**OK, "requests": 1},
    "stdin-stream-json": {**OK, "requests": 1},
    "stdin-and-argv": {**OK, "requests": 1},
    "stdin-empty": {
        "exit": 0,
        "requests": 0,
        "stdout": "could not open TTY",
        "outcome": "protocol",
    },
    "stdin-whitespace": {
        "exit": 1,
        "status": "ERROR",
        "stdout": "empty prompt",
        "outcome": "error",
    },
    "stdin-stream-json-empty": {"exit": 0, "requests": 0, "results": 0, "outcome": "no_result"},
    "argv-leading-dash": {**OK, "identical": True},
    "argv-flag-name": {"exit": 2, "requests": 0, "stderr": 'took "--version" as its prompt'},
    "argv-after-double-dash": {"exit": 2, "requests": 0, "stderr": 'unexpected argument "-p"'},
    "fidelity-unicode-argv": {**OK, "identical": False, "identical_after_strip": True},
    "fidelity-unicode-stdin": {**OK, "identical": False, "identical_after_strip": True},
    "fidelity-unicode-stream-json": {**OK, "identical": False, "identical_after_strip": True},
    "fidelity-sweep": {**OK, "identical": False, "identical_after_strip": True},
    "fidelity-invalid-utf8": {**OK, "identical": True},
    "fidelity-size-190000": {**OK, "identical": True, "truncated": False, "closing_tag_kept": True},
    "fidelity-size-196608": {
        **OK,
        "identical": False,
        "truncated": True,
        "closing_tag_kept": False,
    },
    "fidelity-size-262144": {
        **OK,
        "identical": False,
        "truncated": True,
        "closing_tag_kept": False,
    },
    "fidelity-size-262144-stream-json": {**OK, "truncated": True, "closing_tag_kept": False},
    "deny-command": {
        "exit": 0,
        "status": "SUCCESS",
        "requests": 1,
        "denied": ["command"],
        "response_blocks": 0,
        "outcome": "denied",
        "stderr": "auto-denied",
    },
    "deny-command-json": {"exit": 0, "status": "SUCCESS", "denied": ["command"], "requests": 1},
    "allow-command-skip": {**OK, "requests": 2, "stdout": "Command exited with code 0"},
    "write-outside-default": {**OK, "requests": 2, "outside_file": True},
    # answered with nothing, and shown only as an `unknown` step: never completed
    "ask-question": {
        "exit": 0,
        "status": "SUCCESS",
        "results": 1,
        "requests": 2,
        "stdout": '"step_type":"unknown"',
        "outcome": "ambiguous",
    },
    "background-task": {
        **OK,
        "requests": 3,
        "response_blocks": 2,
        "left": 0,
        "stderr": "waiting for 1 background task(s)",
    },
    "daemon-task": {
        "exit": 0,
        "status": "SUCCESS",
        "left": 0,
        "stderr": "terminating 1 daemon task(s) on exit",
        "outcome": "protocol",
    },
    "detached-child": {**OK, "left": 1},
    "timer": {**OK, "requests": 3, "response_blocks": 2},
    "sigterm-background-task": {
        "exit": 1,
        "finished_reason": "interrupted",
        "left": 2,
        "outcome": "interrupted",
    },
    "print-timeout-background": {
        "exit": 3,
        "finished_reason": "print_timeout",
        "error_status": "DEADLINE_EXCEEDED",
        "response_blocks": 1,
        "left": 0,
        "outcome": "print_timeout",
    },
    # the same on the fixed argv, framed: exit 0, a SUCCESS that names the
    # timeout (contradictory, so a protocol failure), and the task left running
    "print-timeout-background-recommended": {
        "exit": 0,
        "status": "SUCCESS",
        "finished_reason": "print_timeout",
        "error_status": "DEADLINE_EXCEEDED",
        "response_blocks": 1,
        "left": 2,
        "outcome": "protocol",
    },
    "hang-no-limit": {"killed": True, "results": 0, "requests": 1, "outcome": "no_result"},
    "hang-print-timeout": {
        "exit": 3,
        "finished_reason": "print_timeout",
        "error_status": "DEADLINE_EXCEEDED",
        "outcome": "print_timeout",
    },
    "sigint-during-request": {
        "exit": 1,
        "finished_reason": "interrupted",
        "outcome": "interrupted",
    },
    "sigterm-during-request": {
        "exit": 1,
        "finished_reason": "interrupted",
        "outcome": "interrupted",
    },
    "sigint-during-request-json": {"exit": 1, "finished_reason": "interrupted"},
    "api-400": {
        **API_ERROR,
        "requests": 1,
        "error_code": 400,
        "retryable": False,
        "outcome": "error",
    },
    "api-401": {
        **API_ERROR,
        "requests": 1,
        "error_code": 401,
        "retryable": False,
        "outcome": "error",
    },
    "api-403": {
        **API_ERROR,
        "requests": 1,
        "error_code": 403,
        "retryable": False,
        "outcome": "error",
    },
    "api-404": {
        **API_ERROR,
        "requests": 1,
        "error_code": 404,
        "retryable": False,
        "outcome": "error",
    },
    "api-429": {**API_ERROR, "requests": 9, "error_code": 429, "outcome": "usage_limit"},
    "api-500": {
        **API_ERROR,
        "requests": 1,
        "error_code": 500,
        "retryable": False,
        "outcome": "transient",
    },
    "api-503": {**API_ERROR, "requests": 9, "error_code": 503, "outcome": "transient"},
    "api-503-then-success": {**OK, "requests": 2},
    "cut-stream-then-success": {**OK, "requests": 2, "stdout": '"step_type":"error_message"'},
    "cut-stream-always": {
        **API_ERROR,
        "requests": 9,
        "error_status": "UNKNOWN",
        "outcome": "error",
    },
    "api-key-route-without-key": {
        "exit": 1,
        "requests": 0,
        "no_stdout": True,
        "stderr": "GEMINI_API_KEY",
    },
    "no-login-stdin-prompt": {"exit": 1, "status": "ERROR", "stderr": "authentication required"},
    "no-login-argv-prompt": {
        "exit": 1,
        "status": "ERROR",
        "stderr": "Waiting for authentication (timeout 60s)",
    },
    "model-default": {"exit": 0, "status": "SUCCESS", "requests": 1},
    "model-with-effort-suffix": {"exit": 0, "status": "SUCCESS", "requests": 1},
    "model-base-and-effort": {"exit": 0, "status": "SUCCESS", "requests": 1},
    "model-effort-conflict": {
        "exit": 1,
        "status": "ERROR",
        "requests": 0,
        "stderr": "conflicts with",
    },
    "model-effort-unsupported": {"exit": 1, "requests": 0, "stderr": 'has no "max" effort'},
    "model-unknown": {"exit": 1, "requests": 0, "stderr": "is not recognized as a known model"},
    "stream-input-two-turns": {"exit": 0, "results": 2, "requests": 2, "outcome": "protocol"},
    "stream-input-malformed-after-turn": {
        "exit": 1,
        "results": 2,
        "requests": 1,
        "status": "ERROR",
        "outcome": "protocol",
    },
    "stream-input-left-open": {"killed": True, "results": 1, "requests": 1},
    "stream-input-wrong-shape": {
        "exit": 1,
        "requests": 0,
        "status": "ERROR",
        "stderr": 'stream input message is missing the "event" field',
    },
    "stream-input-cut": {"exit": 1, "requests": 0, "status": "ERROR", "stdout": "unexpected EOF"},
    "repository-customizations": CUSTOMIZED,
    "repository-customizations-untrusted": CUSTOMIZED,
    "repository-customizations-sandbox": CUSTOMIZED,
    # the recommended launch itself: the fixed argv, every flag of it, prompt framed
    "repository-customizations-recommended": {**CUSTOMIZED, "outcome": "completed"},
    "fresh-first": {"exit": 0, "requests": 1, "context_markers": ["MARKER-ALPHA-123"]},
    "fresh-second": {"exit": 0, "requests": 1, "context_markers": []},
    "continue-latest": {"exit": 0, "requests": 1, "context_markers": []},
    "concurrent-1": {"exit": 0, "status": "SUCCESS", "requests": 1},
    "concurrent-2": {"exit": 0, "status": "SUCCESS", "requests": 1},
    "concurrent-3": {"exit": 0, "status": "SUCCESS", "requests": 1},
    "acp-text": {"exit": 0, "requests": 1, "titles": 1, "stdout": '"stopReason":"end_turn"'},
    "acp-permission-rejected": {
        "exit": 0,
        "requests": 1,
        "titles": 1,
        "stdout": ["session/request_permission", '"status":"failed"', '"stopReason":"end_turn"'],
    },
}
# The complete measurements: each prompt arrived whole, minus what agy
# removes (complete_sweep_problems holds what it removed to dropped.txt).
for _sc in SCENARIOS:
    if _sc.name.startswith((ALL_PREFIX, FRAMED_PREFIX)):
        EXPECTED[_sc.name] = {
            **OK,
            "requests": 1,
            "truncated": False,
            "closing_tag_kept": True,
            "identical_after_strip": True,
        }
# Framed at the proposed prompt bound: a large mixed sample, the widest
# framing measured, and one astral character alone: each whole and unchanged.
for _name in AT_THE_BOUND:
    EXPECTED[_name] = {
        **OK,
        "requests": 1,
        "identical": True,
        "truncated": False,
        "closing_tag_kept": True,
    }
# No record sets result_less_exit: the MVP never does (result_flags).
for _want in EXPECTED.values():
    _want.setdefault("result_less_exit", False)


def records(directory: Path) -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("*.json")}


def mismatches(found: dict[str, dict[str, Any]]) -> list[str]:
    """Every way the records differ from :data:`EXPECTED`."""
    problems = []
    for name, want in EXPECTED.items():
        record = found.get(name)
        if record is None:
            problems.append(f"{name}: no record")
            continue
        facts = derive(record)
        for key, value in want.items():
            if key == "stdout" or key == "stderr":
                for wanted in [value] if isinstance(value, str) else value:
                    if wanted not in text(record, key):
                        problems.append(f"{name}: {key} lacks {wanted!r}")
            elif key == "no_stdout":
                if text(record, "stdout").strip():
                    problems.append(f"{name}: stdout not empty")
            elif facts[key] != value:
                problems.append(f"{name}: {key} is {facts[key]!r}, the report says {value!r}")
    return problems


def complete_sweep_problems(found: dict[str, dict[str, Any]]) -> list[str]:
    """Every scalar value measured once and cleanly, through each transport,
    and what each removed 1.5.13's table.

    The coverage is the probe's own check, the one ``table`` refuses on
    (:func:`sweep_problems`). A fresh recording of another build is held to
    what 1.5.13 removed: a difference is a finding, and that build's own
    table is written only once it is understood (the report's C3).
    """
    want = [list(r) for r in read_table()]
    problems = []
    for prefix in SWEEPS:
        problems += sweep_problems(found, prefix)
        if dropped_of(found, prefix) != want:
            problems.append(f"what the {prefix}* prompts lost is not the table of dropped.txt")
    return problems


def cross_record_problems(found: dict[str, dict[str, Any]]) -> list[str]:
    """The facts that compare two records."""
    problems = build_problems(found)  # one build: one version, one sha256, as printed
    problems += complete_sweep_problems(found)
    first, second = found["fresh-first"], found["fresh-second"]
    if first["facts"]["system_prompt_sha256"] != second["facts"]["system_prompt_sha256"]:
        problems.append("a second fresh run in the same home saw another system prompt")
    if result_of(second)["conversation_id"] == result_of(first)["conversation_id"]:
        problems.append("a fresh run reused the conversation of the one before")
    left = set(second["home_files"])
    for kept in (
        ".gemini/antigravity-cli/conversations/<id>.db",
        ".gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl",
        ".gemini/antigravity-cli/presence/<id>.lock",
        ".gemini/antigravity-cli/conversation_summaries.db",
    ):
        if kept not in left:
            problems.append(f"a run left no {kept} in its home")
    if not second["facts"]["names_app_data_dir"]:
        problems.append("the system prompt does not name the app data directory")
    both = found["stdin-and-argv"]["requests"][0]["first_user_text"]
    if "argv prompt" not in both or "stdin prompt" in both:
        problems.append("with -p and stdin, the model was not sent the argv prompt alone")
    cont = found["continue-latest"]
    if result_of(cont)["conversation_id"] != result_of(second)["conversation_id"]:
        problems.append("-c did not continue the most recent conversation")
    if cont["requests"][0]["contents"] <= 1:
        problems.append("-c sent no earlier turn")
    ids = {result_of(found[f"concurrent-{i}"])["conversation_id"] for i in (1, 2, 3)}
    if len(ids) != 3:
        problems.append("concurrent runs shared a conversation")
    for i in (1, 2, 3):
        sent = found[f"concurrent-{i}"]["requests"][0]["first_user_text"]
        if f"concurrent prompt {i}" not in sent:
            problems.append(f"concurrent-{i} sent another run's prompt")
    for name in (
        "repository-customizations",
        "repository-customizations-untrusted",
        "repository-customizations-sandbox",
        "repository-customizations-recommended",
    ):
        if found[name]["markers"]["hook-session-start"] != "key-visible":
            problems.append(f"{name}: the repository's hook did not see GEMINI_API_KEY")
    sweep = found["fidelity-sweep"]["facts"]["sweep"]
    for cp in DROPPED_BY_AGY + ["U+200B", "U+202E", "U+FEFF", "U+E0041", "U+F0000"]:
        if cp not in sweep["dropped"]:
            problems.append(f"the sweep kept {cp}")
    for cp in ("U+0009", "U+000A", "U+000D", "U+0085", "U+00A0", "U+FE0F", "U+FFFD"):
        if cp not in sweep["kept"]:
            problems.append(f"the sweep dropped {cp}")
    for name in (
        "fidelity-size-196608",
        "fidelity-size-262144",
        "fidelity-size-262144-stream-json",
    ):
        if found[name]["facts"]["fidelity"]["kept_before_cut_bytes"] != 191_986:
            problems.append(f"{name}: the cut is not after the prompt's first 191986 bytes")
    models = {
        "model-default": (
            "gemini-3.1-pro-preview",
            {"includeThoughts": True, "thinkingBudget": 1024},
        ),
        "model-with-effort-suffix": (
            "gemini-4-argon",
            {"includeThoughts": True, "thinkingLevel": "MEDIUM"},
        ),
        "model-base-and-effort": (
            "gemini-4-argon",
            {"includeThoughts": True, "thinkingLevel": "HIGH"},
        ),
    }
    for name, (model, thinking) in models.items():
        request = found[name]["requests"][0]
        if f"/models/{model}:" not in request["model_path"] or request["thinking"] != thinking:
            problems.append(f"{name}: sent {request['model_path']} {request['thinking']}")
    for name, record in found.items():
        for request in record["requests"]:
            if request["key"] != "synthetic":
                problems.append(f"{name}: a model request without the synthetic key")
    return problems


# -- the committed records ----------------------------------------------------


@pytest.fixture(scope="module")
def committed() -> dict[str, dict[str, Any]]:
    return records(FIXTURES)


def test_every_scenario_has_a_committed_record_of_the_pinned_build(committed):
    """The label is the committed records' alone: the opt-in run holds a
    fresh recording to the same facts whatever version it prints."""
    assert sorted(committed) == sorted(s.name for s in SCENARIOS)
    assert sorted(EXPECTED) == sorted(committed)
    assert {r["agy_version"] for r in committed.values()} == {PINNED}
    assert {r["agy_sha256"] for r in committed.values()} == {PINNED_SHA256}
    assert PINNED_SHA256 in REPORT.read_text(encoding="utf-8")
    assert version_printed(committed["version"]) == PINNED
    assert [line for _, line in committed["version"]["stdout"]] == [PINNED + "\n"]


def test_the_committed_records_show_what_the_report_states(committed):
    assert mismatches(committed) == []
    assert cross_record_problems(committed) == []


# Shapes of real credentials: GitHub, Anthropic, OpenAI and Google keys and
# tokens, AWS keys, private keys, bearer headers. A match is reported by name only.
CREDENTIAL = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_\w{20,}|sk-ant-[\w-]{20,}"
    r"|\bsk-[A-Za-z0-9]{20,}|\bAIza[\w-]{35}\b|\bya29\.[\w-]{20,}|\bAKIA[0-9A-Z]{16}\b"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:authorization: *bearer +[\w.-]{20,})"
)


def test_the_committed_records_carry_no_host_path_query_value_or_key(committed):
    for name, record in committed.items():
        dump = json.dumps(record, ensure_ascii=False)
        # every path the run saw is under the probe's root, spelt <root>
        assert not HOST_PATH.search(dump), (name, HOST_PATH.search(dump))
        assert SYNTHETIC_KEY not in dump, name
        for value in re.findall(r"(?:client_id|code_challenge|state)=([^&\s\"\\]+)", dump):
            assert value == "<redacted>", name
        assert CREDENTIAL.search(dump) is None, f"{name}: a credential-shaped string"
    assert DROPPED_TABLE.read_text(encoding="ascii").isascii()


CAPTURE_KEYS = {
    "stdout_eof",
    "stderr_eof",
    "stdout_truncated",
    "stderr_truncated",
    "stdout_error",
    "stderr_error",
}
WHOLE_CAPTURE = {
    "stdout_eof": True,
    "stderr_eof": True,
    "stdout_truncated": False,
    "stderr_truncated": False,
    "stdout_error": None,
    "stderr_error": None,
}


def test_each_committed_record_names_the_sandbox_it_was_made_in(committed):
    """Every record was made in the private root, with a session keyring of
    its own (the report's header, R4-F1 of PR #530), and says, for each
    stream, whether a read of it returned EOF, whether one failed first,
    and whether a bound dropped any of it. Each reached EOF on both, lost
    nothing, and held only UTF-8 (R4-F3)."""
    assert {r.get("isolation") for r in committed.values()} == {ISOLATION}
    assert {r.get("session_keyring") for r in committed.values()} == {"own"}
    for name, record in committed.items():
        assert set(record["capture"]) == CAPTURE_KEYS, name
        assert record["capture"] == WHOLE_CAPTURE, name
        for stream_name in ("stdout", "stderr"):
            assert all(probe.utf8(line) for _, line in record[stream_name]), name
    assert CREDENTIAL.search("token ghp_" + "a" * 36) and CREDENTIAL.search("AIza" + "b" * 35)


# The runs that were given a prompt and ended on their own without exactly
# one result (the report's sections 1 and 4), by why. Every other such run
# wrote one result: a run's last record, or a refusal's only one.
NOT_ONE_RESULT = {
    # refused before any request, with nothing on stdout
    "argv-flag-name": "usage",
    "argv-after-double-dash": "usage",
    "acp-flag-in-print-mode": "usage",
    "settings-unknown-key": "setting",
    "api-key-route-without-key": "no key",
    # an empty stdin
    "stdin-empty": "empty",
    "stdin-stream-json-empty": "empty",
    # no result records at all: the text output and the ACP server
    "success-text": "text",
    "acp-text": "acp",
    "acp-permission-rejected": "acp",
    # a stream-json input session: one result per turn
    "stream-input-two-turns": "session",
    "stream-input-malformed-after-turn": "session",
}


def test_the_runs_without_one_result_are_the_ones_the_report_names(committed):
    """R2-F7, R3-F5 of PR #530: "every run given a prompt that ended on its
    own wrote one result" is not so. These are the exceptions, each of them:
    the refusals before any request wrote nothing on stdout at all
    (``api-key-route-without-key`` among them, given a prompt), and an empty
    stdin, the text output, ACP and an input session are the rest."""
    found = {}
    for sc in SCENARIOS:
        record = committed[sc.name]
        prompted = "-p" in sc.argv or any(a.startswith("-p=") for a in sc.argv)
        prompted = prompted or sc.stdin is not None or sc.feed is not None
        own = not record["killed_by_probe"] and record["signalled"] is None
        count = sum(
            1
            for _, line in record["stdout"]
            if (obj := _json_or_none(line)) is not None
            and (obj.get("event") == "result" or {"status", "conversation_id"} <= set(obj))
        )
        if prompted and own and count != 1:
            found[sc.name] = count
    assert set(found) == set(NOT_ONE_RESULT)
    for name, why in NOT_ONE_RESULT.items():
        record = committed[name]
        if why in ("usage", "setting", "no key"):
            assert not text(record, "stdout").strip() and record["requests"] == [], name
            assert record["exit"] == (2 if why == "usage" else 1), name
        if why == "session":
            assert found[name] == 2, name
    report = " ".join(REPORT.read_text(encoding="utf-8").split())
    summary = report[report.index("## 1. Summary") : report.index("## 2. Discovery")]
    for name, why in NOT_ONE_RESULT.items():
        if why in ("usage", "setting", "no key", "empty"):
            assert f"`{name}`" in summary, name


def _json_or_none(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def test_the_recommended_launches_are_the_fixed_argv(committed):
    """R3-F5 of PR #530: the launches the report calls the recommended one
    ran the fixed argv of its section 10, every flag of it, with the prompt
    framed on stdin: the customized repository's (whose hooks and MCP
    server ran before the model was asked) and a print timeout during a
    background task. The report's argv is that argv, its three values
    left open."""
    for name, timeout in (
        ("repository-customizations-recommended", probe.FIXED_TIMEOUT),
        ("print-timeout-background-recommended", "5s"),
    ):
        record = committed[name]
        assert record["argv"] == fixed_argv(timeout=timeout) == SCENARIO_NAMED[name].argv
        assert record["stdin_bytes"] == len(probe.stream_message("do it").encode()), name
        request = record["requests"][0]
        assert f"/models/{probe.FIXED_MODEL}:" in request["model_path"], name
        assert request["thinking"] == {"includeThoughts": True, "thinkingLevel": "HIGH"}, name
        init = json.loads(record["stdout"][0][1])
        assert init["event"] == "init" and init["init"]["model"] == probe.FIXED_MODEL, name
    report = REPORT.read_text(encoding="utf-8")
    section = report[report.index("### Suggested adapter contract") :]
    block = section[
        section.index("```sh\n") + 6 : section.index("```", section.index("```sh\n") + 6)
    ]
    argv = shlex.split(block.replace("\\\n", " "))
    assert argv == ["agy", *fixed_argv("<id>", "<effort>", "<seconds>s")]
    # the hooks and the MCP server had run when the model was first asked
    first = committed["repository-customizations-recommended"]["requests"][0]
    assert sorted(first["markers_present"]) == CUSTOMIZED["markers_first_request"]


def test_the_report_cites_every_scenario():
    """The numbered prompts of each complete measurement are cited as a run,
    ``fidelity-all-00`` to the last, and ``fidelity-framed-00`` to the last."""
    report = " ".join(REPORT.read_text(encoding="utf-8").split())  # lines rewrap
    cited = set()
    for prefix in SWEEPS:
        numbered = sorted(s.name for s in SCENARIOS if re.fullmatch(prefix + r"\d+", s.name))
        if f"`{numbered[0]}` to `{numbered[-1]}`" in report:
            cited.update(numbered)
    missing = [s.name for s in SCENARIOS if f"`{s.name}`" not in report and s.name not in cited]
    assert missing == []


def test_the_package_never_imports_the_probe():
    package = Path(autoforge.__file__).parent
    users = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "antigravity_probe" in path.read_text(encoding="utf-8")
    ]
    assert users == []


# -- the prototype classifier --------------------------------------------------


def stream(*events: dict[str, Any]) -> list[str]:
    return [json.dumps(e) + "\n" for e in events]


CID = "c"
INIT = {"event": "init", "conversation_id": CID, "init": {}}


def step(index: int, step_type: str, state: str = "DONE", **more: Any) -> dict[str, Any]:
    update = {"conversation_id": CID, "step_index": index, "state": state, "step_type": step_type}
    return {"event": "step_update", "step_update": {**update, **more}}


USER = step(0, "user_input")


def agent(index: int, delta: str, state: str = "DONE") -> dict[str, Any]:
    return step(index, "agent_response", state, text_delta=delta)


def tool(index: int, state: str) -> dict[str, Any]:
    return step(index, "tool", state)


def result(status: str = "SUCCESS", **more: Any) -> dict[str, Any]:
    """A result of the shape agy wrote: an ERROR one says what failed and why it ended."""
    failed = {"error": "failed", "finished_reason": "error"} if status == "ERROR" else {}
    body = {"conversation_id": CID, "status": status, "response": "", **failed, **more}
    return {"event": "result", "result": body}


REFUSAL = {"conversation_id": "", "status": "ERROR", "response": ""}
REFUSAL.update(error="Error: empty prompt", finished_reason="error")


KINDS = {
    "completed",
    "protocol",
    "no_result",
    "interrupted",
    "print_timeout",
    "usage_limit",
    "transient",
    "error",
    "denied",
    "ambiguous",
    "empty_reply",
    "killed",
    "incomplete_capture",
}


def test_only_the_last_agent_response_is_the_reply():
    """``result.response`` joins every agent response of the run, a retried
    attempt's cut text and a second turn's reply included (``background-task``,
    ``cut-stream-then-success``); the reply is the last response's deltas."""
    lines = stream(
        INIT,
        USER,
        agent(1, "partial "),
        step(2, "error_message"),
        agent(3, "Final ", "ACTIVE"),
        agent(3, "reply"),
        result(),
    )
    assert classify(reduce_stream(lines, []), 0, WHOLE) == Outcome("completed", reply="Final reply")


@pytest.mark.parametrize(
    ("lines", "exit_code", "kind"),
    [
        pytest.param(["not json\n"], 0, "protocol", id="not-json"),
        pytest.param(["[" * 100_000 + "\n"], 0, "protocol", id="nested-too-deep"),
        pytest.param(stream({"event": "surprise"}), 0, "protocol", id="unknown-event"),
        pytest.param(stream(INIT, agent(1, "a"), result(), result()), 0, "protocol", id="two"),
        pytest.param(stream(INIT, result(), agent(1, "late")), 0, "protocol", id="after-result"),
        pytest.param(
            stream(INIT, tool(2, "ACTIVE"), agent(3, "done"), result()),
            0,
            "protocol",
            id="tool-never-ended",
        ),
        # the stream ends while the reply is still being written
        pytest.param(
            stream(INIT, USER, agent(1, "a whole-looking reply", "ACTIVE"), result()),
            0,
            "protocol",
            id="response-never-ended",
        ),
        pytest.param(
            stream(INIT, USER, agent(1, "a", "ERROR"), result()), 0, "protocol", id="response-error"
        ),
        # no init: a SUCCESS result alone, or steps before any init
        pytest.param(stream(result()), 0, "protocol", id="success-without-init"),
        pytest.param(stream(USER, agent(1, "a"), result()), 0, "protocol", id="steps-no-init"),
        pytest.param(stream(USER, INIT, agent(1, "a"), result()), 0, "protocol", id="late-init"),
        pytest.param(stream(INIT, INIT, agent(1, "a"), result()), 0, "protocol", id="two-inits"),
        # step identities
        pytest.param(
            stream(INIT, agent(3, "a"), agent(1, "b"), result()), 0, "protocol", id="index-back"
        ),
        pytest.param(
            stream(INIT, agent(1, "a", "ACTIVE"), tool(1, "DONE"), result()),
            0,
            "protocol",
            id="type-changed",
        ),
        pytest.param(
            stream(INIT, agent(1, "a"), agent(1, "b"), result()), 0, "protocol", id="after-done"
        ),
        # the reply is not the run's last step
        pytest.param(
            stream(INIT, USER, agent(1, "a"), tool(2, "ACTIVE"), tool(2, "DONE"), result()),
            0,
            "empty_reply",
            id="tool-after-reply",
        ),
        # an unanswered question is an `unknown` step (ask-question); so is anything new
        pytest.param(
            stream(INIT, USER, agent(1, ""), step(2, "unknown"), agent(3, "reply"), result()),
            0,
            "ambiguous",
            id="unknown-step",
        ),
        pytest.param(
            stream(INIT, USER, step(1, "plan"), agent(2, "reply"), result()),
            0,
            "ambiguous",
            id="novel-step-type",
        ),
        pytest.param(stream(INIT, agent(1, "a")), 0, "no_result", id="no-result"),
        pytest.param([], 0, "no_result", id="nothing"),
        pytest.param(stream(INIT, agent(1, "a"), result()), 1, "error", id="exit-1"),
        pytest.param(stream(INIT, agent(1, "a"), result("ERROR")), 0, "error", id="error-exit-0"),
        # agy's refusal before any session: an ERROR result alone (stdin-whitespace)
        pytest.param(stream({"event": "result", "result": REFUSAL}), 1, "error", id="refusal"),
        pytest.param(
            stream({"event": "result", "result": {**REFUSAL, "conversation_id": "x"}}),
            1,
            "protocol",
            id="refusal-of-a-conversation",
        ),
        pytest.param(
            stream(INIT, result(denied_actions=[{"action": "file"}])), 0, "denied", id="denied"
        ),
        pytest.param(stream(INIT, result()), 0, "empty_reply", id="no-step"),
        pytest.param(stream(INIT, agent(1, "  \n"), result()), 0, "empty_reply", id="blank"),
    ],
)
def test_no_fault_reads_as_a_completed_run(lines, exit_code, kind):
    assert classify(reduce_stream(lines, []), exit_code, WHOLE).kind == kind


GOOD = [INIT, USER, agent(1, "reply"), result()]
GONE = object()  # the member removed
# (record of GOOD, member path, value): each a shape the contract refuses
MALFORMED = [
    *((0, ("conversation_id",), v) for v in (GONE, None, 0, "", [])),
    *((0, ("init",), v) for v in (GONE, None, [], "x")),
    *((2, ("step_update",), v) for v in (None, [], "x", 0)),
    *((2, ("step_update", "conversation_id"), v) for v in (GONE, None, "other", 0)),
    *((2, ("step_update", "step_index"), v) for v in (GONE, None, "1", -1, True, 1.5)),
    *((2, ("step_update", "state"), v) for v in (GONE, None, "", "WAITING", 0)),
    *((2, ("step_update", "step_type"), v) for v in (GONE, None, "", 7, [])),
    *((2, ("step_update", "text_delta"), v) for v in (None, 7, ["reply"], {"t": "reply"})),
    *((3, ("result",), v) for v in (None, [], "SUCCESS", 0)),
    *((3, ("result", "status"), v) for v in (GONE, None, "success", "WAITING", 0)),
    *((3, ("result", "conversation_id"), v) for v in (GONE, None, "other")),
    *((3, ("result", "response"), v) for v in (GONE, None, 0, [])),
    # a SUCCESS that also carries an error or a reason to have ended early
    *((3, ("result", "error"), v) for v in (None, 0, {}, "", "boom")),
    *((3, ("result", "finished_reason"), v) for v in (None, 0, [], "", "error", "interrupted")),
    *(
        (3, ("result", "denied_actions"), v)
        for v in (None, [], "command", 0, {"action": "x"}, [None], [{}], [{"action": 1}])
    ),
    *((3, ("result", "denied_actions"), v) for v in ([{"action": ""}],)),
]


def mutated(events: list[dict[str, Any]], index: int, path: tuple[str, ...], value: Any) -> list:
    out = copy.deepcopy(events)
    holder = out[index]
    for key in path[:-1]:
        holder = holder[key]
    if value is GONE:
        del holder[path[-1]]
    else:
        holder[path[-1]] = value
    return out


def test_a_well_formed_run_is_completed():
    assert classify(reduce_stream(stream(*GOOD), []), 0, WHOLE) == Outcome(
        "completed", reply="reply"
    )


@pytest.mark.parametrize(
    ("index", "path", "value"),
    MALFORMED,
    ids=[f"{'.'.join(p)}={'gone' if v is GONE else repr(v)}" for _, p, v in MALFORMED],
)
def test_a_malformed_value_is_a_protocol_failure_never_an_exception(index, path, value):
    lines = stream(*mutated(GOOD, index, path, value))
    outcome = classify(reduce_stream(lines, []), 0, WHOLE)
    assert outcome.kind == "protocol", outcome


JUNK = (None, True, -1, 1.5, "", "x", [], [1], {}, {"x": 1})


def test_no_value_anywhere_in_a_recorded_stream_raises(committed):
    """Every member of every record of a few recorded runs, replaced by each
    of :data:`JUNK`: the reducer and classifier always answer, with a known
    kind. (Which of these still complete is the targeted test's business.)"""
    for name in ("success-chunks", "background-task", "ask-question", "print-timeout-background"):
        lines = [line for _, line in committed[name]["stdout"]]
        events = [json.loads(line) for line in lines if line.strip()]
        for index, event in enumerate(events):
            paths = [(k,) for k in event] + [
                (k, sub) for k, v in event.items() if isinstance(v, dict) for sub in v
            ]
            for path in paths:
                for value in (GONE, *JUNK):
                    junk = stream(*mutated(events, index, path, value))
                    assert classify(reduce_stream(junk, []), 0, WHOLE).kind in KINDS, (name, path)


def typed(err: object, exit_code: int = 3, *more: str, capture: Capture = WHOLE) -> str:
    lines = stream(INIT, result("ERROR", finished_reason="error"))
    line = err if isinstance(err, str) else json.dumps(err)
    return classify(reduce_stream(lines, ["AGY_ERROR: " + line, *more]), exit_code, capture).kind


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (429, "usage_limit"),
        (408, "transient"),
        (500, "transient"),
        (503, "transient"),
        (599, "transient"),
        (403, "error"),
        (499, "error"),
        (600, "error"),  # no HTTP status of the contract's transient range
        (99, "error"),
        (429.0, "error"),  # an integer, never a float that equals one
        (408.0, "error"),
        (503.5, "error"),
        (True, "error"),
        ("429", "error"),
        (None, "error"),
    ],
)
def test_a_failed_turn_is_typed_from_an_integer_http_code_alone(code, kind):
    """The contract's typing (the report's section 9): an integer 429 is a
    usage limit, an integer 408 or one of 500 to 599 is transient, anything
    else an error. ``retryable`` is not what the CLI did: it retried a cut
    stream nine times and then called it not retryable
    (``cut-stream-always``), so it is never read."""
    err = {"error_code": code, "code_kind": "http", "retryable": True}
    assert typed(err) == kind
    assert typed({**err, "retryable": False}) == kind


def test_an_agy_error_of_another_shape_or_count_types_nothing():
    for err in (
        {"error_code": 2, "code_kind": "grpc", "status": "UNKNOWN", "retryable": False},
        {"error_code": 429, "code_kind": "grpc"},
        {"error_code": 429},
        "[429]",
        '"429"',
        "{",
        "null",
        '{"error_code": 429, "code_kind": "http", "error_code": 400}',  # a member twice
        '{"error_code": 429, "code_kind": "http", "x": NaN}',
        "[" * 100_000,
    ):
        assert typed(err) == "error", err
    # two typed lines: which one failed the turn is not known
    twice = json.dumps({"error_code": 429, "code_kind": "http"})
    assert typed(twice, 3, "AGY_ERROR: " + twice) == "error"


@pytest.mark.parametrize(
    "capture",
    [
        Capture(exited=True, stdout_eof=True, stderr_eof=False),
        Capture(exited=True, stdout_eof=True, stderr_eof=True, stderr_truncated=True),
    ],
    ids=["stderr-not-at-eof", "stderr-truncated"],
)
def test_a_partial_stderr_types_no_failure(capture):
    """A stderr not read to its end, or with output dropped, may have held a
    second ``AGY_ERROR`` line: its one line proves no type (R3-F3 of PR
    #530), and the failure stays an error. The exit and stdout change nothing."""
    for code in (429, 503, 408):
        err = {"error_code": code, "code_kind": "http"}
        assert typed(err) != "error"
        assert typed(err, capture=capture) == "error"


def test_a_success_with_any_sign_of_failure_is_not_completed():
    """Exit 0 and SUCCESS are needed, and are not enough: a SUCCESS at
    another exit, or with a model API error on stderr, contradicts itself."""
    good = stream(*GOOD)
    assert classify(reduce_stream(good, []), 1, WHOLE).kind == "error"
    assert classify(reduce_stream(good, []), 3, WHOLE).kind == "error"
    err = "AGY_ERROR: " + json.dumps({"error_code": 503, "code_kind": "http"})
    assert classify(reduce_stream(good, [err]), 0, WHOLE).kind == "error"


def test_a_stderr_that_is_not_utf8_neither_completes_nor_types():
    """A stderr line with a byte that is not UTF-8 was not read as it is:
    no AGY_ERROR line can be ruled out, or known to be the only one (R4-F3
    of PR #530). A genuine U+FFFD is UTF-8, and changes nothing."""
    good = stream(*GOOD)
    assert classify(reduce_stream(good, ["notice \ufffd"]), 0, WHOLE).kind == "completed"
    red = reduce_stream(good, ["notice \udcff"])
    assert red.stderr_undecodable == 1
    outcome = classify(red, 0, WHOLE)
    assert outcome.kind == "incomplete_capture" and "not UTF-8" in outcome.reason
    err = {"error_code": 503, "code_kind": "http"}
    assert typed(err) == "transient"
    assert typed(err, 3, "notice \udcff") == "error"
    assert typed(json.dumps(err)[:-1] + ', "x": "\udcff"}') == "error"


GOOD_LINES = stream(*GOOD)
# GOOD's reply with the byte 0xFF in its text, as read_stream carries it
NOT_UTF8_REPLY = GOOD_LINES[2].replace("reply", "reply \udcff")
DUPLICATE_STATUS = (
    '{"event": "result", "result": {"conversation_id": "c", "status": "ERROR", '
    '"status": "SUCCESS", "response": ""}}\n'
)


@pytest.mark.parametrize(
    ("lines", "violation"),
    [
        # the result, or any record, without the LF that ends it
        (GOOD_LINES[:-1] + [GOOD_LINES[-1].rstrip("\n")], "record 4 is not ended by LF"),
        ([GOOD_LINES[0].rstrip("\n"), *GOOD_LINES[1:]], "record 1 is not ended by LF"),
        # a record skipped is a violation, never a gap: blank, empty or padded lines
        ([*GOOD_LINES[:2], "\n", *GOOD_LINES[2:]], "record 3 is blank"),
        ([*GOOD_LINES[:2], "   \n", *GOOD_LINES[2:]], "record 3 is blank"),
        ([*GOOD_LINES[:-1], " " + GOOD_LINES[-1]], "record 4 has whitespace around its object"),
        ([*GOOD_LINES[:-1], GOOD_LINES[-1][:-1] + "\r\n"], "record 4 has whitespace"),
        # a line a bound cut: the probe's, or the reader's own
        (
            [*GOOD_LINES[:-1], GOOD_LINES[-1][:20] + PROBE_CUT + "9000 chars>"],
            "record 4 was cut by the probe's line limit",
        ),
        # a member twice, at the top or nested: which value counts is the reader's guess
        ([*GOOD_LINES[:-1], DUPLICATE_STATUS], "record 4 gives the member 'status' twice"),
        (
            [GOOD_LINES[0][:-2] + ', "event": "init"}\n', *GOOD_LINES[1:]],
            "record 1 gives the member 'event' twice",
        ),
        # numbers JSON does not have
        (
            [*GOOD_LINES[:-1], GOOD_LINES[-1][:-2] + ', "duration_seconds": NaN}\n'],
            "record 4 is not JSON",
        ),
        (
            [*GOOD_LINES[:-1], GOOD_LINES[-1][:-2] + ', "duration_seconds": 1e999}\n'],
            "record 4 is not JSON",
        ),
        # a byte that is not UTF-8, as the recorder carries it (read_stream):
        # inside a JSON string, where a replacement would have made it a record
        (
            [*GOOD_LINES[:2], NOT_UTF8_REPLY, GOOD_LINES[3]],
            "record 3 is not UTF-8",
        ),
    ],
    ids=[
        "result-without-lf",
        "init-without-lf",
        "empty-line",
        "blank-line",
        "leading-space",
        "crlf",
        "cut-by-the-probe",
        "duplicate-nested-status",
        "duplicate-top-level",
        "nan",
        "infinite",
        "not-utf8",
    ],
)
def test_a_record_that_is_not_one_whole_line_is_a_violation(lines, violation):
    red = reduce_stream(lines, [])
    assert any(v.startswith(violation) for v in red.violations), red.violations
    assert classify(red, 0, WHOLE).kind == "protocol"


def test_a_line_over_the_reader_bound_is_a_violation_not_a_skip():
    red = reduce_stream(GOOD_LINES, [], line_bound=len(GOOD_LINES[0]) - 1)
    assert red.violations[0].startswith("record 1 is longer than the line bound")
    assert classify(red, 0, WHOLE).kind == "protocol"
    assert classify(reduce_stream(GOOD_LINES, [], line_bound=10_000), 0, WHOLE).kind == "completed"


def partial(**flags: bool) -> Capture:
    """A capture of a process that exited on its own, whole but for ``flags``."""
    return Capture(**{"exited": True, "stdout_eof": True, "stderr_eof": True, **flags})


@pytest.mark.parametrize(
    ("capture", "kind"),
    [
        (WHOLE, "completed"),
        (partial(exited=False), "killed"),
        (partial(stdout_eof=False), "incomplete_capture"),
        (partial(stderr_eof=False), "incomplete_capture"),
        (partial(stdout_truncated=True), "incomplete_capture"),
        (partial(stderr_truncated=True), "incomplete_capture"),
        (Capture(False, False, False, True, True), "killed"),
    ],
    ids=[
        "whole",
        "not-exited",
        "stdout-not-at-eof",
        "stderr-not-at-eof",
        "stdout-truncated",
        "stderr-truncated",
        "nothing-whole",
    ],
)
def test_a_whole_looking_run_completes_only_on_a_whole_capture(capture, kind):
    """A stream read short proves nothing: the process ended by a limit or a
    signal, a pipe some survivor still held (an abandoned capture), or a
    capture bound that dropped output each keep a well-formed stream from
    completing, on stderr as on stdout: completion needs no ``AGY_ERROR``
    line, and part of stderr cannot show there was none (R3-F3 of PR #530).
    They never turn a failure into anything else."""
    assert classify(reduce_stream(GOOD_LINES, []), 0, capture).kind == kind
    failed = stream(INIT, result("ERROR"))
    assert classify(reduce_stream(failed, []), 3, capture).kind == "error"


def run_record(lines: list[str], exit_code: int = 1, **more: Any) -> dict[str, Any]:
    record = {
        "stdout": [[0.1, line] for line in lines],
        "stderr": [],
        "exit": exit_code,
        "killed_by_probe": False,
        "signalled": None,
        "processes_left": [],
        "capture": dict(WHOLE_CAPTURE),
    }
    record.update(more)
    return record


def test_a_record_is_read_with_its_own_capture(committed):
    """Each record's ``capture`` is the executor's report for it; a record
    without one, or with a flag that is not a boolean, has no whole capture,
    and an exit status that is a signal's is no exit of the process's own,
    whoever sent it (R3-F3 of PR #530)."""
    assert capture_of(committed["success-stream-json"]) == WHOLE
    assert capture_of(committed["detached-child"]) == WHOLE
    assert capture_of(committed["hang-no-limit"]).exited is False
    assert capture_of(committed["sigint-during-request"]).exited is False
    good = run_record(stream(*GOOD), exit_code=0)
    assert capture_of(good) == WHOLE and outcome_of(good).kind == "completed"
    # the synthetic success the review built: whole on stdout, stderr not at its end
    for key, value in (
        ("stderr_eof", False),
        ("stdout_eof", False),
        ("stderr_truncated", True),
        ("stdout_truncated", True),
        ("stderr_eof", None),
        ("stderr_truncated", None),
        ("stdout_truncated", 0),
        # a read that failed: whatever it read, it stopped before the end (R4-F3)
        ("stdout_error", "OSError: [Errno 5] Input/output error"),
        ("stderr_error", "ValueError: I/O operation on closed file"),
        ("stdout_error", ""),
    ):
        short = copy.deepcopy(good)
        short["capture"][key] = value
        assert not capture_of(short).whole, (key, value)
        assert outcome_of(short).kind == "incomplete_capture", (key, value)
    for key in CAPTURE_KEYS:
        short = copy.deepcopy(good)
        del short["capture"][key]
        assert outcome_of(short).kind == "incomplete_capture", key
    assert outcome_of({**good, "capture": None}).kind == "incomplete_capture"
    # a signal the probe did not send: the status is negative, the run no exit of its own
    for code in (-11, -9, -15, 1.0, None, True):
        assert capture_of({**good, "exit": code}).exited is False, code
    began = stream(INIT, USER, agent(1, "half", "ACTIVE"))
    assert outcome_of(run_record(began, exit_code=-11)).kind == "no_result"


# -- the recorder's read of agy's streams, at their bytes ------------------------


def _read(data: bytes) -> probe.StreamRead:
    """``data`` as agy writes it on a pipe, read by the recorder's own reader."""
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb") as w:
        w.write(data)
    got = probe.StreamRead()
    with os.fdopen(read_fd, "rb") as r:
        read_stream(r, time.monotonic(), got)
    return got


def _record_of_bytes(stdout: bytes, stderr: bytes = b"", exit_code: int = 0) -> dict[str, Any]:
    """A run's record as the recorder makes it from these bytes on stdout and stderr."""
    record = run_record([], exit_code=exit_code)
    for name, data in (("stdout", stdout), ("stderr", stderr)):
        got = _read(data)
        record[name] = got.lines
        record["capture"].update(
            {f"{name}_eof": got.eof, f"{name}_truncated": got.truncated, f"{name}_error": got.error}
        )
    return record


GOOD_BYTES = "".join(stream(*GOOD)).encode()
# GOOD's reply with the byte 0xFF inside its JSON string, and with a genuine U+FFFD
INVALID_BYTES = GOOD_BYTES.replace(b'"reply"', b'"reply \xff"')
GENUINE_FFFD = GOOD_BYTES.replace(b'"reply"', b'"reply \xef\xbf\xbd"')


def test_the_recorder_reads_agys_bytes_as_they_are():
    """R4-F3 of PR #530, at the byte-reading boundary: the recorder once
    decoded each line with replacement, so a reply holding a byte that is
    not UTF-8 inside its JSON string became an admissible record, and the
    run completed. Now the byte is carried as it is (``surrogateescape``),
    the line is a violation, and the record is written and read back with
    it; a genuine U+FFFD, three bytes of UTF-8, is a character like any
    other, and its run completes."""
    assert outcome_of(_record_of_bytes(GOOD_BYTES)).kind == "completed"
    genuine = outcome_of(_record_of_bytes(GENUINE_FFFD))
    assert (genuine.kind, genuine.reply) == ("completed", "reply \ufffd")
    invalid = _record_of_bytes(INVALID_BYTES)
    outcome = outcome_of(invalid)
    assert outcome.kind == "protocol" and "record 3 is not UTF-8" in outcome.reason
    # what replacement made of the same bytes: a whole record, and a completed run
    replaced = [line.decode("utf-8", "replace") for line in INVALID_BYTES.splitlines(True)]
    assert classify(reduce_stream(replaced, []), 0, WHOLE).kind == "completed"
    # written as committed and read back, the byte is still there
    text = probe.record_text(invalid)
    assert text.isascii() and json.loads(text) == invalid
    assert outcome_of(json.loads(text)).kind == "protocol"
    assert not probe.record_text(_record_of_bytes(GENUINE_FFFD)).isascii()
    # on stderr: not read as it is, so no completion, and no failure typed from it
    assert outcome_of(_record_of_bytes(GOOD_BYTES, b"notice \xef\xbf\xbd\n")).kind == "completed"
    assert outcome_of(_record_of_bytes(GOOD_BYTES, b"notice \xff\n")).kind == "incomplete_capture"
    failed = "".join(stream(INIT, result("ERROR", finished_reason="error"))).encode()
    err = b"AGY_ERROR: " + json.dumps({"error_code": 503, "code_kind": "http"}).encode() + b"\n"
    assert outcome_of(_record_of_bytes(failed, err, 3)).kind == "transient"
    assert outcome_of(_record_of_bytes(failed, err + b"\xfe\n", 3)).kind == "error"


class _FailingStream:
    """A binary stream whose reads give ``lines``, one each, then fail as a broken pipe would."""

    def __init__(self, *lines: bytes) -> None:
        self.lines = list(lines)

    def readline(self, limit: int = -1) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        raise OSError(errno.EIO, "Input/output error")

    def read1(self, size: int = -1) -> bytes:
        raise OSError(errno.EIO, "Input/output error")


def test_only_a_read_that_returned_eof_is_eof(monkeypatch):
    """R4-F3 of PR #530: a reader that stopped on an exception was once
    reported as having reached EOF, so a whole-looking stdout of a run
    whose read failed completed it. Now only a read that returned no byte
    sets ``eof``; one that raised says why, before the bound and past it,
    and the run is no completed one."""
    lines = stream(*GOOD)
    got = probe.StreamRead()
    read_stream(_FailingStream(*(line.encode() for line in lines)), time.monotonic(), got)
    assert [line for _, line in got.lines] == lines
    assert got.eof is False and got.error == "OSError: [Errno 5] Input/output error"
    record = _record_of_bytes(GOOD_BYTES)
    assert outcome_of(record).kind == "completed"
    record["stdout"] = got.lines
    record["capture"].update(stdout_eof=got.eof, stdout_error=got.error)
    assert outcome_of(record).kind == "incomplete_capture"
    # past the capture bound the stream is read on, and held to the same rule
    monkeypatch.setattr(probe, "CAPTURE_LINES", 1)
    past = probe.StreamRead()
    read_stream(_FailingStream(b"one\n", b"two\n"), time.monotonic(), past)
    assert (past.truncated, past.eof, past.error is not None) == (True, False, True)
    ended = _read(b"one\ntwo\nthree")
    assert (ended.truncated, ended.eof, ended.error) == (True, True, None)
    assert [line for _, line in ended.lines] == ["one\n"]


def test_neither_recovery_flag_is_ever_set(committed):
    """The flags that reach the engine's recovery (the report's section 9).
    ``reply_delivered`` stays unset: agy writes a reply only with its result.
    ``result_less_exit`` stays unset too: the engine relaunches on it, and
    nothing recorded shows an exit of agy's own, without a result, inside a
    session the model had begun; ``init`` and a ``user_input`` step are
    written before the model answers (``hang-no-limit``), so they show no
    running session (R3-F3 of PR #530). Not even on a whole capture of a
    stream the model had begun to answer."""
    for name, record in committed.items():
        assert result_flags(record) == {"reply_delivered": False, "result_less_exit": False}, name
    assert [json.loads(line)["event"] for _, line in committed["hang-no-limit"]["stdout"]] == [
        "init",
        "step_update",
    ]
    assert '"user_input"' in committed["hang-no-limit"]["stdout"][1][1]
    began = stream(INIT, USER, agent(1, "half", "ACTIVE"))
    called = stream(INIT, USER, agent(1, ""), tool(2, "ACTIVE"))
    err = "AGY_ERROR: " + json.dumps({"error_code": 503, "code_kind": "http"})
    for record in (
        run_record(began),
        run_record(began, exit_code=0),
        run_record(called),
        run_record(stream(INIT, USER)),  # what agy writes before any answer
        run_record(stream(INIT)),
        run_record([]),
        run_record(began, exit_code=-11),  # a signal nobody in the probe sent
        run_record(began, killed_by_probe=True),
        run_record(began, signalled="SIGTERM"),
        run_record(began, stderr=[[0.2, err + "\n"]]),
        run_record([*began[:-1], began[-1].rstrip("\n")]),  # its last line cut short
        run_record(stream(INIT, USER, agent(1, "a"), result())),
        run_record(stream(INIT, USER, agent(1, "a"), result()), exit_code=0),
    ):
        assert result_flags(record) == {"reply_delivered": False, "result_less_exit": False}


def test_the_committed_successes_are_the_only_completed_runs(committed):
    """Native failures and soft denials never read as a completed run (#528's
    acceptance criterion): of every committed record, only those whose result
    is SUCCESS with no denial, at exit 0, classify as completed, and each such
    reply holds exactly one CONTROL_RESULT block where the scripted reply did."""
    for name, record in committed.items():
        outcome = outcome_of(record)
        res = result_of(record) or {}
        if outcome.kind == "completed":
            assert record["exit"] == 0 and res.get("status") == "SUCCESS", name
            assert not res.get("denied_actions"), name
            assert '"step_type":"unknown"' not in text(record, "stdout"), name
            if REPLY.splitlines()[0] in outcome.reply:
                assert outcome.reply.count("<<<CONTROL_RESULT>>>") == 1, name


def test_an_unanswered_question_is_never_a_completed_run(committed):
    """``ask-question``: the model asked, agy answered with nothing (``A1: ``
    in the tool result the model got) and went on to a SUCCESS with a whole
    reply. The stream shows the question only as an ``unknown`` step, so the
    run is ambiguous, not completed."""
    record = committed["ask-question"]
    assert record["requests"][1]["tool_results"][0].endswith('A1: "}')
    assert result_of(record)["status"] == "SUCCESS" and record["exit"] == 0
    outcome = outcome_of(record)
    assert outcome.kind == "ambiguous"
    assert "2 (unknown)" in outcome.reason


# -- what a prompt loses -------------------------------------------------------


def test_stripped_is_what_the_sweep_measured():
    assert stripped("a​b‮c\x07d\te\r\nf\U000f0000g ") == "abcd\te\r\nfg "
    # NUL, an unassigned code point and a noncharacter go too (fidelity-all-*)
    assert stripped("\x00͸x\U0010ffff一") == "x一"
    probes = (0x00, 0x09, 0x20, 0x85, 0xA0, 0x378, 0x2028, 0x4E00, 0xFFFD, 0x10FFFF)
    assert [is_dropped(c) for c in probes] == [
        True,
        False,
        False,
        False,
        False,
        True,
        False,
        False,
        False,
        True,
    ]


def test_the_complete_measurements_cover_every_scalar_value_and_are_the_table(committed):
    """Through both transports: text on stdin, and one stream-json message
    escaped as the recommended adapter would write it, astral characters as
    surrogate pairs and a line up to three times the prompt."""
    assert complete_sweep_problems(committed) == []
    for prefix in SWEEPS:
        measured = [r for name, r in committed.items() if name.startswith(prefix)]
        assert sum(r["facts"]["all"]["sent"] for r in measured) == SCALAR_VALUES == 1_112_064
        assert len(measured) == 31
        assert dropped_of(committed, prefix) == [list(r) for r in read_table()]
    framed = [committed[s.name] for s in SCENARIOS if s.name.startswith(FRAMED_PREFIX)]
    assert max(r["stdin_bytes"] for r in framed) == 450_082
    assert DROPPED_TABLE.parent == FIXTURES
    # the counts the report gives (its section 3)
    table = read_table()
    removed = sum(last - first + 1 for first, last in table)
    assert (len(table), removed, SCALAR_VALUES - removed) == (744, 952_427, 159_637)


def test_the_proposed_prompt_bound_arrives_whole_through_the_framing(committed):
    """Three prompts of 160,000 UTF-8 bytes, the bound the report proposes,
    each of characters agy keeps, framed, arrived identical, closing tag and
    metadata kept: a large mixed sample, mostly astral, in a line of 416,882
    bytes (``fidelity-near-limit-stream-json``); one astral character
    alone, 480,046 bytes (``fidelity-astral-stream-json``); and the widest
    framing measured, a ``|`` and VTs, 960,041 bytes
    (``fidelity-widest-stream-json``, R3-F6 of PR #530). It is not the
    longest line the gate admits: 160,000 VTs pass its size bound and its
    table and frame to 960,046 bytes, a blank prompt whose line was not
    measured (R4-F6), and the report states that bound as the one to use."""
    lines = {}
    for name, make in (
        (NEAR_LIMIT, probe.near_limit_prompt),
        (ASTRAL, probe.astral_prompt),
        (WIDEST, probe.widest_prompt),
    ):
        record, sent = committed[name], make()
        assert len(sent.encode()) == probe.PROMPT_LIMIT == 160_000, name
        assert SCENARIO_NAMED[name].argv == probe.sj_run(), name
        lines[name] = record["stdin_bytes"]
        assert lines[name] == len(probe.stream_message(sent).encode()), name
        assert not any(is_dropped(ord(c)) for c in sent), name
        fidelity = record["facts"]["fidelity"]
        assert fidelity["identical"] and fidelity["closing_tag_kept"], name
        assert fidelity["truncated_bytes"] is None, name
        assert fidelity["sent_bytes"] == fidelity["received_bytes"] == 160_000, name
    assert lines == {NEAR_LIMIT: 416_882, ASTRAL: 480_046, WIDEST: 960_041}
    # the widest: no character agy keeps takes more framed bytes per byte of UTF-8
    envelope = len(probe.stream_message("").encode())
    escaped = json.encoder.encode_basestring_ascii  # what stream_message's json.dumps uses
    assert probe.stream_message("\x0b\U0001f680") == probe.stream_message("")[:-4] + (
        escaped("\x0b\U0001f680")[1:-1] + '"}}\n'
    )
    widths = {}
    for c in range(0x110000):
        if 0xD800 <= c <= 0xDFFF or is_dropped(c):
            continue
        ch = chr(c)
        widths[c] = (len(escaped(ch)) - 2) / len(ch.encode())
    assert max(widths.values()) == probe.WIDEST_ESCAPE == widths[0x0B]
    assert [c for c, w in widths.items() if w == probe.WIDEST_ESCAPE] == [0x0B]
    assert lines[WIDEST] == probe.WIDEST_ESCAPE * 160_000 - 5 + envelope
    # the longest line the gate admits: 160,000 VTs, blank, not measured (R4-F6)
    assert envelope == probe.ENVELOPE_BYTES == 46
    blank = "\x0b" * probe.PROMPT_LIMIT
    assert len(blank.encode()) == probe.PROMPT_LIMIT and not any(is_dropped(ord(c)) for c in blank)
    assert len(probe.stream_message(blank).encode()) == probe.FRAMED_BOUND == 960_046
    assert lines[WIDEST] == probe.FRAMED_BOUND - 5 == max(lines.values())
    report = " ".join(REPORT.read_text(encoding="utf-8").split())
    assert "960,046" in report and "960,041" in report


def test_a_measurement_that_lost_a_prompt_or_a_value_is_caught(committed):
    for prefix in SWEEPS:
        gaps = copy.deepcopy(committed)
        del gaps[prefix + "03"]
        found = sweep_problems(gaps, prefix)
        assert f"{prefix}03: no record" in found and any("starts at" in p for p in found)
        unclean = copy.deepcopy(committed)
        unclean[prefix + "nul"]["facts"]["all"]["dropped"] = None
        assert any("not measured cleanly" in p for p in sweep_problems(unclean, prefix))
        assert complete_sweep_problems(_kept(committed, prefix + "00")) == [
            f"what the {prefix}* prompts lost is not the table of dropped.txt"
        ]
        assert sweep_problems({}, prefix)[-1].endswith("does not cover every scalar value once")


def table_dir(tmp_path: Path, found: dict[str, dict[str, Any]]) -> Path:
    out = tmp_path / "records"
    out.mkdir()
    for name, record in found.items():
        (out / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")
    return out


def _without(found: dict[str, dict[str, Any]], *names: str) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in found.items() if k not in names}


def _changed(found: dict[str, Any], name: str, path: tuple[str, ...], value: Any) -> dict:
    """``found`` with one member of one record set to ``value`` (removed for GONE)."""
    out = copy.deepcopy(found)
    holder = out[name]
    for key in path[:-1]:
        holder = holder[key]
    if value is GONE:
        del holder[path[-1]]
    else:
        holder[path[-1]] = value
    return out


def _kept(found: dict[str, Any], name: str) -> dict[str, Any]:
    """``found`` as if ``name`` had kept its first dropped run, counts kept consistent."""
    out = copy.deepcopy(found)
    measured = out[name]["facts"]["all"]
    first, last = measured["dropped"].pop(0)
    measured["received"] += last - first + 1
    return out


def _unlabelled(found: dict[str, Any]) -> dict[str, Any]:
    """No build named anywhere, and no version record to say what printed it."""
    out = copy.deepcopy(_without(found, "version"))
    for record in out.values():
        del record["agy_version"], record["agy_sha256"]
    return out


def _both(found: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """The same change to the two NUL captures, as text and framed."""
    once = _changed(found, ALL_PREFIX + "nul", path, value)
    return _changed(once, FRAMED_PREFIX + "nul", path, value)


def _nul_copies(found: dict[str, Any], name_of: Any) -> dict[str, Any]:
    """``found`` and a copy of each NUL record, named ``name_of(prefix)``, that dropped U+0041.

    The review's case (R4-F4 of PR #530): one copy per transport, of the
    same build, its ``scenario`` its own name (one that starts with the
    sweep's prefix), and a dropped run of an ordinary letter.
    """
    out = copy.deepcopy(found)
    for prefix in SWEEPS:
        name = name_of(prefix)
        extra = copy.deepcopy(found[prefix + "nul"])
        extra["scenario"] = name
        extra["facts"]["all"]["dropped"] = [[0x41, 0x41]]
        out[name] = extra
    return out


A00, F00, F05 = ALL_PREFIX + "00", FRAMED_PREFIX + "00", FRAMED_PREFIX + "05"
OTHER_SHA = "0" * 64
# (the evidence, what the refusal names): each a directory ``table`` refuses
REFUSALS = {
    "empty": (lambda c: {}, "no record"),
    "no-sweep": (lambda c: _without(c, *(n for n in c if n.startswith(SWEEPS))), "no record"),
    "partial": (lambda c: _without(c, ALL_PREFIX + "17"), f"{ALL_PREFIX}17: no record"),
    "partial-framed": (lambda c: _without(c, FRAMED_PREFIX + "nul"), "nul: no record"),
    "mixed": (lambda c: _changed(c, ALL_PREFIX + "05", ("agy_version",), "1.6.0"), "more than"),
    "mixed-other": (lambda c: _changed(c, "help", ("agy_version",), "1.6.0"), "more than one"),
    "mixed-sha": (lambda c: _changed(c, "help", ("agy_sha256",), OTHER_SHA), "more than one"),
    # no label at all, and no version record: never one more label of a kind
    "unlabelled": (_unlabelled, "names no build version (None)"),
    "no-sha": (lambda c: _changed(c, A00, ("agy_sha256",), GONE), "names no build sha256"),
    "bad-version": (
        lambda c: _changed(c, A00, ("agy_version",), "None"),
        "names no build version ('None')",
    ),
    "no-version-record": (lambda c: _without(c, "version"), "no version record"),
    "version-not-printed": (
        lambda c: _changed(c, "version", ("stdout",), [[0.1, "9.9.9\n"]]),
        "did not print the version",
    ),
    # the two NUL captures with no model request: nothing was measured
    "nul-no-request": (lambda c: _both(c, ("requests",), []), "0 model requests, not 1"),
    "two-requests": (
        lambda c: _changed(c, F00, ("requests",), c[F00]["requests"] * 2),
        "2 model requests",
    ),
    "other-key": (
        lambda c: _changed(c, F00, ("requests",), [{**c[F00]["requests"][0], "key": "other"}]),
        "did not carry the synthetic key",
    ),
    "unclean": (
        lambda c: _changed(c, ALL_PREFIX + "02", ("facts", "all", "dropped"), None),
        "not measured cleanly",
    ),
    "not-framed": (lambda c: _changed(c, A00, ("facts", "all", "framed"), False), "framed is"),
    "framed-1": (lambda c: _changed(c, A00, ("facts", "all", "framed"), 1), "framed is 1"),
    "unordered": (lambda c: _changed(c, F05, ("facts", "all", "ordered"), False), "ordered is"),
    "foreign": (lambda c: _changed(c, F05, ("facts", "all", "foreign"), ["U+0041"]), "foreign"),
    # nothing dropped, yet fewer received than sent: the counts do not add up
    "empty-dropped": (lambda c: _changed(c, A00, ("facts", "all", "dropped"), []), "add up"),
    "dropped-junk": (
        lambda c: _changed(c, A00, ("facts", "all", "dropped"), [[3, 1]]),
        "not [first, last] runs",
    ),
    "dropped-outside": (
        lambda c: _changed(c, A00, ("facts", "all", "dropped"), [[0x10FFFF, 0x10FFFF]]),
        "outside what was sent",
    ),
    "dropped-overlap": (
        lambda c: _changed(c, A00, ("facts", "all", "dropped"), [[1, 8], [5, 9]]),
        "is not after the last",
    ),
    "received": (lambda c: _changed(c, F00, ("facts", "all", "received"), 7), "do not add up"),
    "spans": (lambda c: _changed(c, F00, ("facts", "all", "first"), 2), "first is 2"),
    "sent": (lambda c: _changed(c, F00, ("facts", "all", "sent"), 1), "sent is 1"),
    "transport": (lambda c: _changed(c, F00, ("argv",), probe.stdin_run()), "another transport"),
    "stdin-bytes": (lambda c: _changed(c, F00, ("stdin_bytes",), 12), "sent 12 bytes on stdin"),
    "prompt-cut": (
        lambda c: _changed(c, F00, ("facts", "fidelity", "truncated_bytes"), 9),
        "truncated_bytes is 9",
    ),
    "tag-lost": (
        lambda c: _changed(c, F00, ("facts", "fidelity", "closing_tag_kept"), False),
        "closing_tag_kept is False",
    ),
    "capture": (
        lambda c: _changed(c, F00, ("capture", "stderr_eof"), False),
        "capture is not whole",
    ),
    "exit": (lambda c: _changed(c, A00, ("exit",), 1), "exit 1"),
    "not-completed": (
        lambda c: _changed(c, A00, ("stdout",), c[A00]["stdout"][:-1]),
        "the run is no_result",
    ),
    "not-a-record": (lambda c: {**c, A00: [1, 2]}, f"{A00}: not a record"),
    "differ": (lambda c: _kept(c, F00), "lose different characters"),
    # a record the plan does not name, whatever it holds: stale, copied or
    # relabelled, it could otherwise put an ordinary letter in the table (R4-F4)
    "stale-copies": (
        lambda c: _nul_copies(c, lambda prefix: prefix + "nul-stale"),
        f"{ALL_PREFIX}nul-stale: no scenario of the probe's",
    ),
    "unplanned-chunk": (
        lambda c: _nul_copies(c, lambda prefix: prefix + "99"),
        f"{FRAMED_PREFIX}99: no scenario of the probe's",
    ),
    "relabelled": (
        lambda c: {**c, "help": copy.deepcopy(c[ALL_PREFIX + "nul"])},
        f"help: names the scenario '{ALL_PREFIX}nul', not its own",
    ),
}


def test_the_table_command_writes_the_committed_table_from_the_committed_records(
    committed, tmp_path
):
    out = table_dir(tmp_path, committed)
    assert write_table(out) == out / "dropped.txt"
    assert (out / "dropped.txt").read_bytes() == DROPPED_TABLE.read_bytes()
    assert not (out / "dropped.txt.new").exists()
    assert PINNED_SHA256 in DROPPED_TABLE.read_text(encoding="ascii")


BEFORE = "# the table before\n0000\n"


@pytest.mark.parametrize(("evidence", "refusal"), REFUSALS.values(), ids=REFUSALS.keys())
def test_the_table_command_refuses_invalid_evidence_and_keeps_the_table(
    committed, tmp_path, evidence, refusal
):
    """An empty, partial, unlabelled or mixed directory, or a record that is
    not the run its plan made (another transport, another span, a cut
    prompt, no model request), not a whole capture of a completed run, or
    not a clean measurement that adds up, would give a table that cannot
    tell no removal from no measurement (R2-F5, R3-F4 of PR #530): ``table``
    writes nothing, and the table already there stays as it was."""
    out = table_dir(tmp_path, evidence(committed))
    (out / "dropped.txt").write_text(BEFORE, encoding="ascii")
    with pytest.raises(ProbeError, match=re.escape(refusal)):
        write_table(out)
    assert (out / "dropped.txt").read_text(encoding="ascii") == BEFORE
    assert not (out / "dropped.txt.new").exists()


def test_no_record_outside_the_plan_reaches_the_table(committed, tmp_path):
    """R4-F4 of PR #530: the union once took every record whose own
    ``scenario`` started with a sweep's prefix, so two stale copies of the
    NUL records, one per transport, that had dropped U+0041 put it in both
    unions; the two agreed, and ``table`` would have written a table that
    strips an ordinary letter. Now the union reads the plan's records alone,
    by their names, so no extra record changes it, and ``table`` refuses
    such a directory and leaves the table as it was."""
    table = [list(r) for r in read_table()]
    for i, name_of in enumerate(
        (
            lambda prefix: prefix + "nul-stale",
            lambda prefix: prefix + "99",
            lambda prefix: "copy-of-" + prefix + "nul",
        )
    ):
        extra = _nul_copies(committed, name_of)
        for prefix in SWEEPS:
            assert dropped_of(extra, prefix) == table, name_of(prefix)
        assert any("no scenario of the probe's" in p for p in probe.table_problems(extra))
        (tmp_path / str(i)).mkdir()
        out = table_dir(tmp_path / str(i), extra)
        (out / "dropped.txt").write_bytes(DROPPED_TABLE.read_bytes())
        with pytest.raises(ProbeError, match="no scenario of the probe's"):
            write_table(out)
        assert (out / "dropped.txt").read_bytes() == DROPPED_TABLE.read_bytes()
    assert probe.record_name_problems(committed) == []


def test_the_table_command_refuses_on_its_command_line_too(committed, tmp_path):
    """The command itself: exit 1, the refusal on stderr, the table as it was,
    also for a record file that is not JSON at all."""
    out = table_dir(tmp_path, _without(committed, ALL_PREFIX + "17"))
    (out / "dropped.txt").write_text(BEFORE, encoding="ascii")
    (out / "junk.json").write_text("{not json", encoding="utf-8")
    argv = [sys.executable, "-I", str(Path(probe.__file__)), "table", "--records", str(out)]
    done = subprocess.run(argv, capture_output=True, text=True, env={})
    assert done.returncode == 1 and "no table written" in done.stderr
    assert "junk.json: not readable JSON" in done.stderr
    assert f"{ALL_PREFIX}17: no record" in done.stderr
    assert (out / "dropped.txt").read_text(encoding="ascii") == BEFORE
    assert not (out / "dropped.txt.new").exists()


def test_measure_all_reads_only_a_clean_subsequence():
    sent = probe.all_prompt("t", [(0x41, 0x45)])
    header, tail = sent[: sent.index("\n") + 1], probe.ALL_TRAILER
    assert measure_all(sent, header + "ACE" + tail)["dropped"] == [[0x42, 0x42], [0x44, 0x44]]
    assert measure_all(sent, sent)["dropped"] == []
    for got in (header + "AXCE" + tail, header + "CA" + tail, "ACE" + tail, header + "ACE"):
        assert measure_all(sent, got)["dropped"] is None, got


def test_the_table_is_general_category_c_but_six_controls():
    """What the complete measurement found, read with this Python's Unicode
    database: an assigned code point is removed exactly when its category is
    Cc, Cf or Co and it is not TAB, LF, VT, FF, CR or NEL. With Unicode 17.0,
    agy 1.5.13's own, every unassigned one (Cn) is removed too. An older
    database calls the characters assigned since unassigned (Python 3.14's
    16.0 misses 4,803), so it cannot judge those; a newer one could see agy
    remove characters it assigns, so it is not asked."""
    version = tuple(int(x) for x in unicodedata.unidata_version.split("."))
    if version > AGY_UNICODE:
        pytest.skip(f"Unicode {unicodedata.unidata_version} is newer than agy 1.5.13's 17.0.0")
    flags = bytearray(0x110000)
    for first, last in read_table():
        flags[first : last + 1] = b"\x01" * (last - first + 1)
    wrong, unassigned_kept = [], 0
    for c in range(0x110000):
        if 0xD800 <= c <= 0xDFFF:
            continue
        cat = unicodedata.category(chr(c))
        if cat == "Cn":
            unassigned_kept += not flags[c]
        elif bool(flags[c]) != (cat in ("Cc", "Cf", "Co") and c not in KEPT_CONTROLS):
            wrong.append(f"U+{c:04X} {cat}")
    assert wrong == []
    if version == AGY_UNICODE:
        assert unassigned_kept == 0


# -- the version, asked in the sandbox -----------------------------------------


def version_run(stdout: str, exit_code: int = 0, killed: bool = False):
    def run_one(agy: str, sc: Any, root: Path, shared: Any) -> dict[str, Any]:
        printed = stdout if sc.name == "version" else ""
        return {
            "scenario": sc.name,
            "exit": exit_code,
            "killed_by_probe": killed,
            "stdout": [[0.1, printed]] if printed else [],
            "stderr": [],
        }

    return run_one


def test_the_version_is_asked_in_the_sandbox_never_on_the_host(monkeypatch, tmp_path):
    """The recorder once ran ``agy --version`` on the host, in the caller's
    environment, home and network, unbounded. Now the ``version`` scenario
    asks it, through :func:`run_one` (``bwrap``, an empty home, no network,
    the scenario's timeout), first and once, and every record names the
    version that run printed; the probe starts nothing on the host."""
    calls: list[tuple[Any, ...]] = []
    sandboxed = version_run("9.9.9\n")

    def run_one(agy: str, sc: Any, root: Path, shared: Any) -> dict[str, Any]:
        calls.append((agy, sc.name, sc.argv, sc.timeout))
        return sandboxed(agy, sc, root, shared)

    def on_the_host(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"the probe started a process on the host: {args!r}")

    monkeypatch.setattr(probe, "run_one", run_one)
    monkeypatch.setattr(probe, "binary_sha256", lambda path: OTHER_SHA)
    monkeypatch.setattr(probe, "SANDBOX_PYTHON", sys.executable)
    monkeypatch.setattr(probe, "keyctl_number", lambda: 250)
    monkeypatch.setattr(probe.subprocess, "run", on_the_host)
    monkeypatch.setattr(probe.subprocess, "Popen", on_the_host)
    monkeypatch.setattr(probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    version, record = discover_version("/opt/agy", tmp_path, timeout=3)
    assert (version, record["scenario"]) == ("9.9.9", "version")
    assert calls == [("/opt/agy", "version", ["--version"], 3)]
    calls.clear()
    out = tmp_path / "out"
    assert record_all("/opt/agy", out, only={"success-stream-json"}) == ["success-stream-json"]
    assert [c[1] for c in calls] == ["version", "success-stream-json"]
    assert calls[0][3] == VERSION_SCENARIO.timeout == 15
    written = json.loads((out / "success-stream-json.json").read_text())
    assert (written["agy_version"], written["agy_sha256"]) == ("9.9.9", OTHER_SHA)
    assert not (out / "version.json").exists()
    calls.clear()
    assert sorted(record_all("/opt/agy", out, only={"version"})) == ["version"]
    assert [c[1] for c in calls] == ["version"]  # its own record is the discovery's
    assert json.loads((out / "version.json").read_text())["agy_version"] == "9.9.9"


@pytest.mark.parametrize(
    ("stdout", "exit_code", "killed"),
    [
        ("", 0, False),
        ("9.9.9\n", 1, False),
        ("9.9.9\n", -9, True),
        ("agy version 9.9.9\n", 0, False),
        ("9.9.9\nanother line\n", 0, False),
    ],
)
def test_a_version_run_that_printed_no_version_stops_the_recording(
    monkeypatch, tmp_path, stdout, exit_code, killed
):
    monkeypatch.setattr(probe, "run_one", version_run(stdout, exit_code, killed))
    monkeypatch.setattr(probe, "binary_sha256", lambda path: OTHER_SHA)
    monkeypatch.setattr(probe, "SANDBOX_PYTHON", sys.executable)
    monkeypatch.setattr(probe, "keyctl_number", lambda: 250)
    monkeypatch.setattr(probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(ProbeError, match="printed no version"):
        discover_version("/opt/agy", tmp_path)
    with pytest.raises(ProbeError):
        record_all("/opt/agy", tmp_path / "out")
    assert not (tmp_path / "out").exists()


# -- a new build, assessed by what it does ---------------------------------------


def relabelled(found: dict[str, dict[str, Any]], version: str) -> dict[str, dict[str, Any]]:
    """``found`` as a recording of another build, one that prints ``version``."""
    out = copy.deepcopy(found)
    for record in out.values():
        record["agy_version"] = version
        record["agy_sha256"] = OTHER_SHA
    out["version"]["stdout"] = [[0.2, version + "\n"]]
    return out


def test_a_build_of_another_version_is_assessed_by_its_behaviour(committed, tmp_path):
    """The report's C3 admits another build by this probe: a fresh recording
    is held to what agy does, never to the label 1.5.13, and the committed
    records keep theirs."""
    other = tmp_path / "1.6.0"
    other.mkdir()
    for name, record in relabelled(committed, "1.6.0").items():
        (other / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")
    fresh = records(other)
    assert mismatches(fresh) == []
    assert cross_record_problems(fresh) == []
    changed = copy.deepcopy(fresh)
    changed["deny-command"]["exit"] = 1
    assert "deny-command: exit is 1, the report says 0" in mismatches(changed)
    garbled = copy.deepcopy(fresh)
    garbled["version"]["stdout"] = [[0.2, "agy 1.6.0 (build 7)\n"]]
    assert cross_record_problems(garbled) == [
        "the version record did not print the version the records name"
    ]
    mixed = copy.deepcopy(fresh)
    mixed["help"]["agy_version"] = PINNED
    assert cross_record_problems(mixed)[0].startswith("the records name more than one build")
    assert {r["agy_version"] for r in records(FIXTURES).values()} == {PINNED}


# -- the probe itself, against a fake agy, in the sandbox ------------------------


def _keyring_works() -> bool:
    """Linux with a supported keyctl(2) number and permission to join a session
    keyring (a container's seccomp may refuse it)."""
    if sys.platform != "linux":
        return False
    try:
        launch = probe.keyring_launcher()
    except ProbeError:
        return False
    true = shutil.which("true") or "/bin/true"
    return subprocess.run([*launch, true], capture_output=True).returncode == 0


def _bwrap_works() -> bool:
    """bwrap with unprivileged user namespaces, the sandbox's interpreter, and a
    session keyring the launch can join (a container's seccomp may refuse it)."""
    if not _keyring_works() or shutil.which("bwrap") is None:
        return False
    if not Path(SANDBOX_PYTHON).is_file():
        return False
    argv = ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "--unshare-pid", "--", "true"]
    return subprocess.run(argv, capture_output=True).returncode == 0


# The host's one file of /etc the sandbox shows, where the host has it.
SHOWN_ETC = [f"/etc/{n}" for n in probe.HOST_ETC if Path("/etc", n).exists()]

needs_keyring = pytest.mark.skipif(
    not _keyring_works(),
    reason="needs Linux with keyctl(2) permission to join a session keyring",
)
needs_bwrap = pytest.mark.skipif(
    not _bwrap_works(),
    reason=f"needs Linux, {SANDBOX_PYTHON}, bwrap with unprivileged user namespaces "
    "and keyctl(2) to join a session keyring",
)

# A fake agy, run by the sandbox's interpreter: asked its version, it
# reports on stderr what it can reach of the host (each canary the test
# planted: a file, a variable's value, a socket by path, an abstract
# socket and a loopback port), tries to plant a link and a FIFO where the
# host could read them (the spec directory), and to write into the
# record's channel when told to, and prints a version. Asked anything
# else, it reports what it finds of the workspace's git repository.
FAKE_AGY = """#!SANDBOX_PYTHON -I
import errno, json, os, socket, sys, time

log = os.environ.get("FAKE_AGY_HOST_LOG")
if log:  # only a launch in the caller's environment sees it
    with open(log, "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
canary = CANARY


def attempt(action):
    try:
        action()
        return "done"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))


if sys.argv[1:] == ["--version"]:
    if HANG:
        time.sleep(600)
    # the spec directory: the last argument of the helper that started us
    args = open(f"/proc/{os.getppid()}/cmdline", "rb").read().split(b"\\0")
    spec_dir = os.path.dirname(args[-2].decode())
    planted = {
        "link": attempt(lambda: os.symlink(canary["file"], spec_dir + "/result.json")),
        "fifo": attempt(lambda: os.mkfifo(spec_dir + "/result.json")),
        "spec": attempt(lambda: open(spec_dir + "/spec.json", "a").close()),
    }
    if canary.get("flood"):  # into the record's channel, through bwrap's own process
        with open("/proc/1/fd/1", "wb") as f:
            f.write(canary["flood"].encode() * canary.get("times", 1))

    def reach(family, address):
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(2)
        try:
            s.connect(address)
            return "connected"
        except OSError as e:
            return errno.errorcode.get(e.errno, "timeout") if e.errno else "timeout"
        finally:
            s.close()

    keyring = {}
    if canary.get("key"):  # a key the test put in the caller's session keyring
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        for where, ring in (("session", -3), ("user", -4), ("user-session", -5)):
            rc = libc.syscall(
                ctypes.c_long(canary["keyctl"]),
                ctypes.c_long(10),  # KEYCTL_SEARCH
                ctypes.c_long(ring),
                b"user",
                canary["key"].encode(),
                ctypes.c_long(0),
            )
            keyring[where] = "found" if rc >= 0 else errno.errorcode.get(ctypes.get_errno(), "?")

    environs = {}
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                environs[pid] = open(f"/proc/{pid}/environ", "rb").read()
            except OSError:
                pass
    seen = {
        "keyring": keyring,
        "planted": planted,
        "pid1_fds": {fd: os.readlink(f"/proc/1/fd/{fd}").split(":")[0] for fd in "012"},
        "home": os.environ.get("HOME", ""),
        "cwd": os.getcwd(),
        "env": sorted(os.environ),
        "helper_env": [
            v.split(b"=", 1)[0].decode()
            for v in environs.get(str(os.getppid()), b"?=").split(b"\\0")
            if v
        ],
        "helper_pwd": environs.get(str(os.getppid()), b"").startswith(b"PWD=/\\0"),
        "environ_canary": [p for p, e in environs.items() if canary["value"].encode() in e],
        "files": {path: os.path.exists(path) for path in canary["paths"]},
        "root": sorted(os.listdir("/")),
        "etc": sorted(os.listdir("/etc")),
        "unix": reach(socket.AF_UNIX, canary["socket"]),
        "abstract": reach(socket.AF_UNIX, "\\0" + canary["abstract"]),
        "loopback": reach(socket.AF_INET, ("127.0.0.1", canary["port"])),
        "net": reach(socket.AF_INET, ("192.0.2.1", 9)),
    }
    print("SEEN " + json.dumps(seen), file=sys.stderr)
    print("9.9.9")
else:
    work = {
        "git_head": os.path.isfile(".git/HEAD"),
        "git_files": sorted(os.listdir(".git")) if os.path.isdir(".git") else [],
        "git_env": sorted(k for k in os.environ if k.startswith("GIT")),
    }
    print("WORK " + json.dumps(work), file=sys.stderr)
"""


def fake_agy(directory: Path, canary: dict[str, Any] | None = None, hang: bool = False) -> str:
    path = directory / "bin" / "agy"
    path.parent.mkdir(parents=True, exist_ok=True)
    plan = canary or {"value": "-", "paths": [], "socket": "/nonexistent", "abstract": "-"}
    plan.setdefault("port", 9)
    plan.setdefault("file", "/nonexistent")
    script = FAKE_AGY.replace("SANDBOX_PYTHON", SANDBOX_PYTHON).replace("HANG", str(hang))
    path.write_text(script.replace("CANARY", repr(plan)))
    path.chmod(0o755)
    return str(path)


def test_the_sandbox_is_a_private_root_with_an_empty_environment(tmp_path, monkeypatch):
    """No host root, no home, nothing writable but the run's own
    directories, and an empty environment for the helper, whatever the
    caller's (R2-F1 of PR #530); a user namespace that is mandatory, and a
    launch that joins a session keyring of its own before bwrap (R4-F1).
    The argv alone is under test: on a host without bwrap or keyctl(2)
    (macOS), both are simulated."""
    monkeypatch.setattr(probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(probe, "keyctl_number", lambda: 250)
    agy = tmp_path / "agy"
    agy.write_text("")
    base, spec, home = tmp_path / "base", tmp_path / "spec", tmp_path / "base" / "home"
    spec.mkdir()
    argv = sandbox_argv(str(agy), base, spec, home, ["inner", str(spec / "spec.json")])
    launch = probe.keyring_launcher()
    assert launch == [sys.executable, "-I", "-S", "-c", probe.KEYRING_LAUNCHER, "250"]
    assert argv[: len(launch)] == launch and argv[len(launch)] == "/usr/bin/bwrap"
    assert "--clearenv" in argv and "--unshare-all" in argv and "--unshare-user" in argv
    mounts = [argv[i : i + 3] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
    sources = {m[1] for m in mounts}
    assert "/" not in sources and str(Path.home()) not in sources
    # the home and workspace alone are writable; the spec directory is not (R3-F1)
    assert [m[1] for m in mounts if m[0] == "--bind"] == [str(base)]
    assert {m[2] for m in mounts if m[0] == "--ro-bind"} >= {"/usr", probe.SANDBOX_AGY, str(spec)}
    etc = sorted(m[2] for m in mounts if m[2].startswith("/etc/"))
    assert etc == sorted(["/etc/group", "/etc/hosts", "/etc/passwd"] + SHOWN_ETC)
    assert argv[argv.index("--") + 1 :][:3] == [SANDBOX_PYTHON, "-I", "-S"]
    passwd = (spec / "etc" / "passwd").read_text()
    assert passwd == f"probe:x:{os.getuid()}:{os.getgid()}:probe:{home}:/bin/sh\n"


@needs_bwrap
def test_a_fake_agy_reaches_no_canary_of_the_host(tmp_path, monkeypatch):
    """The recorder's isolation, measured: a fake agy, asked its version in
    the sandbox, can open no file of the host outside its own directories
    (the checkout, the caller's home, /run, /etc's other files), finds no
    value of the caller's environment in any process's, and can connect to
    no socket of the host's: one by path, an abstract one, a loopback port."""
    value = "agy-canary-" + os.urandom(8).hex()
    monkeypatch.setenv("AUTOFORGE_AGY_CANARY", value)
    host_log = tmp_path / "host.log"
    monkeypatch.setenv("FAKE_AGY_HOST_LOG", str(host_log))
    planted = tmp_path / "canary-file"
    planted.write_text(value)
    unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    name = "autoforge-agy-canary-" + os.urandom(4).hex()
    try:
        bind_unix(unix, tmp_path / "c.sock")
        abstract.bind("\0" + name)
        tcp.bind(("127.0.0.1", 0))
        for s in (unix, abstract, tcp):
            s.listen(1)
        paths = [
            str(planted),
            str(REPO / "README.md"),
            str(Path(__file__).resolve()),
            str(Path.home()),
            "/run",
            "/var",
            "/home",
            "/root",
            "/etc/shadow",
            "/etc/ssh",
            "/etc/hostname",
        ]
        plan = {
            "value": value,
            "paths": paths,
            "socket": str(tmp_path / "c.sock"),
            "abstract": name,
            "port": tcp.getsockname()[1],
        }
        written = record_all(fake_agy(tmp_path, plan), tmp_path / "out", only={"version"})
    finally:
        for s in (unix, abstract, tcp):
            s.close()
    assert written == ["version"]
    record = json.loads((tmp_path / "out" / "version.json").read_text())
    assert record["agy_version"] == "9.9.9" and record["isolation"] == ISOLATION
    seen = next(
        json.loads(line[len("SEEN ") :]) for _, line in record["stderr"] if line.startswith("SEEN ")
    )
    assert re.fullmatch(r"<root>/version\.[^/]+/home", seen["home"]), seen
    assert re.fullmatch(r"<root>/version\.[^/]+/work", seen["cwd"]), seen
    assert seen["files"] == {path: False for path in paths}
    assert seen["environ_canary"] == []
    # the helper's environment: only the PWD bwrap sets at --chdir /
    assert seen["helper_env"] == ["PWD"] and seen["helper_pwd"]
    assert "AUTOFORGE_AGY_CANARY" not in seen["env"]
    assert seen["env"] == sorted(
        ["HOME", "PATH", "LANG", "TERM", "GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL"]
        + [f"XDG_{d}_HOME" for d in ("CONFIG", "CACHE", "DATA", "STATE")]
    )
    assert seen["etc"] == sorted(["group", "hosts", "passwd"] + [Path(e).name for e in SHOWN_ETC])
    top = Path(tempfile.gettempdir()).resolve().parts[1]
    assert set(seen["root"]) <= {
        *probe.ROOT_LINKS,
        "usr",
        "dev",
        "proc",
        "tmp",
        "etc",
        "probe",
        top,
    }
    assert seen["unix"] == "ENOENT"  # no such path: the host's /tmp is not there
    assert seen["abstract"] == "ECONNREFUSED"  # another network namespace's
    assert seen["loopback"] == "ECONNREFUSED"  # the sandbox's own loopback
    assert seen["net"] == "ENETUNREACH"  # no route out
    assert not host_log.exists()
    # nothing of the host's streams: bwrap's own process holds /dev/null and two pipes
    assert seen["pid1_fds"] == {"0": "/dev/null", "1": "pipe", "2": "pipe"}
    # the spec directory is read-only: no link or FIFO for the host to open
    assert seen["planted"] == {"link": "EROFS", "fifo": "EROFS", "spec": "EROFS"}


def test_the_host_reads_the_record_from_the_channel_never_from_a_path(tmp_path, monkeypatch):
    """R3-F1 of PR #530: the host once read ``result.json`` from a directory
    the sandbox could write, so a link there would have been followed in the
    host's namespace, and a FIFO or a huge file would have defeated the
    bound. Now the record is the sandbox's stdout, read with a bound. Here
    the "sandbox" is a plain process that plants a link to a host canary
    and a FIFO as ``result.json`` in the spec directory, writable for once,
    and writes its record on stdout: the host takes the stdout's record, and
    the canary's content goes nowhere."""
    canary = tmp_path / "host-canary"
    secret = "host-canary-" + os.urandom(8).hex()
    canary.write_text(secret)
    plant = (
        "import json, os, sys\n"
        "spec = os.path.dirname(sys.argv[-1])\n"
        "os.symlink(sys.argv[1], spec + '/result.json')\n"
        "os.mkfifo(spec + '/result.fifo')\n"
        "rec = {'argv': [], 'stdout': [], 'stderr': [], 'capture': {}, 'from': 'stdout'}\n"
        "print(json.dumps(rec))\n"
    )

    def sandbox(agy: str, base: Path, spec_dir: Path, home: Path, inner: list[str]) -> list[str]:
        return [sys.executable, "-I", "-c", plant, str(canary), *inner]

    monkeypatch.setattr(probe, "sandbox_argv", sandbox)
    monkeypatch.setattr(probe, "keyctl_number", lambda: 250)  # a Linux host, simulated
    monkeypatch.setattr(probe, "session_keyring", lambda number: 1)
    root = tmp_path / "root"
    root.mkdir()
    record = probe.run_one("/opt/agy", SCENARIO_NAMED["success-stream-json"], root, {})
    assert record["from"] == "stdout"
    assert secret not in json.dumps(record)
    spec_dir = next(root.glob("spec-success-stream-json.*"))
    assert (spec_dir / "result.json").is_symlink()  # planted, and never opened


@pytest.mark.parametrize(
    ("script", "refusal"),
    [
        ("sys.stdout.write('x' * 5000)", "over 4096 bytes"),
        ("sys.stdout.write('[1, 2]')", "not one JSON object"),
        ('sys.stdout.write(\'{"a": 1}{"b": 2}\')', "not one JSON object"),
        ('sys.stdout.write(\'{"a": 1, "a": 2}\')', "not one JSON object"),
        ("sys.stdout.write('{}'); sys.exit(3)", "the sandbox exited 3"),
        ("sys.stdout.write('{\"\u00e9\": 1}')", "not one JSON object"),
        ("import time; time.sleep(30)", "did not end within"),
    ],
    ids=["oversized", "array", "two-objects", "member-twice", "exit", "not-ascii", "hang"],
)
def test_a_record_channel_out_of_bounds_or_shape_is_refused(script, refusal):
    """The bounded read itself: past the bound the sandbox is killed and the
    record refused unread; a stream that is not one JSON object of ASCII, a
    failed sandbox and one that outlives its timeout are refused too."""
    cmd = [sys.executable, "-I", "-c", "import sys\n" + script]
    with pytest.raises(ProbeError, match=refusal):
        run_sandboxed(cmd, timeout=3, bound=4096)
    assert run_sandboxed([sys.executable, "-I", "-c", "print('{}')"], timeout=10) == {}


@needs_bwrap
@pytest.mark.parametrize(
    ("flood", "times", "refusal"),
    [("garbage\n", 1, "not one JSON object"), ("x" * 65536, 64, "over 1048576 bytes")],
    ids=["garbage", "oversized"],
)
def test_a_fake_agy_that_writes_into_the_record_channel_is_refused(
    tmp_path, monkeypatch, flood, times, refusal
):
    """A process of the run can open bwrap's own end of the channel
    (/proc/1/fd/1) and write into it: what arrives is then not the helper's
    record, or is over the bound, and the recording stops with nothing
    written. It cannot make the host read anything else."""
    monkeypatch.setattr(probe, "RESULT_BOUND", 1 << 20)
    agy = fake_agy(
        tmp_path,
        {
            "value": "-",
            "paths": [],
            "socket": "/x",
            "abstract": "-",
            "flood": flood,
            "times": times,
        },
    )
    with pytest.raises(ProbeError, match=refusal):
        record_all(agy, tmp_path / "out", only={"version"})
    assert not (tmp_path / "out").exists()


# add_key(2)'s number, on the architectures the keyring canary runs on.
_SYS_ADD_KEY = {"x86_64": 248, "aarch64": 217}

# The keyring canary's helper. It joins a session keyring of its own first,
# so that the operator's is never touched, adds a harmless key to it and
# finds it there. As the control, it looks for the key from a bwrap that
# joined no keyring, which finds it (ADR 0005 D10.1's measurement). Then it
# records the ``version`` scenario with a fake agy through the probe's own
# command line, in its session keyring, the key's.
_KEYRING_HELPER = """
import ctypes, json, subprocess, sys
probe_py, agy, out, name = sys.argv[1:5]
add_key, keyctl = int(sys.argv[5]), int(sys.argv[6])
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
L = ctypes.c_long
assert libc.syscall(L(keyctl), L(1), None) > 0  # KEYCTL_JOIN_SESSION_KEYRING: its own
key = libc.syscall(L(add_key), b"user", name.encode(), b"harmless", ctypes.c_size_t(8), L(-3))
found = libc.syscall(L(keyctl), L(10), L(-3), b"user", name.encode(), L(0))  # KEYCTL_SEARCH @s
search = (
    "import ctypes, errno\\n"
    "libc = ctypes.CDLL(None, use_errno=True)\\n"
    "libc.syscall.restype = ctypes.c_long\\n"
    f"rc = libc.syscall(ctypes.c_long({keyctl}), ctypes.c_long(10), ctypes.c_long(-3), "
    f"b'user', {name.encode()!r}, ctypes.c_long(0))\\n"
    "print('found' if rc >= 0 else errno.errorcode[ctypes.get_errno()])\\n"
)
bwrap = ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--"]
control = subprocess.run(
    [*bwrap, "/usr/bin/python3", "-I", "-S", "-c", search],
    capture_output=True, text=True, timeout=60,
)
recorded = subprocess.run(
    [sys.executable, "-I", probe_py, "record", "--agy", agy, "--out", out, "--only", "version"],
    capture_output=True, text=True, timeout=120,
)
print(json.dumps({
    "key": key, "found": found, "control": control.stdout.strip(),
    "recorded": recorded.returncode, "stderr": recorded.stderr[-2000:],
}))
"""


def _seen(record: dict[str, Any]) -> dict[str, Any]:
    return next(
        json.loads(line[len("SEEN ") :]) for _, line in record["stderr"] if line.startswith("SEEN ")
    )


@needs_bwrap
def test_a_key_in_the_callers_session_keyring_is_out_of_the_runs_reach(tmp_path):
    """R4-F1 of PR #530: the recorder gave agy a private root and an empty
    environment, but the caller's session keyring came with it, and a key
    there is readable inside a bwrap user namespace (the control below).
    Now the launch joins a session keyring of its own before bwrap runs: a
    harmless key the caller holds is in none of the run's keyrings, and the
    record says the keyring was the run's own."""
    machine = platform.machine().lower()
    if machine not in _SYS_ADD_KEY:
        pytest.skip(f"no add_key(2) number for {machine}")
    name = "autoforge-agy-canary-" + os.urandom(8).hex()
    keyctl = probe.SYS_KEYCTL[machine]
    plan = {"value": "-", "paths": [], "socket": "/x", "abstract": "-", "key": name}
    agy = fake_agy(tmp_path, {**plan, "keyctl": keyctl})
    out = tmp_path / "out"
    argv = [str(Path(probe.__file__)), agy, str(out), name, str(_SYS_ADD_KEY[machine]), str(keyctl)]
    done = subprocess.run(
        [sys.executable, "-I", "-c", _KEYRING_HELPER, *argv],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    helper = json.loads(done.stdout)
    assert helper["key"] > 0 and helper["found"] == helper["key"]  # the canary is there
    assert helper["control"] == "found"  # and in reach of a bwrap that joined no keyring
    assert helper["recorded"] == 0, helper["stderr"]
    record = json.loads((out / "version.json").read_text())
    assert record["session_keyring"] == "own"
    assert _seen(record)["keyring"] == {
        "session": "ENOKEY",
        "user": "ENOKEY",
        "user-session": "ENOKEY",
    }


@needs_bwrap
def test_a_run_that_would_hold_the_callers_session_keyring_is_refused(tmp_path, monkeypatch):
    """The helper's own check, with the launcher left out: the run's session
    keyring is then the caller's (or none, where the caller's was revoked),
    and the helper refuses before it sets anything up or starts agy. The
    recording stops, and writes nothing."""
    monkeypatch.setattr(probe, "keyring_launcher", lambda: [])
    out = tmp_path / "out"
    with pytest.raises(ProbeError, match="session keyring.*refused, nothing started"):
        record_all(fake_agy(tmp_path), out, only={"version"})
    assert not out.exists()


def test_a_host_where_no_session_keyring_can_be_joined_records_nothing(tmp_path, monkeypatch):
    """An architecture whose keyctl(2) number the probe does not know, or a
    host that has no keyctl(2): no run could get a session keyring of its
    own, so the recording is refused before it starts anything."""

    def on_the_host(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"the probe started a process: {args!r}")

    monkeypatch.setattr(probe, "SYS_KEYCTL", {})
    monkeypatch.setattr(probe, "SANDBOX_PYTHON", sys.executable)
    monkeypatch.setattr(probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(probe, "binary_sha256", lambda path: OTHER_SHA)
    monkeypatch.setattr(probe.subprocess, "Popen", on_the_host)
    monkeypatch.setattr(probe.subprocess, "run", on_the_host)
    with pytest.raises(ProbeError, match="keyctl"):
        record_all("/opt/agy", tmp_path / "out")
    assert not (tmp_path / "out").exists()


# getpid(2)'s number: a call that succeeds and joins nothing, so that the
# launcher's join cannot have happened.
_SYS_GETPID = {"x86_64": 39, "aarch64": 172}


@needs_keyring
def test_the_launcher_joins_a_session_keyring_of_its_own():
    """The launcher, on its own: the command it execs holds a session
    keyring that is not this process's (R5-F5: gated on keyring capability)."""
    number = probe.keyctl_number()
    show = f"""
import ctypes, json
libc = ctypes.CDLL(None)
libc.syscall.restype = ctypes.c_long
args = [ctypes.c_long(a) for a in ({number}, 0, -3, 0)]  # KEYCTL_GET_KEYRING_ID @s
print(json.dumps({{"own": libc.syscall(*args)}}))
"""
    launched = run_sandboxed([*probe.keyring_launcher(), sys.executable, "-I", "-c", show], 30)
    assert launched["own"] > 0 and launched["own"] != probe.session_keyring(number)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="getpid(2) refusal stand-in is Linux's",
)
def test_the_launcher_starts_nothing_when_no_keyring_was_joined(tmp_path):
    """When the join cannot have happened (here a call that joins nothing
    stands in for keyctl), the launcher refuses with its own status and the
    command never starts, even on hosts that deny keyctl(2) (R5-F5)."""
    machine = platform.machine().lower()
    if machine not in _SYS_GETPID:
        pytest.skip(f"no getpid(2) number for {machine}")
    marker = tmp_path / "started"
    launch = [sys.executable, "-I", "-S", "-c", probe.KEYRING_LAUNCHER, str(_SYS_GETPID[machine])]
    started = f"open({str(marker)!r}, 'w').close(); print('{{}}')"
    with pytest.raises(ProbeError, match="could not join a session keyring of its own"):
        run_sandboxed([*launch, sys.executable, "-I", "-c", started], 30)
    assert not marker.exists()


def _tree(path: Path) -> dict[str, bytes]:
    return {
        p.relative_to(path).as_posix(): p.read_bytes() if p.is_file() else b"<dir>"
        for p in sorted(path.rglob("*"))
    }


def callers_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The caller's git variables, each pointing somewhere harmless of the test's own."""
    alt = tmp_path / "callers-git-dir"
    alt.mkdir()
    (alt / "untouched").write_text("the caller's\n")
    template = tmp_path / "callers-template"
    (template / "hooks").mkdir(parents=True)
    (template / "hooks" / "from-the-callers-template").write_text("x")
    config = tmp_path / "callers-gitconfig"
    config.write_text(f"[init]\n\ttemplateDir = {template}\n")
    monkeypatch.setenv("GIT_DIR", str(alt))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    return alt


def test_the_host_sets_up_no_workspace(tmp_path, monkeypatch):
    """R3-F2 of PR #530: the customized workspace's ``git init`` once ran on
    the host, with the caller's environment and git configuration, and a
    caller's ``GIT_DIR`` was initialized instead of the workspace. Now the
    host makes two empty directories and starts nothing; the setup is the
    sandbox's (the next test)."""
    alt = callers_git(tmp_path, monkeypatch)
    before = _tree(alt)

    def on_the_host(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"the probe started a process on the host: {args!r}")

    monkeypatch.setattr(probe.subprocess, "run", on_the_host)
    monkeypatch.setattr(probe.subprocess, "Popen", on_the_host)
    root = tmp_path / "root"
    root.mkdir()
    sc = SCENARIO_NAMED["repository-customizations"]
    assert sc.workspace == "customized"
    home, work = probe.prepare(sc, root, {})
    assert list(home.iterdir()) == [] and list(work.iterdir()) == []
    assert _tree(alt) == before


@needs_bwrap
def test_the_workspace_repository_is_made_in_the_sandbox_with_nothing_of_the_callers(
    tmp_path, monkeypatch
):
    """The setup runs inside the sandbox, whose environment is empty, with
    git's own environment set and no template: whatever git variables the
    caller has, its alternate git directory stays as it was, the workspace
    is the repository, and no template of the caller's is copied."""
    alt = callers_git(tmp_path, monkeypatch)
    before = _tree(alt)
    out = tmp_path / "out"
    assert record_all(fake_agy(tmp_path), out, only={"repository-customizations"}) == [
        "repository-customizations"
    ]
    assert _tree(alt) == before
    record = json.loads((out / "repository-customizations.json").read_text())
    work = next(
        json.loads(line[len("WORK ") :]) for _, line in record["stderr"] if line.startswith("WORK ")
    )
    assert work == {
        "git_head": True,
        "git_files": ["HEAD", "config", "objects", "refs"],
        "git_env": [],
    }


@needs_bwrap
def test_a_fake_agy_that_never_prints_its_version_is_killed_at_the_timeout(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ProbeError, match="killed True"):
        discover_version(fake_agy(tmp_path, hang=True), root.resolve(), timeout=2)


# -- opt-in: the scenarios again, against an installed binary -----------------


@pytest.mark.skipif(not PROBE_BIN, reason="AUTOFORGE_AGY_PROBE_BIN names no agy binary")
def test_an_installed_agy_still_shows_what_the_report_states(tmp_path):
    if not _bwrap_works():
        pytest.skip("needs Linux and bwrap with unprivileged user namespaces")
    agy = str(Path(PROBE_BIN).expanduser().resolve())
    written = record_all(agy, tmp_path / "records")
    assert sorted(written) == sorted(s.name for s in SCENARIOS)
    fresh = records(tmp_path / "records")
    assert mismatches(fresh) == []
    assert cross_record_problems(fresh) == []
