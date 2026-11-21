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
cannot be made to produce (a malformed line, a duplicate or misplaced
result, a tool that never ends), and the records are checked to carry no
path of the host, no OAuth query value and no key. The package never
imports the probe.

Opt-in, ``AUTOFORGE_AGY_PROBE_BIN`` names an installed ``agy``: the probe
records every scenario again with that binary (Linux and ``bwrap`` only;
about three minutes, most of it the CLI's own retries of a 429, a 503 and a
cut stream) and the fresh records are held to the same :data:`EXPECTED`.
No model is called: the key is synthetic and the sandbox has no route out::

    AUTOFORGE_AGY_PROBE_BIN=~/.local/bin/agy uv run pytest tests/test_antigravity_probe.py

A failure there on a new build is the point: the report's claims hold for
1.5.13 and must be measured again before they are trusted for another.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import autoforge

from .antigravity_probe import (
    REPLY,
    SCENARIOS,
    SYNTHETIC_KEY,
    Outcome,
    classify,
    outcome_of,
    record_all,
    reduce_stream,
    stripped,
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
    "version": {"exit": 0, "stdout": PINNED},
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
    "ask-question": {**OK, "requests": 2, "stdout": '"step_type":"unknown"'},
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


def cross_record_problems(found: dict[str, dict[str, Any]]) -> list[str]:
    """The facts that compare two records."""
    problems = []
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
    assert sorted(committed) == sorted(s.name for s in SCENARIOS)
    assert sorted(EXPECTED) == sorted(committed)
    assert {r["agy_version"] for r in committed.values()} == {PINNED}


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
    report = REPORT.read_text(encoding="utf-8")
    missing = [s.name for s in SCENARIOS if f"`{s.name}`" not in report]
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


INIT = {"event": "init", "conversation_id": "c", "init": {}}


def agent(index: int, delta: str, state: str = "DONE") -> dict[str, Any]:
    return {
        "event": "step_update",
        "step_update": {
            "step_index": index,
            "state": state,
            "step_type": "agent_response",
            "text_delta": delta,
        },
    }


def tool(index: int, state: str) -> dict[str, Any]:
    return {
        "event": "step_update",
        "step_update": {"step_index": index, "state": state, "step_type": "tool"},
    }


def result(status: str = "SUCCESS", **more: Any) -> dict[str, Any]:
    return {"event": "result", "result": {"status": status, "response": "", **more}}


def test_only_the_last_agent_response_is_the_reply():
    """``result.response`` joins every agent response of the run, a retried
    attempt's cut text and a second turn's reply included (``background-task``,
    ``cut-stream-then-success``); the reply is the last response's deltas."""
    red = reduce_stream(
        stream(INIT, agent(1, "partial "), agent(3, "Final "), agent(3, "reply"), result()), []
    )
    assert classify(red, 0) == Outcome("completed", reply="Final reply")


@pytest.mark.parametrize(
    ("lines", "exit_code", "kind"),
    [
        (["not json\n"], 0, "protocol"),
        (stream({"event": "surprise"}), 0, "protocol"),
        (stream(INIT, agent(1, "a"), result(), result()), 0, "protocol"),
        (stream(INIT, result(), agent(1, "late")), 0, "protocol"),
        (stream(INIT, tool(2, "ACTIVE"), agent(3, "done"), result()), 0, "protocol"),
        (stream(INIT, agent(1, "a")), 0, "no_result"),
        ([], 0, "no_result"),
        (stream(INIT, agent(1, "a"), result()), 1, "error"),
        (stream(INIT, agent(1, "a"), result("ERROR")), 0, "error"),
        (stream(INIT, result(denied_actions=[{"action": "file"}])), 0, "denied"),
        (stream(INIT, result()), 0, "empty_reply"),
        (stream(INIT, agent(1, "  \n"), result()), 0, "empty_reply"),
    ],
)
def test_no_fault_reads_as_a_completed_run(lines, exit_code, kind):
    assert classify(reduce_stream(lines, []), exit_code).kind == kind


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
            if REPLY.splitlines()[0] in outcome.reply:
                assert outcome.reply.count("<<<CONTROL_RESULT>>>") == 1, name


def test_stripped_is_what_the_sweep_measured():
    assert stripped("a​b‮c\x07d\te\r\nf\U000f0000g ") == "abcd\te\r\nfg "


# -- opt-in: the scenarios again, against an installed binary -----------------


def _bwrap_works() -> bool:
    if sys.platform != "linux" or shutil.which("bwrap") is None:
        return False
    probe = ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "--unshare-pid", "--", "true"]
    return subprocess.run(probe, capture_output=True).returncode == 0


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
