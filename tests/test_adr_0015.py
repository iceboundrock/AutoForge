"""ADR 0015's record, held to the code it describes (#316).

ADR 0015 decides how task state below a phase, Issue Working Memory and
their crash order fit the controller, and implements nothing. What it says
of the code at the commit it was written at goes stale silently: an adapter
that drops the flag its fresh-context proof rests on, a provider added
without a row in its table, a new owner of a SQLite connection. These tests
read the record and the code it names, and fail when the two part, so that
the change that parts them also updates the record. Whether a decision is
right is review's business; that the record is internally closed and true
of the code is theirs.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

import pytest

import autoforge
from autoforge.auto_recovery import StepKey
from autoforge.config import ProfileConfig, default_config
from autoforge.engine import generate_run_id
from autoforge.errors import ConfigurationError
from autoforge.providers import (
    _REGISTRY,
    ClaudeCodeProvider,
    ClaudeSdkProvider,
    OpenCodeProvider,
    PiProvider,
)
from autoforge.transitions import WorkflowMode
from tests.test_container_backend_record import FENCE, LINK, PROTOCOL, _broken

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path(autoforge.__file__).parent
ADR_0015 = ROOT / "docs" / "adr" / "0015-task-state-issue-memory-and-crash-order.md"

# D3: the task states, and the two of them no transition leaves.
TASK_STATES = {
    "PROPOSED",
    "ADMITTED",
    "RUNNING",
    "VERIFYING",
    "ACCEPTED",
    "RETRYABLE",
    "CANCELED",
}
TERMINAL = {"ACCEPTED", "CANCELED"}
INITIAL = {"PROPOSED"}
# Workflow states D3 decides are not task states.
NOT_TASK_STATES = {"BLOCKED", "FAILED"}


def _text() -> str:
    return ADR_0015.read_text(encoding="utf-8")


def _decision(number: str) -> str:
    """The text of ``### D<number>.``, up to the next decision or section."""
    text = _text()
    start = text.index(f"\n### D{number}. ")
    ends = [i for i in (text.find("\n### ", start + 1), text.find("\n## ", start + 1)) if i > 0]
    return text[start : min(ends)]


def _tables(section: str) -> dict[str, list[list[str]]]:
    """Each Markdown table of ``section``, keyed by its header row, as its
    body rows' stripped cells."""
    tables: dict[str, list[list[str]]] = {}
    block: list[str] = []
    for line in [*section.splitlines(), ""]:
        if line.startswith("|"):
            block.append(line)
            continue
        if len(block) > 2:
            rows = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in block]
            tables[" | ".join(rows[0])] = rows[2:]
        block = []
    return tables


