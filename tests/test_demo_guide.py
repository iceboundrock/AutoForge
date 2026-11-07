"""The demonstration procedure's trace commands and links (#261).

``docs/demo.md`` tells the maintainer how to cut the excerpts a public
demonstration publishes from a run's ``status --json`` and its
``events.jsonl``, and its claims table says which of their fields show
each claim. Those ``jq`` programs name fields of the state file and of the
run log; a field renamed in the code would leave the published procedure
extracting nulls, and a write-up built on them claiming nothing it could
check. These tests drive a scripted run with one fix iteration through the
unchanged engine, then check that every field the programs read is one that
run wrote, and, where ``bash`` and ``jq`` are installed, run the guide's own
"Collect the trace" commands on it and check the facts the claims table
reads from their output. They run them with the run's state in the default
directory and in another one, and once more around a replacement of the
run, as ``run --force`` makes it. Every link of the page, and every link
into it, must reach a file and a heading that exist.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from autoforge import cli
from autoforge.claude_stream import ClaudeStream
from autoforge.config import DEFAULT_STATE_DIR
from autoforge.engine import ControllerEngine
from autoforge.providers import ProviderRegistry, ScriptedProvider
from autoforge.state import load_state
from tests.conftest import (
    EPIC,
    ISSUE,
    PR,
    FakeGitHub,
    block,
    fix_commit,
    git_repo,
    implement,
    make_engine,
    review_result,
)
from tests.test_troubleshooting_guide import _broken, _links, _markdown_files

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "demo.md"
FENCE = re.compile(r"^\s*```")
HEADING = re.compile(r"^(#+)\s+(.*?)\s*$")
# One jq command: its flags, its program (single-quoted, possibly over several
# lines, or one bare word) and the file it reads.
JQ = re.compile(r"\bjq((?:\s+-[a-z]+)*)\s+(?:'([^']*)'|([^\s']+))\s+(\S+)")
STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
DOTTED = re.compile(r"\.([A-Za-z_]\w*)")
NAME = re.compile(r"[A-Za-z_]\w*")
AGENT_PHASES = ("ANALYZE_EXECUTE", "REVIEW", "FIX")
COST_USD = 0.0125  # what the scripted Claude Code launches report
COLLECT = "Collect the trace"
# A state directory other than the default, below the checkout as the
# controller requires (and relative, as a configuration's `state_dir` is).
ELSEWHERE = "var/autoforge-demo"
# `autoforge` in the guide's commands: records its arguments, one per line,
# and prints the `status --json` document the test produced in-process.
SHIM = 'autoforge() { printf "%s\\n" "$@" > "$AUTOFORGE_ARGV"; cat "$AUTOFORGE_STATUS"; }'

needs_jq = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="jq or bash is not installed",
)


def _code_blocks(path: Path, section: str | None = None) -> list[str]:
    """The fenced code blocks of a Markdown file, line continuations joined:
    all of them, or those of the ``section`` heading's own text, up to the
    next heading of any level."""
    blocks: list[str] = []
    current: list[str] | None = None
    inside = section is None
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            if current is None:
                current = []
            else:
                if inside:
                    blocks.append("\n".join(current).replace("\\\n", " "))
                current = None
        elif current is not None:
            current.append(line)
        elif section is not None and (heading := HEADING.match(line)):
            inside = heading.group(2) == section
    assert current is None, "an unclosed fence"
    return blocks


def _jq_commands() -> list[tuple[list[str], str, str]]:
    """Each ``jq`` command of the guide: its flags, its program, and ``status``
    or ``events`` for the file it reads."""
    commands = []
    for text in _code_blocks(GUIDE):
        for flags, quoted, bare, target in JQ.findall(text):
            target = target.strip("\"')")
            if target.endswith("/status.json"):
                source = "status"
            elif target.endswith("/events.jsonl"):
                source = "events"
            else:
                raise AssertionError(f"a jq command reads {target!r}, which no test covers")
            commands.append((flags.split(), quoted or bare, source))
    return commands


def _top_level_entries(body: str) -> list[str]:
    """The comma-separated entries of an object construction's body."""
    entries, depth, start = [], 0, 0
    for i, char in enumerate(body):
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == "," and depth == 0:
            entries.append(body[start:i].strip())
            start = i + 1
    entries.append(body[start:].strip())
    return entries


