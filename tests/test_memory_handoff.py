"""Issue Working Memory's single-agent handoff (#333; ADR 0015 D9, D11 to D13, D20).

End to end and offline: real git repositories and a local bare origin under
``tmp_path``, the in-memory GitHub, real Python validators the controller
runs, and a scripted agent that holds Pi's fresh-context proof
(:class:`tests.test_memory.PiShaped`). The outer sandbox's probe answers
"supported" (conftest's autouse ``sandbox_probe``), and a validator's launch
runs unsandboxed here and reports the facts an enforced one does
(:func:`enforced_validators`), so the suite runs on a host without the real
backend. What is held:

- the projection reaches every Memory launch of ANALYZE_EXECUTE as
  ``memory.md`` of its read context, rendered from the committed snapshot for
  the ordinal its intent save spends and bound to its candidate; no other
  phase gets one;
- the controller's validation-failure observation is saved with the record
  it cites, and an accepted result's proposals with its acceptance, never
  with a refused, corrected or lost reply;
- with Memory off, the prompt, the read context, the state and the run's
  behaviour are what they were;
- crashes on both sides of every Memory write and every launch, secrets,
  bounds, staleness and retraction.

The store's own rules are in ``tests/test_memory.py``.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from pathlib import Path

import pytest

import autoforge
from autoforge.auto_recovery import StepKey
from autoforge.config import MemoryConfig, default_config
from autoforge.doctor import Doctor
from autoforge.evidence import MAX_FAILURE_COUNT, WorktreeProvenance, parse_record
from autoforge.executor import execute
from autoforge.memory import (
    MAX_PROJECTION_BYTES,
    MAX_PROJECTION_CHARS,
    MAX_TEXT_BYTES,
    Action,
    Actor,
    Kind,
    MemoryScope,
    MemoryStore,
)
from autoforge.memory_handoff import (
    MAX_PROPOSALS,
    MEMORY_FILE,
    SUPPORTED_PHASES,
    UNSUPPORTED_PHASES,
    Proposal,
    Proposals,
    admit_proposals,
    observe_validation_failure,
    parse_proposals,
    projection_request,
    render_launch_projection,
)
from autoforge.prompts import load_template
from autoforge.providers import ProviderRegistry
from autoforge.sandbox import TEARDOWN_UNCOUNTED, SandboxFacts
from autoforge.state import load_state
from autoforge.transitions import AGENT_PHASES, Phase
from tests.test_evidence import RUN, record, remote
from tests.test_memory import PiShaped, pi_profile

from .conftest import (
    GIT_IDENT,
    FakeGitHub,
    analyze_payload,
    block,
    commit_in,
    connect_origin,
    git_out,
    make_engine,
    review_result,
    scripted,
)

PY = sys.executable
# Passes only on a tree holding ``fixed.txt``, and says why on both streams.
NEEDS_FIX = [
    PY,
    "-c",
    "import os, sys\n"
    "if os.path.exists('fixed.txt'):\n"
    "    sys.exit(0)\n"
    "print('fixed.txt is missing')\n"
    "sys.stderr.write('validator: FAILED\\n')\n"
    "sys.exit(1)",
]
FAILS = [PY, "-c", "import sys; print('the suite failed'); sys.exit(3)"]
REPO = "owner/repo"
ISSUE = "https://github.com/owner/repo/issues/2"
# A credential of the shape the run-log redactor replaces (``github-token``).
SECRET = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0" * 2
SECTION = "## Issue Working Memory"


# -- fixtures --------------------------------------------------------------------------------


def enforced_validators(request):
    """The executor, with a validator's sandboxed launch run as an enforced one reports.

    A validation command under ``sandbox.mode: required`` carries a launch
    description; here it runs without one and returns the facts the real
    backend records for an enforced launch, so its record says
    ``enforced: true`` as on a capable host. Every other process runs as it
    would.
    """
    if request.sandbox is None:
        return execute(request)
    result = execute(dataclasses.replace(request, sandbox=None))
    result.sandbox = SandboxFacts(
        enforced=True,
        backend="bwrap",
        backend_version="0.0-test",
        teardown=TEARDOWN_UNCOUNTED,
        network="host",
    )
    return result


@pytest.fixture
def pi_home(tmp_path_factory, monkeypatch):
    """A home of the test's own with an empty ``~/.pi/agent``, and no agent directory set."""
    home = tmp_path_factory.mktemp("home")
    (home / ".pi" / "agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    return home


def memory_config(*commands, memory=True):
    cfg = default_config()
    cfg.memory = MemoryConfig(memory)
    cfg.sandbox.mode = "required"
    cfg.validation.commands = [list(c) for c in commands]
    cfg.profiles["analyze_execute"] = pi_profile()
    return cfg


def memory_engine(state_dir, gh, script, *commands, memory=True, cfg=None, workdir=None, new=True):
    """A REMOTE run at INITIALIZING whose ANALYZE_EXECUTE agent is ``script``, behind Pi's proof.

    A new run (``new``) records its configuration as ``run`` does, Memory
    included, so its store is on exactly when ``memory`` is, and is saved;
    otherwise the caller loads the run already on disk.
    """
    cfg = cfg or memory_config(*commands, memory=memory)
    eng = make_engine(state_dir, None, github=gh, cfg=cfg, origin=workdir is None, workdir=workdir)
    pi = PiShaped(script)
    eng.providers = ProviderRegistry(
        overrides={"pi": pi, "claude": eng.provider, "opencode": eng.provider}
    )
    eng._runner = enforced_validators
    if new:
        eng.record_run_configuration()
        eng._save()
    return eng, pi


def producing(state_dir, gh, script, *commands, memory=True):
    eng, pi = memory_engine(state_dir, gh, script, *commands, memory=memory)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    return eng, pi


def resumed(eng, gh, script):
    """A new controller process on ``eng``'s run: its state, configuration, origin and GitHub."""
    eng.close()
    eng2, pi = memory_engine(
        eng.paths.state_dir, gh, script, cfg=eng.config, workdir=eng.workdir, new=False
    )
    connect_origin(eng2, eng.origin)
    eng2.origin = eng.origin
    eng2.load()
    return eng2, pi


def saved(eng):
    return load_state(eng.paths.state_file)


def read_context_of(prompt: str) -> Path:
    """The read-context directory a prompt names (common.md's ``{{READ_CONTEXT_DIR}}``)."""
    found = re.search(r"^`(/[^`\n]*/read-context/[^`\n]+)`$", prompt, re.M)
    assert found, "the prompt names no read context"
    return Path(found.group(1))


def memory_md(req) -> str | None:
    """The ``memory.md`` of the launch ``req`` was given, or ``None`` when it has none."""
    path = read_context_of(req.prompt) / MEMORY_FILE
    return path.read_text(encoding="utf-8") if path.exists() else None


class Seen:
    """A handler wrapper that keeps what each launch was shown: its memory.md and prompt."""

    def __init__(self, *steps):
        self.inner = scripted(*steps)
        self.memory: list[str | None] = []
        self.prompts: list[str] = []
        self.index: list[str] = []

    def __call__(self, req):
        self.memory.append(memory_md(req))
        self.prompts.append(req.prompt)
        self.index.append((read_context_of(req.prompt) / "README.md").read_text())
        return self.inner(req)


def implement(req, **overrides):
    return block(analyze_payload(commit_in(req.cwd), **overrides))


def repaired(req, **overrides):
    """An implementer that commits ``fixed.txt``, so :data:`NEEDS_FIX` passes."""
    (Path(req.cwd) / "fixed.txt").write_text("ok\n", encoding="utf-8")
    git_out("-C", req.cwd, "add", "fixed.txt")
    git_out("-C", req.cwd, *GIT_IDENT, "commit", "-q", "-m", "Implement the feature (#2)")
    head = git_out("-C", req.cwd, "rev-parse", "HEAD")
    return block(analyze_payload(head, **overrides))


def stopping(status, memory=None, message="cannot go on"):
    """An agent that stops with ``status``, proposing ``memory`` when given."""

    def handler(req):
        payload = {"phase": "ANALYZE_EXECUTE", "status": status, "message": message}
        if memory is not None:
            payload["memory"] = memory
        return block(payload)

    return handler


def add(kind, text):
    return {"op": "add", "kind": kind, "text": text}


def records(state) -> list[dict]:
    return state.memory().catalog(state.current_issue_url)["records"]


def texts(state) -> list[str]:
    return [r["text"] for r in records(state) if r["status"] == "current"]


def state_tree_text(eng) -> str:
    """Every file under the state directory, run log and read contexts included, as text."""
    parts = []
    for path in sorted(Path(eng.paths.state_dir).rglob("*")):
        if path.is_file():
            parts.append(path.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(parts)


def execution_records(eng) -> list[dict]:
    """Every ANALYZE_EXECUTE agent launch's ``request.json`` of the run, in step order."""
    run = Path(eng.paths.state_dir) / "logs" / eng.state.run_id
    found = []
    for path in sorted(run.glob("*/request.json")):
        data = json.loads(path.read_text())
        if data.get("phase") == "ANALYZE_EXECUTE" and data.get("profile") == "analyze_execute":
            found.append(data)
    return found


# -- the supported scope (D13), as doctor and the documentation name it -----------------------


def test_only_analyze_execute_is_supported_and_every_other_agent_phase_is_named():
    assert SUPPORTED_PHASES == (Phase.ANALYZE_EXECUTE,)
    assert set(UNSUPPORTED_PHASES) | set(SUPPORTED_PHASES) == set(AGENT_PHASES)
    assert not set(UNSUPPORTED_PHASES) & set(SUPPORTED_PHASES)


# -- a validation failure and its retry, end to end --------------------------------------------


def test_a_validation_failure_reaches_the_retry_as_an_observed_record(tmp_state_dir, pi_home):
    gh = FakeGitHub()
    seen = Seen(implement, repaired)
    eng, pi = producing(tmp_state_dir, gh, seen, NEEDS_FIX)
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE" and "did not pass" in out.message
    s = saved(eng)
    (failed,) = s.validation_records
    commit = failed["candidate"]["commit"]
    # One save holds the record and the observation that cites it (D11 stage 4).
    (observed,) = records(s)
    assert observed["kind"] == "validation-failure" and observed["author"] == "controller"
    assert observed["citation"] == ["validation", failed["seq"], failed["digest"]]
    assert observed["scope"]["candidate"] == commit
    assert observed["scope"]["base"] == failed["candidate"]["provenance"]["base_revision"]
    assert observed["attempt"] == s.launch_seq == 1
    assert "fixed.txt is missing" in observed["text"]
    # The first launch had Memory, empty: the projection says nothing applies.
    first = seen.memory[0]
    assert first is not None and "Shown 00;" in first and "[No current record applies]" in first
    assert SECTION in seen.prompts[0] and f"`{MEMORY_FILE}`" in seen.prompts[0]

    assert eng.step().next_phase == "REVIEW"
    retry = seen.memory[1]
    assert retry is not None
    assert "launch: 2" in retry and f"Candidate: {commit}" in retry
    assert f"Observed validation {failed['seq']} ({failed['digest']}): FAIL on {commit}" in retry
    assert "Shown 01;" in retry and "needs revalidation 00" in retry
    assert f"- `{MEMORY_FILE}`:" in seen.index[1]
    # The run log records what each launch was shown, never a record's text.
    logged = [r["metadata"]["memory"] for r in execution_records(eng)]
    assert [m["launch"] for m in logged] == [1, 2]
    assert [m["shown"] for m in logged] == [0, 1]
    assert all(m["chars"] <= MAX_PROJECTION_CHARS and m["render_ms"] < 1000 for m in logged)
    assert logged[1]["candidate"] == commit and logged[1]["memory_digest"]
    assert all("fixed.txt is missing" not in json.dumps(m) for m in logged)
    assert pi.calls and all(r.fresh_context for r in pi.calls)
    eng.close()


def test_a_failure_on_an_earlier_commit_is_labelled_stale_on_its_descendant(tmp_state_dir, pi_home):
    """Every projection is bound to the current candidate: a failure observed
    on C1 is shown as established on C1 only. On C2, built on C1, it names its
    source candidate and needs revalidation; C2's own failure is established."""
    gh = FakeGitHub()
    assert MAX_FAILURE_COUNT == 3, "the scenario spends the whole bound"
    seen = Seen(implement, implement, implement)
    eng, _ = producing(tmp_state_dir, gh, seen, FAILS)
    phases = [eng.step().next_phase for _ in range(MAX_FAILURE_COUNT)]
    assert phases == ["ANALYZE_EXECUTE", "ANALYZE_EXECUTE", "BLOCKED"], "the bound is as before"
    s = saved(eng)
    first, second, third = (r["candidate"]["commit"] for r in s.validation_records)
    shown = seen.memory[2]
    assert f"Candidate: {second}" in shown
    assert f"Observed validation 2 ({s.validation_records[1]['digest']}): FAIL on {second}" in (
        shown
    )
    m1 = shown.index("\nM1 ")
    assert f"Source candidate: {first}" in shown[m1:]
    assert "retained; does not establish this claim on this launch" in shown[m1:]
    assert "needs revalidation 01" in shown
    # Each failure is observed once, bound to its own commit.
    assert [r["scope"]["candidate"] for r in records(s)] == [first, second, third]
    eng.close()


# -- the pure rules: candidate binding, proposals, observations ---------------------------------

STEP = StepKey(Phase.ANALYZE_EXECUTE, ISSUE, "", 0, 1)
C1, C2, BASE = "c" * 40, "d" * 40, "b" * 40


def enabled(seq=1):
    return MemoryStore(
        REPO, RUN, seq, dict(enabled=True, run_id=RUN, events=[], texts={}, refusals=0)
    )


def failing_record(seq=1, *, commit=C1, base=BASE, **kwargs):
    candidate = remote(commit=commit, provenance=WorktreeProvenance(2, "2", True, base))
    return record(seq, candidate=candidate, exit_code=1, **kwargs)


def render(store, candidate="", ancestors=(), receipts=()):
    request = projection_request(store, STEP, candidate=candidate, ancestors=ancestors)
    return render_launch_projection(store, request, validation_records=list(receipts)).text


def test_a_record_bound_to_a_base_is_shown_only_on_its_descendants():
    """Established on the failing commit; labelled stale, with its source, on
    a descendant of the same base; omitted and counted on a commit that does
    not descend from the base, and when the launch has no candidate."""
    rec = failing_record()
    out = observe_validation_failure(
        enabled(), STEP, parse_record(rec), attempt=1, text="definition [0] failed"
    )
    assert out is not None and out.status == "applied"
    store = out.store
    assert store.range_bases(ISSUE) == (BASE,)
    on_it = render(store, C1, (BASE,), [rec])
    assert f"Observed validation 1 ({rec['digest']}): FAIL on {C1}" in on_it
    stale = render(store, C2, (BASE,), [rec])
    assert f"Source candidate: {C1}" in stale and "needs revalidation 01" in stale
    assert "Observed validation" not in stale
    for candidate, ancestors in ((C2, ()), ("", ())):
        unrelated = render(store, candidate, ancestors, [rec])
        assert "Shown 00;" in unrelated and "inapplicable 01" in unrelated
        assert "definition [0] failed" not in unrelated


def test_an_observation_is_keyed_by_its_citation_and_passes_are_not_observed():
    rec = parse_record(failing_record())
    store = observe_validation_failure(enabled(2), STEP, rec, attempt=1, text="failed").store
    # The same record at a later launch (a relaunch reporting the same
    # commit, a resume): nothing to write, and nothing counted.
    assert observe_validation_failure(store, STEP, rec, attempt=2, text="failed") is None
    assert store.refusals == 0
    passed = parse_record(record(2, exit_code=0))
    assert observe_validation_failure(store, STEP, passed, attempt=2, text="passed") is None
    off = MemoryStore(REPO, RUN, 1, {})
    assert observe_validation_failure(off, STEP, rec, attempt=1, text="failed") is None


@pytest.mark.parametrize(
    "raw, entries, refused",
    [
        (None, 0, 0),
        ("add a note", 0, 1),
        ({"op": "add"}, 0, 1),
        ([add("note", "kept")], 1, 0),
        ([add("seed", "an operator's kind")], 0, 1),
        ([add("validation-failure", "a controller's kind")], 0, 1),
        ([add("candidate-finding", "needs an exact candidate")], 0, 1),
        ([{**add("note", "x"), "extra": 1}], 0, 1),
        ([{"op": "retract", "record": True, "reason": "a bool is no record"}], 0, 1),
        ([{"op": "retract", "record": 0, "reason": "no record zero"}], 0, 1),
        ([{"op": "correct", "record": 1, "reason": "no text"}], 0, 1),
        ([{"op": "supersede", "record": 1, "reason": "not offered"}], 0, 1),
        ([add("note", str(i)) for i in range(MAX_PROPOSALS + 3)], MAX_PROPOSALS, 3),
    ],
)
def test_a_results_memory_list_is_parsed_without_raising(raw, entries, refused):
    parsed = parse_proposals(raw)
    assert (len(parsed.entries), parsed.refused) == (entries, refused)


def admitted(store, *entries, attempt=1, refused=0):
    return admit_proposals(
        store, Proposals(tuple(entries), refused), step=STEP, attempt=attempt, input_sha=BASE
    )


def test_an_accepted_results_proposals_are_admitted_once_and_counted_when_refused():
    note = Proposal(Action.ADD, kind=Kind.HYPOTHESIS, text="the lexer splits 1e5")
    first = admitted(enabled(), note, refused=2)
    assert (first.applied, first.replayed, first.refused) == (1, 0, 2)
    assert first.store.refusals == 2
    (row,) = first.store.catalog(ISSUE)["records"]
    assert row["author"] == "agent" and row["kind"] == "hypothesis"
    # The same accepted result admitted again (a crash before its save was
    # published, then the same reply): a replay, never a second record.
    again = admitted(first.store, note)
    assert (again.applied, again.replayed) == (0, 1)
    assert len(again.store.catalog(ISSUE)["records"]) == 1
    # A later launch corrects it, then retracts it.
    later = MemoryStore(REPO, RUN, 2, again.store.encode())
    fixed = admitted(
        later,
        Proposal(Action.CORRECT, record=1, text="the lexer is fine", reason="checked"),
        attempt=2,
    )
    assert fixed.applied == 1
    assert fixed.store.catalog(ISSUE)["records"][0]["text"] == "the lexer is fine"
    gone = admitted(fixed.store, Proposal(Action.RETRACT, record=1, reason="wrong"), attempt=2)
    assert gone.store.catalog(ISSUE)["records"][0]["status"] == "retracted"
    # A record that does not exist, and Memory off: nothing changes the step.
    missing = admitted(gone.store, Proposal(Action.RETRACT, record=9, reason="no"), attempt=2)
    assert (missing.applied, missing.refused) == (0, 1)
    off = admitted(MemoryStore(REPO, RUN, 1, {}), note, refused=1)
    assert (off.applied, off.refused, off.store.encode()) == (0, 0, {})


def test_an_agent_cannot_revise_the_controllers_or_the_operators_record():
    rec = parse_record(failing_record())
    store = observe_validation_failure(enabled(2), STEP, rec, attempt=1, text="failed").store
    for proposal in (
        Proposal(Action.CORRECT, record=1, text="it passed", reason="trust me"),
        Proposal(Action.RETRACT, record=1, reason="hide it"),
    ):
        out = admitted(store, proposal, attempt=2)
        assert (out.applied, out.refused) == (0, 1)
        assert out.store.catalog(ISSUE)["records"][0]["status"] == "current"
    seeded = (
        enabled(0)
        .add(
            actor=Actor.OPERATOR,
            kind=Kind.SEED,
            scope=MemoryScope(StepKey(Phase.INITIALIZING, ISSUE, "", 0, 1)),
            text="the operator's lesson",
        )
        .store
    )
    seeded = MemoryStore(REPO, RUN, 1, seeded.encode())
    out = admitted(seeded, Proposal(Action.RETRACT, record=1, reason="hide it"))
    assert out.refused == 1 and out.store.catalog(ISSUE)["records"][0]["status"] == "current"


def test_a_proposal_from_a_launch_before_a_records_latest_change_is_refused():
    """The controller names the record's current revision; the store still
    refuses a revision from a launch that started before the record changed."""
    note = Proposal(Action.ADD, kind=Kind.NOTE, text="first note")
    store = MemoryStore(REPO, RUN, 3, admitted(enabled(3), note, attempt=3).store.encode())
    late = admitted(
        store,
        Proposal(Action.CORRECT, record=1, text="from an older launch", reason="late"),
        attempt=2,
    )
    assert (late.applied, late.refused) == (0, 1)


def test_a_seeded_secret_in_a_proposal_is_refused_and_never_kept():
    out = admitted(
        enabled(),
        Proposal(Action.ADD, kind=Kind.NOTE, text=f"the token is {SECRET}"),
        Proposal(Action.ADD, kind=Kind.NOTE, text="x" * (MAX_TEXT_BYTES + 1)),
    )
    assert (out.applied, out.refused) == (0, 2)
    assert SECRET not in json.dumps(out.store.encode())


def test_a_seeded_secret_in_a_validators_output_is_redacted_before_memory_and_projection():
    rec = failing_record(stderr=f"token={SECRET}\nfailed\n")
    view = parse_record(rec)
    text = f"definition [0] failed\n{view.raw['output']['stderr_tail']}\nalso {SECRET}"
    out = observe_validation_failure(enabled(), STEP, view, attempt=1, text=text)
    assert out is not None and out.status == "applied"
    assert SECRET not in json.dumps(out.store.encode())
    assert SECRET not in render(out.store, C1, receipts=[rec])


def test_the_projection_stays_within_its_bounds_and_says_what_it_cut():
    store = enabled(64)
    for n in range(1, 40):
        note = Proposal(Action.ADD, kind=Kind.NOTE, text=f"lesson {n}: " + "é" * 900)
        store = admitted(store, note, attempt=n).store
    text = render(store)
    assert len(text) <= MAX_PROJECTION_CHARS and len(text.encode()) <= MAX_PROJECTION_BYTES
    shown = int(re.search(r"Shown (\d+);", text).group(1))
    omitted = int(re.search(r"omitted (\d+);", text).group(1))
    assert shown + omitted == 39 and omitted > 0
    # Newest first among proposals: the latest lessons are the ones shown.
    assert "lesson 39:" in text and "lesson 1:" not in text


# -- crash windows of each Memory write and each launch (D11) ------------------------------------


def test_a_crash_before_the_record_save_leaves_neither_record_nor_observation(
    tmp_state_dir, pi_home
):
    gh = FakeGitHub()
    eng, _ = producing(tmp_state_dir, gh, scripted(implement), NEEDS_FIX)
    append = eng._append_validation_record

    def crash_before(*args, **kwargs):
        raise KeyboardInterrupt

    eng._append_validation_record = crash_before
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    s = saved(eng)
    assert s.validation_records == [] and records(s) == []
    assert s.validation_attempt, "the marker the next command records as interrupted"
    eng._append_validation_record = append
    eng.close()


def test_a_crash_after_the_record_save_keeps_both_and_rederiving_adds_nothing(
    tmp_state_dir, pi_home
):
    """The record and its observation are one save. After a crash past it,
    the next process reports the same failing commit again: the record is
    reused, counted again, and the observation is not written twice."""
    gh = FakeGitHub()
    eng, _ = producing(tmp_state_dir, gh, scripted(implement), NEEDS_FIX)
    gate = eng._gate_producer

    def gated_then_crashed(*args):
        with pytest.raises(Exception, match="did not pass"):
            gate(*args)
        raise KeyboardInterrupt

    eng._gate_producer = gated_then_crashed
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    s = saved(eng)
    assert len(s.validation_records) == 1 and len(records(s)) == 1

    def same_commit(req):
        return block(analyze_payload(git_out("-C", req.cwd, "rev-parse", "HEAD")))

    seen = Seen(same_commit)
    eng2, _ = resumed(eng, gh, seen)
    assert eng2.step().next_phase == "ANALYZE_EXECUTE"
    s = saved(eng2)
    assert len(s.validation_records) == 1, "the terminal record was reused"
    assert s.producer_validation_failure["count"] == 2
    assert len(records(s)) == 1 and s.memory().refusals == 0
    assert seen.memory[0].count("Observed validation 1 ") == 1
    eng2.close()


def stop_save_crash(eng):
    """Crash at the save that would record the agent's own stop."""
    save = eng._save

    def crash_at_the_stop():
        if eng.state.phase in (Phase.BLOCKED, Phase.FAILED):
            raise KeyboardInterrupt
        save()

    eng._save = crash_at_the_stop


@pytest.mark.parametrize("status", ["blocked", "failure"])
def test_a_stop_results_proposals_land_in_its_stop_save_and_nowhere_before(
    tmp_state_dir, pi_home, status
):
    gh = FakeGitHub()
    proposals = [add("failed-approach", "Bumping the timeout did not help.")]
    eng, _ = producing(tmp_state_dir, gh, scripted(stopping(status, proposals)))
    stop_save_crash(eng)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    s = saved(eng)
    assert s.phase == Phase.ANALYZE_EXECUTE and records(s) == [], "a lost reply leaves none"
    eng2, _ = resumed(eng, gh, scripted(stopping(status, proposals)))
    out = eng2.step()
    assert out.next_phase == ("BLOCKED" if status == "blocked" else "FAILED")
    s = saved(eng2)
    assert texts(s) == ["Bumping the timeout did not help."]
    assert s.launch_seq == 2 and records(s)[0]["attempt"] == 2
    eng2.close()


def test_a_blocked_results_proposals_reach_the_launch_after_unblock(tmp_state_dir, pi_home):
    gh = FakeGitHub()
    seen = Seen(stopping("blocked", [add("handoff", "Start from tests/test_lexer.py.")]), implement)
    eng, _ = producing(tmp_state_dir, gh, seen)
    assert eng.step().next_phase == "BLOCKED"
    eng.unblock("carry on")
    assert eng.step().next_phase == "REVIEW"
    shown = seen.memory[1]
    assert "M1 handoff — proposal" in shown and "Start from tests/test_lexer.py." in shown
    eng.close()


def test_a_success_keeps_its_proposals_with_its_plan_and_a_crash_after_adds_none(
    tmp_state_dir, pi_home
):
    gh = FakeGitHub()
    noted = [add("repository-constraint", "make check runs ruff on tests/ too.")]

    def with_notes(req):
        return implement(req, memory=noted)

    eng, _ = producing(tmp_state_dir, gh, scripted(with_notes))
    complete = eng._complete_analyze

    def planned_then_crashed():
        raise KeyboardInterrupt

    eng._complete_analyze = planned_then_crashed
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    s = saved(eng)
    assert s.effect_records and texts(s) == ["make check runs ruff on tests/ too."]
    eng._complete_analyze = complete
    eng2, pi = resumed(eng, gh, scripted(lambda req: "the agent must not run again"))
    assert eng2.step().next_phase == "REVIEW"
    assert pi.calls == []
    assert texts(saved(eng2)) == ["make check runs ruff on tests/ too."]
    eng2.close()


def test_a_refused_or_corrected_reply_leaves_no_proposal(tmp_state_dir, pi_home):
    """A reply the controller refuses (here a head_sha that is not the
    worktree's) is corrected; only the accepted correction's proposals are
    kept. A result whose commit fails validation keeps none either, only the
    controller's own observation."""
    gh = FakeGitHub()

    def wrong_head(req):
        commit_in(req.cwd)
        return block(analyze_payload("f" * 40, memory=[add("note", "from a refused reply")]))

    def corrected(req):
        head = git_out("-C", req.cwd, "rev-parse", "HEAD")
        return block(analyze_payload(head, memory=[add("note", "from the accepted reply")]))

    eng, _ = producing(tmp_state_dir, gh, scripted(wrong_head, corrected), FAILS)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    s = saved(eng)
    assert [r["kind"] for r in records(s)] == ["validation-failure"]
    assert "from a refused reply" not in json.dumps(s.issue_memory)
    assert "from the accepted reply" not in json.dumps(s.issue_memory)
    eng.close()


def test_a_crash_between_the_projection_and_the_intent_save_renders_it_again(
    tmp_state_dir, pi_home
):
    """The projection is rendered before anything of the launch is saved: a
    crash there leaves the state as it was, and the next command renders the
    same bytes for the same ordinal."""
    gh = FakeGitHub()
    seen = Seen(stopping("blocked", [add("note", "a lesson")]), implement)
    eng, _ = producing(tmp_state_dir, gh, seen)
    eng.step()
    eng.unblock("go")
    before = eng.paths.state_file.read_bytes()
    written: list[str] = []
    collect = eng._collect_read_context

    def collected_then_crashed(phase):
        context = collect(phase)
        written.append(context.files[MEMORY_FILE])
        raise KeyboardInterrupt

    eng._collect_read_context = collected_then_crashed
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    assert eng.paths.state_file.read_bytes() == before
    eng2, _ = resumed(eng, gh, seen)
    assert eng2.step().next_phase == "REVIEW"
    assert seen.memory[1] == written[0]
    assert "launch: 2" in written[0] and "a lesson" in written[0]
    eng2.close()


def test_a_crash_after_the_intent_save_binds_the_next_launch_to_the_next_ordinal(
    tmp_state_dir, pi_home
):
    """W2: the ordinal is spent, nothing ran and Memory is unchanged; the next
    launch's projection is bound to the ordinal after it."""
    gh = FakeGitHub()
    seen = Seen(stopping("blocked", [add("note", "a lesson")]), implement)
    eng, _ = producing(tmp_state_dir, gh, seen)
    eng.step()
    eng.unblock("go")
    memory_before = saved(eng).issue_memory
    begin = eng._begin_step

    def saved_then_crashed(*args, **kwargs):
        raise KeyboardInterrupt

    eng._begin_step = saved_then_crashed
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    s = saved(eng)
    assert s.launch_seq == 2 and s.issue_memory == memory_before
    eng._begin_step = begin
    eng2, _ = resumed(eng, gh, seen)
    assert eng2.step().next_phase == "REVIEW"
    assert "launch: 3" in seen.memory[1] and "a lesson" in seen.memory[1]
    eng2.close()


# -- what Memory never reaches -------------------------------------------------------------------


def test_review_is_shown_no_memory_and_its_launch_reads_no_projection(tmp_state_dir, pi_home):
    gh = FakeGitHub()
    eng, _ = producing(
        tmp_state_dir, gh, scripted(lambda req: implement(req, memory=[add("note", "n")]))
    )
    assert eng.step().next_phase == "REVIEW"
    head = saved(eng).current_head_sha
    eng.provider._handler = lambda req: block(review_result(1, head))
    eng.step()
    (review,) = [r for r in eng.provider.calls if r.phase == "REVIEW"]
    assert SECTION not in review.prompt and MEMORY_FILE not in review.prompt
    assert not (read_context_of(review.prompt) / MEMORY_FILE).exists()
    eng.close()


def test_an_operator_retraction_is_absent_from_the_next_launch(tmp_state_dir, pi_home):
    gh = FakeGitHub()
    seen = Seen(stopping("blocked", [add("hypothesis", "It is a race in the cache.")]), implement)
    eng, _ = producing(tmp_state_dir, gh, seen)
    eng.step()
    s = eng.state
    store = s.memory().revise(
        Action.RETRACT, 1, actor=Actor.OPERATOR, reason="wrong", issue=s.current_issue_url
    )
    assert store.status == "applied"
    s.issue_memory = store.store.encode()
    eng._save()
    eng.unblock("go")
    eng.step()
    assert "race in the cache" not in seen.memory[1] and "withdrawn 01" in seen.memory[1]
    eng.close()


def test_no_seeded_secret_reaches_memory_a_projection_a_log_or_the_state(tmp_state_dir, pi_home):
    gh = FakeGitHub()
    leaky = [PY, "-c", f"import sys; sys.stderr.write('token={SECRET}\\n'); sys.exit(1)"]
    seen = Seen(
        lambda req: implement(req, memory=[add("note", f"use {SECRET}")]),
        stopping("blocked", [add("note", f"still {SECRET}"), add("note", "a clean note")]),
        implement,
    )
    eng, _ = producing(tmp_state_dir, gh, seen, leaky)
    eng.step()
    eng.step()
    s = saved(eng)
    assert texts(s)[-1] == "a clean note" and s.memory().refusals == 1
    eng.unblock("go")
    eng.step()
    assert all(SECRET not in (shown or "") for shown in seen.memory)
    assert SECRET not in state_tree_text(eng)
    eng.close()


# -- with Memory off, nothing changes ------------------------------------------------------------


def test_with_memory_off_the_prompt_read_context_state_and_run_are_unchanged(
    tmp_state_dir, pi_home
):
    """The Memory-off prompt is the template with the Memory placeholder
    rendered empty, byte for byte; no launch gets a memory.md or a Memory
    record in the run log; a ``memory`` list in a result is ignored, and the
    run's phases, records and state are those of the same run without it."""
    template = load_template("analyze_execute.md")
    assert template.count("{{MEMORY_CONTEXT}}") == 1
    assert "{{VALIDATION_FAILURE}}{{MEMORY_CONTEXT}}\n" in template

    def run(state_dir, with_field):
        gh = FakeGitHub()
        extra = {"memory": [add("note", "ignored with Memory off")]} if with_field else {}
        seen = Seen(lambda req: implement(req, **extra), lambda req: repaired(req, **extra))
        eng, _ = producing(state_dir, gh, seen, NEEDS_FIX, memory=False)
        phases = [eng.step().next_phase, eng.step().next_phase]
        s = saved(eng)
        eng.close()
        return eng, seen, phases, s

    eng, seen, phases, s = run(tmp_state_dir, True)
    _, plain, plain_phases, plain_state = run(tmp_state_dir.parent / "plain" / ".autoforge", False)
    assert phases == plain_phases == ["ANALYZE_EXECUTE", "REVIEW"]
    assert s.issue_memory == {} == plain_state.issue_memory
    assert [r["outcome"] for r in s.validation_records] == ["FAIL", "PASS"]
    assert seen.memory == [None, None]
    assert all(SECTION not in prompt and MEMORY_FILE not in prompt for prompt in seen.prompts)
    assert all(MEMORY_FILE not in index for index in seen.index)
    assert all("memory" not in r["metadata"] for r in execution_records(eng))
    assert eng._format_memory_context() == ""


def test_the_memory_section_is_the_only_difference_memory_makes_to_the_prompt(
    tmp_state_dir, pi_home
):
    gh = FakeGitHub()
    eng, _ = producing(tmp_state_dir, gh, scripted(), memory=False)
    off = eng.render_prompt_for(Phase.ANALYZE_EXECUTE)
    eng.state.issue_memory = eng.state.memory_setting(True)
    section = eng._format_memory_context()
    on = eng.render_prompt_for(Phase.ANALYZE_EXECUTE)
    assert section.startswith("\n\n" + SECTION) and section in on
    assert on.replace(section, "", 1) == off
    eng.close()


# -- doctor names the supported scope honestly ------------------------------------------------


@pytest.mark.parametrize("memory", [False, True])
def test_doctor_names_the_supported_phase_and_every_unsupported_one(tmp_path, memory):
    doctor = Doctor(cwd=str(tmp_path))
    doctor.config = memory_config(memory=memory)
    row = doctor.check_memory()
    assert row.required is False and row.ok
    if not memory:
        assert row.detail.startswith("off")
        return
    assert "ANALYZE_EXECUTE" in row.detail and MEMORY_FILE in row.detail
    for phase in UNSUPPORTED_PHASES:
        assert phase.value in row.detail
    assert "pi" in row.detail and "sandbox" in row.detail
    assert "No live run" in row.detail


# -- the controlled scenario: a failed approach, a corrected hypothesis, a crash -------------


def interrupted(req):
    """A launch the operator's ``Ctrl-C`` ends while the agent runs (W3)."""
    raise KeyboardInterrupt


def test_a_correction_survives_a_crash_and_the_next_launch_never_sees_what_it_replaced(
    tmp_state_dir, pi_home
):
    """Launch 1 records a failed approach and two hypotheses and stops
    ``blocked``. Launch 2 sees them, corrects one hypothesis, retracts the
    other and stops again. Launch 3 is interrupted while it runs, and a new
    controller process resumes: launch 4 sees the correction and the failed
    approach, and neither the text the correction replaced nor the
    retracted hypothesis."""
    gh = FakeGitHub()
    first = [
        add("failed-approach", "Patching parse() alone broke three callers in cli.py."),
        add("hypothesis", "The bug is in the parser's number handling."),
        add("hypothesis", "The bug only shows on Windows paths."),
    ]
    revisions = [
        {
            "op": "correct",
            "record": 2,
            "text": "The bug is in the lexer: it splits '1e5' into two tokens.",
            "reason": "the parser receives the wrong tokens",
        },
        {"op": "retract", "record": 3, "reason": "it reproduces on POSIX paths too"},
    ]
    seen = Seen(
        stopping("blocked", first, "need a decision on the parser"),
        stopping("blocked", revisions, "need a decision on the lexer"),
        interrupted,
    )
    eng, _ = producing(tmp_state_dir, gh, seen)
    assert eng.step().next_phase == "BLOCKED"
    s = saved(eng)
    assert [r["kind"] for r in records(s)] == ["failed-approach", "hypothesis", "hypothesis"]
    assert all(r["author"] == "agent" for r in records(s))
    eng.unblock("decided: look at the lexer")
    assert eng.step().next_phase == "BLOCKED"
    shown = seen.memory[1]
    assert "Patching parse() alone broke three callers" in shown
    assert "number handling" in shown and shown.count("— proposal") == 3
    s = saved(eng)
    assert {r["id"]: r["status"] for r in records(s)} == {
        1: "current",
        2: "current",
        3: "retracted",
    }
    eng.unblock("go on")
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    crashed = saved(eng)
    assert crashed.phase == Phase.ANALYZE_EXECUTE and crashed.launch_seq == 3
    assert crashed.issue_memory == s.issue_memory, "a launch writes nothing to Memory"

    after = Seen(implement)
    eng2, _ = resumed(eng, gh, after)
    assert eng2.step().next_phase == "REVIEW"
    shown = after.memory[0]
    assert "launch: 4" in shown
    assert "The bug is in the lexer: it splits '1e5' into two tokens." in shown
    assert "Patching parse() alone broke three callers in cli.py." in shown
    assert "number handling" not in shown and "Windows paths" not in shown
    assert "withdrawn 01" in shown
    eng2.close()


def test_a_crash_before_a_successs_plan_save_keeps_no_proposal(tmp_state_dir, pi_home):
    gh = FakeGitHub()
    noted = [add("note", "kept only with the plan")]
    eng, _ = producing(tmp_state_dir, gh, scripted(lambda req: implement(req, memory=noted)))

    def crash_before():
        raise KeyboardInterrupt

    eng._save_plan = crash_before
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    s = saved(eng)
    assert s.effect_records == [] and records(s) == []
    eng.close()


# -- no module that decides a phase, a write, a budget or a merge reads Memory (§4.2) --------

# What decides: the transitions, the effects and their journal, the merge gate's
# evidence, the result's parse, recovery and stop classification, the replan, the
# scheduler and GitHub's client. None imports Memory.
DECIDING_MODULES = (
    "auto_recovery",
    "certificate",
    "delivered_result",
    "effect_ops",
    "effects",
    "evidence",
    "github",
    "loop_guard",
    "premerge",
    "replan",
    "replan_txn",
    "result_parser",
    "scheduler",
    "stop_classification",
    "transitions",
)
# The modules that may: the store's checkpoint, the orchestration that saves
# Memory beside the step, the operator's commands and doctor's row.
MEMORY_IMPORTERS = {"cli", "doctor", "engine", "memory_handoff", "state"}


def _imports(path: Path) -> set[str]:
    import ast

    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                found.add(module.split(".")[0] if module else "")
                found.update(alias.name for alias in node.names if not module)
            elif module.startswith("autoforge."):
                found.add(module.split(".")[1])
        elif isinstance(node, ast.Import):
            found.update(
                a.name.split(".")[1] for a in node.names if a.name.startswith("autoforge.")
            )
    return found


def test_no_module_that_decides_imports_memory_and_its_importers_are_known():
    package = Path(autoforge.__file__).parent
    importers = {
        path.stem
        for path in package.glob("*.py")
        if {"memory", "memory_handoff"} & _imports(path) and path.stem != "memory"
    }
    assert importers == MEMORY_IMPORTERS
    for name in DECIDING_MODULES:
        assert not {"memory", "memory_handoff"} & _imports(package / f"{name}.py"), name
