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
from autoforge.state import AutoForgeState
from autoforge.transitions import Phase, WorkflowMode
from tests.conftest import block, git_repo, isolated_git, isolated_git_run, make_engine
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
    task state. Every state that is not terminal can be canceled when its
    step ends, by a conclusion or a reconciliation (``step-concluded``) and
    by an ``unblock`` (``unblocked``), as D3.1 and the workflow mirror say.
    The table of how each state is read in the single-agent path has a row
    for every state that path can be in: all but PROPOSED."""
    section = _decision("3")
    assert "one of seven states" in section and len(TASK_STATES) == 7
    tables = _tables(section)
    transitions = tables["From | Event | To"]
    assert transitions
    edges = []
    step_ended: set[str] = set()
    for cells in transitions:
        assert len(cells) == 3, cells
        (source,), (target,) = _code_spans(cells[0]), _code_spans(cells[2])
        edges.append((source, target))
        reasons = set(_code_spans(cells[1]))
        if target == "CANCELED" and {"step-concluded", "unblocked"} <= reasons:
            step_ended.add(source)
    sources, targets = {s for s, _ in edges}, {t for _, t in edges}
    assert sources | targets <= TASK_STATES
    assert targets == TASK_STATES - INITIAL
    assert sources == TASK_STATES - TERMINAL
    assert not (sources | targets) & NOT_TASK_STATES
    assert step_ended == TASK_STATES - TERMINAL
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


def _step_key(state: AutoForgeState) -> StepKey:
    return StepKey(
        phase=state.phase,
        issue_url=state.current_issue_url,
        pr_url=state.current_pr_url,
        review_round=state.review_round,
        execution_attempt=state.execution_attempt,
    )


def test_adr_0015_attempt_repeats_after_an_unblock_into_the_same_step(tmp_state_dir, fake_github):
    """D2.1's regression scenario, as today's code runs it. An ``unblock``
    back into the same phase keeps the step's key and restarts the attempt
    counter, so two launches of the step share ``(StepKey, attempt)``: the
    counter names no launch. The persisted launch ordinal distinguishes them."""
    stop = block({"phase": "ANALYZE_EXECUTE", "status": "blocked", "message": "need decision"})
    eng = make_engine(tmp_state_dir, [stop, stop], github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    key = _step_key(eng.state)

    assert eng.step().next_phase == "BLOCKED"
    first = (eng.state.attempt, eng.state.step_count, eng.state.launch_seq)
    assert first[2] == 1
    eng.load()
    assert eng.state.launch_seq == first[2]
    assert eng.unblock("the operator decided").unblocked
    assert eng.state.phase is Phase.ANALYZE_EXECUTE and _step_key(eng.state) == key
    assert eng.state.attempt == 0 and eng.state.step_count == first[1]

    assert eng.step().next_phase == "BLOCKED"
    second = (eng.state.attempt, eng.state.step_count, eng.state.launch_seq)
    assert [call.attempt for call in eng.provider.calls] == [1, 1]
    assert first[0] == second[0] == 1 and second[1] > first[1]
    assert second[2] == 2

    identity = next(
        cells
        for cells in _tables(_decision("2"))["Identity | What it is | Persisted"]
        if _code_spans(cells[0]) == ["AttemptID"]
    )
    assert "`(RunID, launch_seq)`" in identity[1] and "state.attempt" not in identity[1]
    assert "launch_seq" in {field.name for field in dataclasses.fields(AutoForgeState)}


@pytest.mark.parametrize(
    ("status", "phase", "code"),
    [("blocked", "BLOCKED", "agent-blocked"), ("failure", "FAILED", "agent-failure")],
)
def test_adr_0015_a_stop_result_is_applied_in_one_save_with_no_plan(
    tmp_state_dir, fake_github, monkeypatch, status, phase, code
):
    """D4.6, as today's code runs it. An ordinary ``blocked`` or ``failure``
    result is applied in the first save after its launch: the stop, with the
    agent's code and message. No save of the step holds a completion context
    or an effect record, and nothing reaches GitHub. That stop save is where
    D4.6 accepts the producing task and admits its Memory proposals, so the
    child that changes it updates the record with this test."""
    payload = {"phase": "ANALYZE_EXECUTE", "status": status, "message": "need decision"}
    eng = make_engine(tmp_state_dir, [block(payload)], github=fake_github, origin=True)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    saves: list[tuple] = []
    save = eng._save

    def recording() -> None:
        state = eng.state
        saves.append(
            (
                len(eng.provider.calls),
                state.phase.value,
                state.stop_code,
                state.block_reason,
                dict(state.completion_context),
                list(state.effect_records),
            )
        )
        save()

    monkeypatch.setattr(eng, "_save", recording)
    assert eng.step().next_phase == phase

    after = [s for s in saves if s[0] == 1]
    assert after and after[0][1:4] == (phase, code, "need decision")
    assert all(s[1:4] == after[0][1:4] for s in after)
    assert all(s[4] == {} and s[5] == [] for s in saves)
    assert fake_github.effect_writes == []
    assert "**D4.6. A result that stops the step is accepted in its stop save" in _text()


def test_adr_0015_a_candidates_ref_is_created_and_never_moved(tmp_path):
    """D7.7 names one ref per ``verified`` candidate, created with a
    create-only write before the save that keeps it, so that keeping C2
    never moves the ref of C1, the candidate the state kept before (W12).
    This holds the git behaviour it rests on: the name D7.7 gives is a
    valid ref outside ``refs/heads/`` for a real ``RunID``; two candidates'
    refs coexist under the run's prefix; and a create-only write of a name
    that exists, at another commit or the same one, is refused and leaves
    the ref where it was. Which candidate is kept is the state's record,
    which no ref decides, and #319 owes that scenario with fakes (§4.2)."""
    pattern = "refs/autoforge/<run>/verified/<commit>"
    assert f"`{pattern}`" in _decision("7")
    run, repo = generate_run_id(), str(git_repo(tmp_path / "repo"))
    tree = isolated_git("-C", repo, "mktree", input=b"").decode().strip()
    identity = ("-c", "user.name=AutoForge tests", "-c", "user.email=tests@example.invalid")

    def commit(message: str) -> str:
        return (
            isolated_git("-C", repo, *identity, "commit-tree", tree, "-m", message).decode().strip()
        )

    def ref(sha: str) -> str:
        return pattern.replace("<run>", run).replace("<commit>", sha)

    def resolves(name: str) -> str:
        return isolated_git("-C", repo, "rev-parse", "--verify", name).decode().strip()

    def create_only(name: str, sha: str) -> int:
        return isolated_git_run("-C", repo, "update-ref", name, sha, "").returncode

    c1, c2 = commit("c1"), commit("c2")
    kept, new = ref(c1), ref(c2)
    for name in (kept, new):
        assert isolated_git_run("check-ref-format", name).returncode == 0, name
        assert not name.startswith("refs/heads/")
    assert create_only(kept, c1) == 0
    # The crash window W12: C2's ref is created, and the save that would
    # record C2 never happens. C1's ref still resolves to C1.
    assert create_only(new, c2) == 0
    assert (resolves(kept), resolves(new)) == (c1, c2)
    listed = isolated_git(
        "-C", repo, "for-each-ref", "--format=%(refname)", f"refs/autoforge/{run}/"
    )
    assert sorted(listed.decode().split()) == sorted([kept, new])
    # A taken name is never moved: not to another commit, and a reuse reads
    # the ref rather than writing it.
    assert create_only(kept, c2) != 0
    assert create_only(new, c2) != 0
    assert (resolves(kept), resolves(new)) == (c1, c2)


def test_sqlite_is_only_for_the_read_only_probe_and_disposable_index():
    """D10 prohibits pathname-opened SQLite for Memory or its index."""
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
                    source = ast.get_source_segment(path.read_text(), function)
                    if path.name == "memory_fts5.py":
                        assert tuple(map(ast.literal_eval, node.args)) == (":memory:",)
                        assert {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords} == {
                            "check_same_thread": False,
                        }
                        assert "PRAGMA temp_store = MEMORY" in source
                        assert "setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)" in source
                    else:
                        assert "mode=ro" in source
        if "sqlite3" in path.read_text(encoding="utf-8"):
            assert path.name in ("providers.py", "memory_fts5.py"), path
    assert sorted(callers) == [
        ("memory_fts5.py", "__init__"),
        ("providers.py", "_opencode_database_problems"),
    ]