def _fields(program: str) -> set[str]:
    """Every field a jq program reads: each ``.name`` and each shorthand
    ``{name}`` entry of an object construction (``{a}`` is ``{a: .a}``);
    string literals hold none."""
    text = STRING.sub('""', program)
    fields = set(DOTTED.findall(text))
    for i, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        for j in range(i, len(text)):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                break
        fields |= {e for e in _top_level_entries(text[i + 1 : j]) if NAME.fullmatch(e)}
    return fields


def _keys(value: object) -> set[str]:
    """Every object key anywhere in a JSON value."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def _assignment(block: str, name: str) -> str | None:
    """The value a block's ``name=...`` line gives, or None without one."""
    found = re.findall(rf"(?m)^{name}=(\S*)$", block)
    assert len(found) <= 1, f"{name} is set twice in one block"
    return found[0] if found else None


def _collect(checkout: Path, state: str, out: str, status: Path) -> list[str]:
    """Run the guide's "Collect the trace" commands from ``checkout`` with its
    ``state`` and ``out`` set as given and ``autoforge`` serving ``status``;
    the arguments the commands passed to ``autoforge``."""
    blocks = _code_blocks(GUIDE, COLLECT)
    script = [SHIM]
    for text in blocks:
        if _assignment(text, "state") is not None:
            text = re.sub(r"(?m)^state=\S*$", f"state={shlex.quote(state)}", text)
            text = re.sub(r"(?m)^out=\S*$", f"out={shlex.quote(out)}", text)
        script.append(text)
    argv = checkout.parent / f"{checkout.name}-autoforge-argv"
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", "\n".join(script)],
        cwd=checkout,
        env={
            "PATH": os.environ["PATH"],
            "LC_ALL": "C",
            "AUTOFORGE_ARGV": str(argv),
            "AUTOFORGE_STATUS": str(status),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return argv.read_text(encoding="utf-8").splitlines()


def _claude_summary() -> dict[str, str | int | bool]:
    """The summary Claude Code's stream gives a launch that cost ``COST_USD``."""
    stream = ClaudeStream()
    record = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "duration_ms": 1000,
        "total_cost_usd": COST_USD,
        "result": "done",
    }
    stream.feed(json.dumps(record).encode())
    stream.stream_ended()
    return stream.summary()


@pytest.fixture
def demo_run(request, tmp_path, monkeypatch, capsys):
    """A run that went ANALYZE_EXECUTE, REVIEW (one finding), FIX, REVIEW
    (clean) to READY_FOR_MERGE, its state in the directory the test names
    (relative to the checkout; the default without one), its ``status
    --json`` as ``status --state-dir`` prints it from the checkout, and its
    run log's lines. Later engines and CLI commands of the test read the same
    fake GitHub, and none can launch an agent."""
    state = getattr(request, "param", DEFAULT_STATE_DIR)
    checkout = git_repo(tmp_path / "checkout")
    gh = FakeGitHub()
    fixed: list[str] = []

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW" and "Round 1" in req.prompt:
            finding = {
                "id": "R1-F1",
                "classification": "non-blocked",
                "title": "missing test",
                "location": "tests/",
                "required_resolution": "add a regression test",
            }
            return block(review_result(1, gh.prs[PR].head_sha, [finding]))
        if req.phase == "FIX":
            previous = gh.prs[PR].head_sha
            fixed.append(fix_commit(req))
            return block(
                {
                    "phase": "FIX",
                    "status": "success",
                    "previous_head_sha": previous,
                    "head_sha": fixed[0],
                    "resolutions": [
                        {"finding_id": "R1-F1", "resolution": "fixed", "commit_sha": fixed[0]}
                    ],
                }
            )
        return block(review_result(2, gh.prs[PR].head_sha))

    eng = make_engine(checkout / state, agent, github=gh, origin=True, workdir=checkout)
    scripted = eng.provider
    launch = scripted.execute

    def execute(req):
        # Claude Code's launches carry the cost its stream reported.
        result = launch(req)
        if req.profile.provider == "claude":
            result.provider_summary.update(_claude_summary())
        return result

    monkeypatch.setattr(scripted, "execute", execute)
    eng._save()
    eng.run(max_steps=20)
    run = load_state(eng.paths.state_file)
    assert run.phase.value == "READY_FOR_MERGE"

    real = ControllerEngine.__init__
    launches: list[str] = []

    def no_agent(req) -> str:
        launches.append(req.phase)
        return ""

    def on_the_fake(self, *a, **k):
        k.setdefault("github", gh)
        no_agents = ScriptedProvider(no_agent)
        k.setdefault(
            "providers", ProviderRegistry(overrides={"claude": no_agents, "opencode": no_agents})
        )
        real(self, *a, **k)

    monkeypatch.setattr(ControllerEngine, "__init__", on_the_fake)
    monkeypatch.chdir(checkout)

    def status() -> Path:
        """``status --json`` as the guide's command prints it now, in a file
        outside the checkout."""
        capsys.readouterr()
        assert cli.main(["--state-dir", state, "status", "--json"]) == 0
        served = tmp_path / "status-served.json"
        served.write_text(capsys.readouterr().out, encoding="utf-8")
        return served

    yield {
        "checkout": checkout,
        "state": state,
        "status": status,
        "events": checkout / state / "logs" / run.run_id / "events.jsonl",
        "run_id": run.run_id,
        "head": gh.prs[PR].head_sha,
        "fixed": fixed[0],
    }
    assert launches == [], "a command after the scripted run launched an agent"


