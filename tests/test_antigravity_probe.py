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
cannot be made to produce (a malformed line or value, a duplicate or
misplaced result, a missing init, a response or tool that never ends, an
ambiguous step), the complete measurement of what a prompt loses
(``fidelity-all-*``) is held to the committed table ``dropped.txt``, and the
records are checked to carry no path of the host, no OAuth query value and
no key. The package never imports the probe.

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
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

import autoforge

from . import antigravity_probe as probe
from .antigravity_probe import (
    ALL_PREFIX,
    DROPPED_TABLE,
    REPLY,
    SCALAR_VALUES,
    SCENARIOS,
    SYNTHETIC_KEY,
    VERSION_SCENARIO,
    Outcome,
    ProbeError,
    classify,
    discover_version,
    dropped_of,
    is_dropped,
    measure_all,
    outcome_of,
    read_table,
    record_all,
    reduce_stream,
    stripped,
    version_printed,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "antigravity" / "1.5.13"
REPORT = REPO / "docs" / "antigravity-cli-spike.md"
PINNED = "1.5.13"
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
        "left": len(record["processes_left"]),
        "markers": sorted(record["markers"]),
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
# The repository's rules are loaded, and its hooks and MCP server run, whatever the settings.
CUSTOMIZED = {
    "exit": 0,
    "status": "SUCCESS",
    "markers": ["hook-pre-invocation", "hook-session-start", "hook-stop", "mcp-started"],
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
# The complete measurement: each prompt arrived whole, minus what agy
# removes (complete_sweep_problems holds what it removed to dropped.txt).
for _sc in SCENARIOS:
    if _sc.name.startswith(ALL_PREFIX):
        EXPECTED[_sc.name] = {
            **OK,
            "requests": 1,
            "truncated": False,
            "closing_tag_kept": True,
            "identical_after_strip": True,
        }


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


def scalar_values_in(first: int, last: int) -> int:
    """How many scalar values (code points but surrogates) ``first..last`` holds."""
    surrogates = max(0, min(last, 0xDFFF) - max(first, 0xD800) + 1)
    return last - first + 1 - surrogates


def complete_sweep_problems(found: dict[str, dict[str, Any]]) -> list[str]:
    """Every scalar value measured once and cleanly, and what was dropped 1.5.13's table.

    A fresh recording of another build is held to what 1.5.13 removed: a
    difference is a finding, and that build's own table is written only
    once it is understood (the report's C3).
    """
    problems = []
    spans = []
    for name, record in sorted(found.items()):
        if not name.startswith(ALL_PREFIX):
            continue
        measured = record["facts"].get("all")
        if not measured or measured["dropped"] is None:
            clean = {k: (measured or {}).get(k) for k in ("framed", "ordered", "foreign")}
            problems.append(f"{name}: not measured cleanly {clean}")
            continue
        spans.append((measured["first"], measured["last"], measured["sent"], name))
    following = 0
    for first, last, sent, name in sorted(spans):
        if following == 0xD800:
            following = 0xE000
        if first != following:
            problems.append(f"{name}: starts at U+{first:04X}, not U+{following:04X}")
        if sent != scalar_values_in(first, last):
            problems.append(f"{name}: sent {sent} code points of U+{first:04X}..U+{last:04X}")
        following = last + 1
    if following != 0x110000 or sum(s[2] for s in spans) != SCALAR_VALUES:
        problems.append("the complete measurement does not cover every scalar value once")
    want = [list(r) for r in read_table()]
    if dropped_of(found.values()) != want:
        problems.append("what the prompts lost is not the table of dropped.txt")
    return problems


def cross_record_problems(found: dict[str, dict[str, Any]]) -> list[str]:
    """The facts that compare two records."""
    problems = []
    labels = {r["agy_version"] for r in found.values()}
    if len(labels) != 1:
        problems.append(f"the records name more than one build: {sorted(labels)}")
    elif version_printed(found["version"]) != next(iter(labels)):
        problems.append("the version record did not print the version the records name")
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
    assert version_printed(committed["version"]) == PINNED
    assert [line for _, line in committed["version"]["stdout"]] == [PINNED + "\n"]


def test_the_committed_records_show_what_the_report_states(committed):
    assert mismatches(committed) == []
    assert cross_record_problems(committed) == []


def test_the_committed_records_carry_no_host_path_query_value_or_key(committed):
    for name, record in committed.items():
        dump = json.dumps(record, ensure_ascii=False)
        # every path the run saw is under the probe's root, spelt <root>
        assert not HOST_PATH.search(dump), (name, HOST_PATH.search(dump))
        assert SYNTHETIC_KEY not in dump, name
        for value in re.findall(r"(?:client_id|code_challenge|state)=([^&\s\"\\]+)", dump):
            assert value == "<redacted>", name


def test_the_report_cites_every_scenario():
    """The numbered prompts of the complete measurement are cited as a run,
    ``fidelity-all-00`` to the last."""
    report = REPORT.read_text(encoding="utf-8")
    numbered = sorted(s.name for s in SCENARIOS if re.fullmatch(ALL_PREFIX + r"\d+", s.name))
    family = f"`{numbered[0]}` to `{numbered[-1]}`"
    missing = [
        s.name
        for s in SCENARIOS
        if f"`{s.name}`" not in report and not (s.name in numbered and family in report)
    ]
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
    body = {"conversation_id": CID, "status": status, "response": "", **more}
    return {"event": "result", "result": body}


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
    assert classify(reduce_stream(lines, []), 0) == Outcome("completed", reply="Final reply")


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
        pytest.param(
            stream({"event": "result", "result": {"conversation_id": "", "status": "ERROR"}}),
            1,
            "error",
            id="refusal",
        ),
        pytest.param(
            stream({"event": "result", "result": {"conversation_id": "x", "status": "ERROR"}}),
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
    assert classify(reduce_stream(lines, []), exit_code).kind == kind


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
    *((3, ("result", "response"), v) for v in (None, 0, [])),
    *((3, ("result", "error"), v) for v in (None, 0, {})),
    *((3, ("result", "finished_reason"), v) for v in (None, 0, [])),
    *(
        (3, ("result", "denied_actions"), v)
        for v in ("command", 0, {"action": "x"}, [None], [{}], [{"action": 1}])
    ),
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
    assert classify(reduce_stream(stream(*GOOD), []), 0) == Outcome("completed", reply="reply")


@pytest.mark.parametrize(
    ("index", "path", "value"),
    MALFORMED,
    ids=[f"{'.'.join(p)}={'gone' if v is GONE else repr(v)}" for _, p, v in MALFORMED],
)
def test_a_malformed_value_is_a_protocol_failure_never_an_exception(index, path, value):
    lines = stream(*mutated(GOOD, index, path, value))
    outcome = classify(reduce_stream(lines, []), 0)
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
                    assert classify(reduce_stream(junk, []), 0).kind in KINDS, (name, path)


def test_a_failed_turn_is_typed_from_agy_error_not_from_its_retryable_flag():
    """``retryable`` is not what the CLI did: it retried a cut stream nine
    times and then called it not retryable (``cut-stream-always``)."""
    lines = stream(INIT, result("ERROR", finished_reason="error"))
    for code, kind in (
        (429, "usage_limit"),
        (503, "transient"),
        (408, "transient"),
        (403, "error"),
    ):
        err = {"error_code": code, "code_kind": "http", "retryable": code != 403}
        red = reduce_stream(lines, ["AGY_ERROR: " + json.dumps(err)])
        assert classify(red, 3).kind == kind
    grpc = {"error_code": 2, "code_kind": "grpc", "status": "UNKNOWN", "retryable": False}
    assert classify(reduce_stream(lines, ["AGY_ERROR: " + json.dumps(grpc)]), 3).kind == "error"
    # an AGY_ERROR line of another shape types nothing, and raises nothing
    odd_lines = ("[429]", '"429"', '{"error_code": "429", "code_kind": "http"}', "{", "null")
    for odd in (*odd_lines, "[" * 100_000):
        assert classify(reduce_stream(lines, ["AGY_ERROR: " + odd]), 3).kind == "error", odd
    true_code = {"error_code": True, "code_kind": "http"}
    assert (
        classify(reduce_stream(lines, ["AGY_ERROR: " + json.dumps(true_code)]), 3).kind == "error"
    )


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


def test_the_complete_measurement_covers_every_scalar_value_and_is_the_table(committed):
    assert complete_sweep_problems(committed) == []
    measured = [r for name, r in committed.items() if name.startswith(ALL_PREFIX)]
    assert sum(r["facts"]["all"]["sent"] for r in measured) == SCALAR_VALUES == 1_112_064
    assert len(measured) == 31
    assert DROPPED_TABLE.parent == FIXTURES
    # the counts the report gives (its section 3)
    table = read_table()
    removed = sum(last - first + 1 for first, last in table)
    assert (len(table), removed, SCALAR_VALUES - removed) == (744, 952_427, 159_637)


def test_a_measurement_that_lost_a_prompt_or_a_value_is_caught(committed):
    gaps = copy.deepcopy(committed)
    del gaps[ALL_PREFIX + "03"]
    assert any("starts at" in p for p in complete_sweep_problems(gaps))
    unclean = copy.deepcopy(committed)
    unclean[ALL_PREFIX + "nul"]["facts"]["all"]["dropped"] = None
    assert any("not measured cleanly" in p for p in complete_sweep_problems(unclean))
    kept = copy.deepcopy(committed)
    kept[ALL_PREFIX + "00"]["facts"]["all"]["dropped"].pop(0)
    assert complete_sweep_problems(kept) == [
        "what the prompts lost is not the table of dropped.txt"
    ]


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
    assert json.loads((out / "success-stream-json.json").read_text())["agy_version"] == "9.9.9"
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


def _bwrap_works() -> bool:
    if sys.platform != "linux" or shutil.which("bwrap") is None:
        return False
    argv = ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "--unshare-pid", "--", "true"]
    return subprocess.run(argv, capture_output=True).returncode == 0


needs_bwrap = pytest.mark.skipif(
    not _bwrap_works(), reason="needs Linux and bwrap with unprivileged user namespaces"
)

FAKE_AGY = """#!PYTHON -I
import errno, os, socket, sys, time

log = os.environ.get("FAKE_AGY_HOST_LOG")
if log:  # only a launch in the caller's environment sees it
    with open(log, "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    if HANG:
        time.sleep(600)
    try:
        socket.create_connection(("192.0.2.1", 9), timeout=2).close()
        net = "connected"
    except OSError as e:
        net = errno.errorcode.get(e.errno, "timeout") if e.errno else "timeout"
    print("HOME=" + os.environ.get("HOME", ""), file=sys.stderr)
    print("CWD=" + os.getcwd(), file=sys.stderr)
    print("CANARY=" + ("present" if "FAKE_AGY_CANARY" in os.environ else "absent"), file=sys.stderr)
    print("NET=" + net, file=sys.stderr)
    print("9.9.9")
"""


def fake_agy(directory: Path, hang: bool = False) -> str:
    path = directory / "bin" / "agy"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_AGY.replace("PYTHON", sys.executable).replace("HANG", str(hang)))
    path.chmod(0o755)
    return str(path)


@needs_bwrap
def test_a_fake_agy_is_asked_its_version_inside_the_sandbox(tmp_path, monkeypatch):
    host_log = tmp_path / "host.log"
    monkeypatch.setenv("FAKE_AGY_HOST_LOG", str(host_log))
    monkeypatch.setenv("FAKE_AGY_CANARY", "the caller's")
    assert record_all(fake_agy(tmp_path), tmp_path / "out", only={"version"}) == ["version"]
    record = json.loads((tmp_path / "out" / "version.json").read_text())
    assert record["agy_version"] == "9.9.9"
    seen = dict(line.rstrip("\n").split("=", 1) for _, line in record["stderr"])
    assert re.fullmatch(r"<root>/version\.[^/]+/home", seen["HOME"]), seen
    assert re.fullmatch(r"<root>/version\.[^/]+/work", seen["CWD"]), seen
    assert seen["CANARY"] == "absent"
    assert seen["NET"] == "ENETUNREACH"  # no route out of the sandbox's network
    assert not host_log.exists()


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