def _code_spans(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


# -- the record ------------------------------------------------------------------------


def test_every_test_adr_0015_names_exists():
    named = set(re.findall(r"`(test_[a-z0-9_]+)`", _text()))
    assert named
    defined = {
        node.name
        for path in (ROOT / "tests").glob("test_*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
    }
    assert sorted(named - defined) == []


def test_adr_0015_claims_no_state_protocol_number_and_its_links_resolve():
    """D19 lists what #23 numbers and assigns nothing: no protocol past the
    current one is named. Every relative link outside a fence resolves."""
    text = _text()
    current = int(autoforge.__protocol_version__)
    assert [n for n in PROTOCOL.findall(text) if int(n) > current] == []
    broken, fenced, links = [], False, 0
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        for target in [] if fenced else LINK.findall(line):
            links += 1
            if "://" not in target:
                broken += [p for p in [_broken(ADR_0015, target)] if p]
    assert links and broken == []


def test_adr_0015_task_table_is_closed_over_its_states():
    """D3's transition table uses only the seven task states, every state
    but the initial one is entered and every state but the terminal ones is
    left, nothing leaves a terminal state, and the workflow's stops are no
    task state. The table of how each state is read in the single-agent path
    has a row for every state that path can be in: all but PROPOSED."""
    section = _decision("3")
    assert "one of seven states" in section and len(TASK_STATES) == 7
    tables = _tables(section)
    transitions = tables["From | Event | To"]
    assert transitions
    edges = []
    for cells in transitions:
        assert len(cells) == 3, cells
        (source,), (target,) = _code_spans(cells[0]), _code_spans(cells[2])
        edges.append((source, target))
    sources, targets = {s for s, _ in edges}, {t for _, t in edges}
    assert sources | targets <= TASK_STATES
    assert targets == TASK_STATES - INITIAL
    assert sources == TASK_STATES - TERMINAL
    assert not (sources | targets) & NOT_TASK_STATES
    derived = tables["State | How it is read in the single-agent path"]
    assert {_code_spans(cells[0])[0] for cells in derived} == TASK_STATES - INITIAL
    mirrored = tables["Workflow | Task | How the two relate"]
    stops = {span for cells in mirrored for span in _code_spans(cells[0])}
    assert NOT_TASK_STATES <= stops
    assert all(cells[1] == "none" for cells in mirrored if set(_code_spans(cells[0])) & stops)


def _fresh_context_rows() -> dict[str, list[str]]:
    rows = _tables(_decision("14"))[
        "Provider | The proof today | What voids it | Status at `9baed0a`"
    ]
    return {_code_spans(cells[0])[0]: cells for cells in rows}


def test_adr_0015_fresh_context_table_takes_every_provider():
    """D14 says, for every provider, how a launch proves a fresh context. A
    provider registered later has no row until someone decides it."""
    assert set(_fresh_context_rows()) == set(_REGISTRY)


def test_adr_0015_fresh_context_proofs_hold_in_the_adapters():
    """The argv facts D14's proofs rest on, as the adapters build them."""
    claude = default_config().profile("analyze_execute")
    assert claude.provider == "claude"
    argv = ClaudeCodeProvider().build_command_for(claude, "x")
    assert argv[1] == "-p"
    assert "--no-session-persistence" in argv and "--safe-mode" in argv
    persisted = dataclasses.replace(claude, options={"session_persistence": "true"})
    assert "--no-session-persistence" not in ClaudeCodeProvider().build_command_for(persisted, "x")

    opencode = ProfileConfig(name="o", provider="opencode", model="openai/gpt-5.6-luna")
    assert OpenCodeProvider().build_command_for(opencode, "x")[1:3] == ["run", "--standalone"]

    pi = ProfileConfig(name="p", provider="pi", model="openai/gpt-5.6-terra", effort="high")
    assert "--no-session" in PiProvider().build_command_for(pi, "x")
    with pytest.raises(ConfigurationError, match="pi takes no extra_args"):
        PiProvider().validate_profile(dataclasses.replace(pi, extra_args=["--session-id", "s"]))

    sdk = ProfileConfig(
        name="s",
        provider="claude-sdk",
        model="fable",
        effort="high",
        command="/opt/claude-sdk/bin/python",
        extra_args=["--resume"],
    )
    with pytest.raises(ConfigurationError, match="takes no extra_args"):
        ClaudeSdkProvider().validate_profile(sdk)


@pytest.mark.parametrize("provider", ["claude", "opencode"])
def test_adr_0015_resume_flags_pass_validation_today(provider):
    """D14's status column says that each flag it lists as voiding a
    pass-through provider's proof is accepted by that provider's profile
    validation at the commit the record was written at. A change that
    refuses one updates that column with this test."""
    flags = [f for f in _code_spans(_fresh_context_rows()[provider][2]) if f.startswith("-")]
    assert flags
    if provider == "claude":
        adapter, base = ClaudeCodeProvider(), default_config().profile("analyze_execute")
    else:
        adapter = OpenCodeProvider()
        base = ProfileConfig(name="o", provider="opencode", model="openai/gpt-5.6-luna")
    for flag in flags:
        adapter.validate_profile(dataclasses.replace(base, extra_args=[flag]))


# -- the code the record states facts of -----------------------------------------------


def test_adr_0015_identities_are_the_codes():
    """D2: a RunID is the state's run id as the engine makes it; a step is
    ADR 0012's StepKey; REMOTE is the one workflow mode."""
    assert re.fullmatch(r"af-\d{8}T\d{6}Z-[0-9a-f]{6}", generate_run_id())
    assert [f.name for f in dataclasses.fields(StepKey)] == [
        "phase",
        "issue_url",
        "pr_url",
        "review_round",
        "execution_attempt",
    ]
    assert [mode.value for mode in WorkflowMode] == ["REMOTE"]


def test_the_package_opens_sqlite_only_for_the_opencode_probe():
    """D10 refuses SQLite as authoritative storage and says the package
    opens it in one place, read-only: the probe of OpenCode's organization
    integrations. A second connection, or a writable one, is a decision
    D10 has to take first."""
    callers = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "connect"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "sqlite3"
                ):
                    callers.append((path.relative_to(PACKAGE).as_posix(), function.name))
                    assert "mode=ro" in ast.get_source_segment(path.read_text(), function)
        if "sqlite3" in path.read_text(encoding="utf-8"):
            assert path.name == "providers.py", path
    assert sorted(set(callers)) == [("providers.py", "_opencode_database_problems")]