def _read_trace(checkout: Path, out: str) -> tuple[dict, list[dict]]:
    """The two excerpts the guide's commands wrote into ``out``."""
    trace = checkout / out
    excerpt = json.loads((trace / "status-excerpt.json").read_text(encoding="utf-8"))
    lines = (trace / "events-excerpt.jsonl").read_text(encoding="utf-8").splitlines()
    return excerpt, [json.loads(line) for line in lines]


def _assert_the_demonstrated_run(demo_run, excerpt: dict, lines: list[dict]) -> None:
    """The state excerpt shows the phase and both rounds with their commits
    and comments; the events excerpt shows the phase sequence with durations
    and Claude Code's cost, and none of the fields the guide leaves out."""
    assert excerpt["run_id"] == demo_run["run_id"]
    assert excerpt["phase"] == "READY_FOR_MERGE" and excerpt["review_round"] == 2
    assert excerpt["pr"]
    first, second = excerpt["reviews"]
    assert (first["result"], second["result"]) == ("needs_fix", "clean")
    assert first["finding_count"] == 1 and second["finding_count"] == 0
    assert second["reviewed_head_sha"] == demo_run["fixed"] == demo_run["head"]
    assert first["reviewed_head_sha"] != second["reviewed_head_sha"]
    assert all(review["review_comment_url"] for review in excerpt["reviews"])

    agents = [line for line in lines if line["phase"] in AGENT_PHASES]
    assert [line["phase"] for line in agents] == ["ANALYZE_EXECUTE", "REVIEW", "FIX", "REVIEW"]
    # The rounds completed when each launch began, as the guide says: a
    # REVIEW line shows the round before its own.
    assert [line["review_round"] for line in agents] == [0, 0, 1, 1]
    assert all(line["seconds"] == 1 for line in agents)  # the scripted stamps
    claude = [line for line in agents if line["provider"] == "claude"]
    assert [line["phase"] for line in claude] == ["ANALYZE_EXECUTE", "FIX"]
    assert all(line["cost_micro_usd"] == round(COST_USD * 1_000_000) for line in claude)
    left_out = {"cwd", "log_dir", "command", "metadata", "error", "parsed_result"}
    assert not left_out & set().union(*lines)


def test_the_guide_has_both_excerpts_and_the_run_id_lookup():
    sources = [source for _, _, source in _jq_commands()]
    assert sources.count("status") >= 2, "the state excerpt and the run id"
    assert sources.count("events") == 1


def test_the_trace_is_collected_from_the_state_directory_the_operator_sets():
    """One block sets ``state`` and ``out``, ``state`` to the controller's
    default, and no command of the section names a state directory itself."""
    blocks = _code_blocks(GUIDE, COLLECT)
    setters = [text for text in blocks if _assignment(text, "state") is not None]
    assert len(setters) == 1
    assert _assignment(setters[0], "state") == DEFAULT_STATE_DIR
    assert _assignment(setters[0], "out") == "trace/run-1"
    rest = "\n".join(text for text in blocks if text is not setters[0])
    assert DEFAULT_STATE_DIR not in rest
    assert '--state-dir "$state"' in rest


def test_every_field_the_guides_jq_programs_read_is_one_a_run_writes(demo_run):
    """Fields read from ``status --json`` are keys of a real run's document;
    fields read from ``events.jsonl`` are keys of its lines, Claude Code's
    ``cost_micro_usd`` included."""
    written = {
        "status": _keys(json.loads(demo_run["status"]().read_text(encoding="utf-8"))),
        "events": set().union(
            *(
                _keys(json.loads(line))
                for line in demo_run["events"].read_text(encoding="utf-8").splitlines()
            )
        ),
    }
    unknown = {
        f"{source}: .{name}"
        for _, program, source in _jq_commands()
        for name in _fields(program)
        if name not in written[source]
    }
    assert unknown == set()


def test_the_field_reader_sees_shorthand_entries_and_skips_strings():
    program = '{a, b: .c.d, e: [.f[] | {g, h}], i: (.j | sub("\\\\.k"; ""))}'
    assert _fields(program) == {"a", "c", "d", "f", "g", "h", "j"}


@needs_jq
@pytest.mark.parametrize("demo_run", [DEFAULT_STATE_DIR, ELSEWHERE], indirect=True)
def test_the_guides_commands_show_what_the_claims_table_reads(demo_run):
    """Run as written, from the checkout, with ``state`` naming the run's
    state directory, the default or another: ``status`` is asked for that
    directory, and both excerpts are the run's."""
    checkout, state = demo_run["checkout"], demo_run["state"]
    argv = _collect(checkout, state, "trace/run-1", demo_run["status"]())
    assert argv[-2:] == ["status", "--json"]
    assert argv[argv.index("--state-dir") + 1] == state
    _assert_the_demonstrated_run(demo_run, *_read_trace(checkout, "trace/run-1"))


@needs_jq
@pytest.mark.parametrize("demo_run", [ELSEWHERE], indirect=True)
def test_a_run_collected_before_it_is_replaced_keeps_its_trace(demo_run, capsys):
    """``run --force`` replaces the state: ``status`` then shows only the new
    run, which blocks on the old run's pull request without launching an
    agent. The old run's trace, collected first into its own directory, is
    untouched by collecting the new one into another, and its run log is
    still where the guide says it stays."""
    checkout, state = demo_run["checkout"], demo_run["state"]
    _collect(checkout, state, "trace/run-1", demo_run["status"]())
    kept = {p.name: p.read_bytes() for p in (checkout / "trace" / "run-1").iterdir()}

    replace = ["--state-dir", state, "run", "--force", "--epic", EPIC, "--issue", ISSUE]
    assert cli.main([*replace, "--max-steps", "2"]) == 1
    assert "pr-owner" in capsys.readouterr().out

    _collect(checkout, state, "trace/run-2", demo_run["status"]())
    assert {p.name: p.read_bytes() for p in (checkout / "trace" / "run-1").iterdir()} == kept
    _assert_the_demonstrated_run(demo_run, *_read_trace(checkout, "trace/run-1"))
    assert demo_run["events"].is_file()

    excerpt, lines = _read_trace(checkout, "trace/run-2")
    assert excerpt["run_id"] != demo_run["run_id"]
    assert excerpt["phase"] == "BLOCKED" and excerpt["reviews"] == []
    assert [line["phase"] for line in lines] == ["INITIALIZING-epic-binding"]


def test_the_guides_links_resolve():
    broken = [
        problem
        for target in _links(GUIDE)
        if "://" not in target and not target.startswith("mailto:")
        for problem in [_broken(GUIDE, target)]
        if problem
    ]
    assert broken == []


def test_every_link_into_the_guide_resolves():
    broken = []
    for source in _markdown_files():
        for target in _links(source):
            file_part = target.partition("#")[0]
            if "://" in target or not file_part:
                continue
            if (source.parent / file_part).resolve() == GUIDE:
                broken += [p for p in [_broken(source, target)] if p]
    assert broken == []
