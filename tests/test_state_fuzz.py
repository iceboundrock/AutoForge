"""The REMOTE state loader as the place illegal state stops existing (ADR 0009 A.2.3 B, #386).

Persisted state is the one input to the controller that no live actor
produced: it arrives from a disk that a crash, a hand edit, a truncated
write, a partial restore or another controller may have touched, and
everything downstream (which phase runs, which PR a review is bound to,
whether a bound is enforced at all) reads it as fact.

So the invariant is: **a REMOTE state object that exists satisfies every
invariant the loader promises.** ``load_state`` either produces a legal
state or raises :class:`StateError`; there is no "loaded but questionable"
state to check for later. These tests try to manufacture a counter-example:
a field the controller does not know, a root that is not an object, and
random mutations of a valid file.

They are the REMOTE forms of the LOCAL tests in ``tests/test_local.py`` and
``tests/test_local_state_fuzz.py`` that LOCAL's removal retires.
"""

import json
import random
import re
from dataclasses import fields
from pathlib import Path

import pytest

from autoforge import __protocol_version__
from autoforge.config import default_config
from autoforge.config_fingerprint import ConfigFingerprint, RecordedConfiguration
from autoforge.engine import ControllerEngine
from autoforge.errors import ConfigurationError, StateError
from autoforge.result_parser import FINDING_ID_RE
from autoforge.runlog import RUN_ID_RE
from autoforge.state import AutoForgeState, load_state, save_state
from autoforge.transitions import Phase, WorkflowMode
from autoforge.validation import parse_pr_url

from .conftest import EPIC, ISSUE, MERGE_BASE, PR, SHA_A, make_engine

# =============================================================================
# A field the controller does not know
# =============================================================================


def test_a_remote_state_file_with_an_unknown_field_is_corruption(tmp_path, local_fields):
    """An unknown field is refused, never dropped; a newer protocol is named as such.

    Dropping a field the loader does not recognise would turn a state file
    written by a controller that knows about (say) a pending checkpoint into
    a valid-looking file without one. The protocol check runs first, so a
    file from a newer controller is reported by its protocol rather than as
    a pile of unknown fields. The loader never writes the file it refuses,
    and an engine's ``load`` (what ``resume``, ``step`` and ``status`` do)
    refuses it the same way.

    ADR 0009 A.2.3 B (#386): the REMOTE replacement of
    tests/test_local.py::test_a_state_file_with_an_unknown_field_is_corruption
    """
    eng = make_engine(tmp_path / ".autoforge")
    eng.save()
    eng.close()
    path = Path(eng.paths.state_file)
    payload = local_fields(json.loads(path.read_text(encoding="utf-8")))
    assert payload["mode"] == "REMOTE"

    bad = dict(payload, remote_future_checkpoint="something this controller ignores")
    raw = json.dumps(bad).encode("utf-8")
    path.write_bytes(raw)
    with pytest.raises(StateError, match="unknown field.*'remote_future_checkpoint'"):
        load_state(path)
    with pytest.raises(StateError, match="unknown field.*'remote_future_checkpoint'"):
        ControllerEngine(default_config(), state_dir=eng.paths.state_dir).load()
    assert path.read_bytes() == raw

    bad["protocol_version"] = "999.0"
    raw = json.dumps(bad).encode("utf-8")
    path.write_bytes(raw)
    with pytest.raises(StateError, match="protocol_version '999.0'") as refused:
        load_state(path)
    assert "unknown field" not in str(refused.value)
    with pytest.raises(StateError, match="protocol_version '999.0'"):
        ControllerEngine(default_config(), state_dir=eng.paths.state_dir).load()
    assert path.read_bytes() == raw


# =============================================================================
# A root that is not an object
# =============================================================================

NOT_AN_OBJECT = (
    "",
    "{",
    '{"phase": ',
    '{"phase": "REVIEW"\x00}',
    "\x00\x00",
    "[]",
    '["phase", "REVIEW"]',
    '"a string"',
    "null",
    "true",
    "0",
    "-1",
    "1.5",
)


@pytest.mark.parametrize("text", NOT_AN_OBJECT, ids=repr)
def test_a_remote_state_file_that_is_not_a_json_object_is_corruption_not_a_fresh_run(
    tmp_path, text
):
    """Truncated JSON and every root that is not an object are corruption, never a new run.

    The most likely real corruption is a truncated write. Read as "no run",
    it would let ``run`` start a second implementation of the issue beside
    the PR the first one opened. ``load_state``, an engine's ``load`` and the
    question ``run`` asks before it creates a run (``existing_run``) all
    refuse it as a corrupt file, never as a missing one, and none of them
    writes the file.

    ADR 0009 A.2.3 B (#386): the REMOTE replacement of
    tests/test_local_state_fuzz.py::test_a_state_file_that_is_not_json_is_corruption_not_a_fresh_run
    """
    eng = make_engine(tmp_path / ".autoforge")
    eng.save()
    eng.close()
    path = Path(eng.paths.state_file)
    path.write_text(text, encoding="utf-8")
    raw = path.read_bytes()
    corrupt = "corrupted state file|must contain a JSON object"
    with pytest.raises(StateError, match=corrupt):
        load_state(path)
    engine = ControllerEngine(default_config(), state_dir=eng.paths.state_dir)
    with pytest.raises(StateError, match=corrupt):
        engine.load()
    assert engine.state is None
    with pytest.raises(StateError, match=corrupt):
        engine.existing_run()
    engine.close()
    assert path.read_bytes() == raw


# =============================================================================
# Random corruption of a valid REMOTE state
# =============================================================================


def good_remote() -> dict:
    """A REMOTE run in FIX, as a save writes it: a PR, a bound review, one open finding."""
    state = AutoForgeState(
        run_id="af-20260101T000000Z-abc123",
        repository="owner/repo",
        epic_url=EPIC,
        current_issue_url=ISSUE,
        current_pr_url=PR,
        current_branch="autoforge/2",
        phase=Phase.FIX,
        review_round=1,
        reviewed_pr_url=PR,
        reviewed_head_sha=SHA_A,
        reviewed_base_ref="main",
        reviewed_merge_base_sha=MERGE_BASE,
        current_head_sha=SHA_A,
        current_base_ref="main",
        current_merge_base_sha=MERGE_BASE,
        last_review_comment_url=f"{PR}#issuecomment-7",
        last_review_result="needs_fix",
        last_review_needs_fix=True,
        open_findings=[
            {
                "id": "R1-F1",
                "classification": "non-blocked",
                "title": "missing test",
                "location": "tests/",
                "required_resolution": "add a regression test",
            }
        ],
        unblock_history=[
            {
                "at": "2026-01-01T00:00:00+00:00",
                "reason": "the operator checked the PR",
                "block_reason": "a check failed",
                "phase": "REVIEW",
                "detail": "the PR is open at the reviewed HEAD",
            }
        ],
        next_issue_rejections=["next issue .../issues/9 could not be verified"],
        verification_failures=["REVIEW: a check failed"],
        step_count=4,
        config_fingerprint=RecordedConfiguration(
            fingerprint=ConfigFingerprint.from_config(default_config(), "/srv/worktrees"),
            config_file="/home/me/autoforge.json",
            source="created",
            recorded_at="2026-01-01T00:00:00+00:00",
        ).to_dict(),
        sandbox_pin={
            "schema": 1,
            "mode": "required",
            "source": "created",
            "recorded_at": "2026-01-01T00:00:00+00:00",
        },
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    return state.to_dict()


def write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


_SHA40 = re.compile(r"[0-9a-f]{40}")


def holds_every_remote_invariant(s: AutoForgeState) -> None:
    """What a *loaded* REMOTE state is allowed to be. Nothing else may load.

    Only what :meth:`AutoForgeState.from_dict` itself guarantees: a phase
    whose meaning is the engine's to check (a replan stage, a merge binding)
    is not asserted here.
    """
    assert s.mode is WorkflowMode.REMOTE
    assert isinstance(s.phase, Phase)
    assert s.protocol_version == __protocol_version__
    # Identity: the run, its repository and its EPIC; the run id names a
    # directory under the state directory, so it is one safe component.
    assert isinstance(s.run_id, str) and RUN_ID_RE.match(s.run_id)
    assert s.run_id not in (".", "..") and "/" not in s.run_id
    for name in ("repository", "epic_url", "created_at", "updated_at"):
        assert isinstance(getattr(s, name), str) and getattr(s, name), name
    # Every plain str or int field the dataclass declares has its type; a
    # boolean is not an integer here.
    for f in fields(AutoForgeState):
        if f.type in ("str", "int"):
            value = getattr(s, f.name)
            expected = {"str": str, "int": int}[f.type]
            assert isinstance(value, expected) and not isinstance(value, bool), f.name
    # The counters that bound the run are never negative, and attempt zero
    # of an implementation never happened.
    for name in (
        "review_round",
        "step_count",
        "attempt",
        "merged_since_epic_update",
        "escalation_count",
    ):
        assert getattr(s, name) >= 0, name
    assert s.execution_attempt >= 1
    # Lists and objects have their shapes.
    for name in ("open_findings", "prior_findings"):
        findings = getattr(s, name)
        assert isinstance(findings, list), name
        for finding in findings:
            assert isinstance(finding, dict), name
            assert isinstance(finding.get("id"), str) and FINDING_ID_RE.match(finding["id"])
    for name in ("counted_merged_prs", "next_issue_rejections", "verification_failures"):
        value = getattr(s, name)
        assert isinstance(value, list) and all(isinstance(v, str) for v in value), name
    for name in ("last_fix_resolutions", "superseded_prs", "unblock_history"):
        value = getattr(s, name)
        assert isinstance(value, list) and all(isinstance(v, dict) for v in value), name
    assert isinstance(s.premerge_verified_commands, list)
    assert all(
        isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        for argv in s.premerge_verified_commands
    )
    for name in ("replan_transaction", "config_fingerprint", "sandbox_pin"):
        assert isinstance(getattr(s, name), dict), name
    assert s.last_review_needs_fix is None or isinstance(s.last_review_needs_fix, bool)
    for entry in s.unblock_history:
        for key in ("at", "reason", "block_reason", "phase", "detail"):
            assert isinstance(entry.get(key), str), key
        assert entry["at"] and Phase(entry["phase"])
    # The review binding the merge gate compares by identity.
    for name in ("reviewed_merge_base_sha", "current_merge_base_sha"):
        assert getattr(s, name) == "" or _SHA40.fullmatch(getattr(s, name)), name
    if s.reviewed_pr_url:
        try:
            reviewed = parse_pr_url(s.reviewed_pr_url)
        except ConfigurationError as exc:  # pragma: no cover - the assertion's message
            raise AssertionError(f"reviewed_pr_url does not parse: {exc}") from None
        assert reviewed.same_repository(s.repository)
    # A REMOTE run carries no LOCAL checkpoint and no LOCAL contract (read
    # with a default: the fields leave the state when LOCAL does, #388).
    assert getattr(s, "local_run_contract", {}) == {}
    assert getattr(s, "local_pending_phase", "") == ""
    assert getattr(s, "local_pending_fingerprint", "") == ""
    assert getattr(s, "local_pending_attempts", 0) == 0
    # The checks that run on the parsed evidence and effect state still pass.
    s.validation_evidence()
    s.phase_effects()
    # A state that loaded is one its own save loads again, unchanged.
    again = AutoForgeState.from_dict(json.loads(json.dumps(s.to_dict())))
    assert again.to_dict() == s.to_dict()


def test_the_remote_baseline_state_loads_and_round_trips(tmp_path):
    """The property below is only meaningful if the unmutated file is accepted.

    ADR 0009 A.2.3 B (#386): the REMOTE baseline of
    tests/test_local_state_fuzz.py::test_random_corruption_either_fails_or_is_legal
    """
    path = write(tmp_path / "state.json", good_remote())
    loaded = load_state(path)
    holds_every_remote_invariant(loaded)
    assert loaded.phase == Phase.FIX and [f["id"] for f in loaded.open_findings] == ["R1-F1"]
    save_state(loaded, tmp_path / "again.json")
    assert load_state(tmp_path / "again.json").to_dict() == loaded.to_dict()


JUNK = [
    None,
    True,
    False,
    -1,
    0,
    1,
    2**63,
    1.5,
    "",
    " ",
    "../escape",
    "/etc",
    "NaN",
    "REMOTE",
    "LOCAL",
    "ANALYZE_EXECUTE",
    "MERGE",
    "R1-F1",
    "owner/other",
    "https://github.com/owner/other/pull/1",
    "a" * 40,
    "A" * 40,
    [],
    ["x"],
    [{}],
    [{"id": "R1-F1"}],
    [{"id": "F1"}],
    [[]],
    {},
    {"a": 1},
    "\x00",
    "d" * 64,
]


@pytest.mark.parametrize("seed", range(200))
def test_random_corruption_of_a_remote_state_either_fails_or_is_legal(tmp_path, seed):
    """No mutation of a valid REMOTE file may load as a state that breaks an invariant.

    It is not that some listed corruptions are refused, but that a *loaded*
    state is legal, whatever arrived on disk. A mutation that happens to be
    harmless (another branch name, one more step) may load; it just has to
    load as something the invariants accept, and as what the file says:
    nothing in it was dropped or rewritten to make it load.

    ADR 0009 A.2.3 B (#386): the REMOTE replacement of
    tests/test_local_state_fuzz.py::test_random_corruption_either_fails_or_is_legal
    """
    rng = random.Random(seed)
    payload = good_remote()
    for _ in range(rng.randint(1, 3)):
        action = rng.choice(("set", "delete", "add"))
        if action == "delete" and payload:
            del payload[rng.choice(sorted(payload))]
        elif action == "add":
            payload[f"field_{rng.randrange(1000)}"] = rng.choice(JUNK)
        else:
            payload[rng.choice(sorted(payload))] = rng.choice(JUNK)

    path = write(tmp_path / "state.json", payload)
    raw = path.read_bytes()
    try:
        state = load_state(path)
    except StateError:
        assert path.read_bytes() == raw, "a refusal never writes the file"
        return  # a refusal is always an acceptable answer
    holds_every_remote_invariant(state)
    # Every field of the file is in the state as the file holds it: no
    # mutation here labels an older protocol, so nothing is migrated.
    loaded = state.to_dict()
    assert {k: loaded.get(k, "(dropped)") for k in payload} == payload


# =============================================================================
# A LOCAL checkpoint on a REMOTE state
# =============================================================================

# Every checkpoint LOCAL's loader accepted on a LOCAL run: a write phase's,
# under that phase or kept as evidence by BLOCKED or FAILED.
WELL_FORMED_CHECKPOINTS = (
    (Phase.FIX, Phase.FIX),
    (Phase.ANALYZE_EXECUTE, Phase.ANALYZE_EXECUTE),
    (Phase.FIX, Phase.BLOCKED),
    (Phase.ANALYZE_EXECUTE, Phase.FAILED),
)


@pytest.mark.parametrize(
    ("pending", "phase"), WELL_FORMED_CHECKPOINTS, ids=lambda p: p.value.lower()
)
def test_a_remote_state_carrying_a_well_formed_local_checkpoint_is_refused(
    tmp_path, pending, phase
):
    """A REMOTE run has no working-tree checkpoint, so one on its file is corruption.

    The file is :func:`good_remote` in ``phase``, which loads, with a
    checkpoint that is complete and consistent with that phase: only the
    REMOTE-mode guard of the checkpoint refuses it, and the refusal names it.
    ``load_state``, an engine's ``load`` and ``existing_run`` all refuse it,
    and none of them writes the file. The random mutations above never build
    such a checkpoint, so without this test they pass with the guard gone.

    #387 (R2-F2): the REMOTE row of the deleted
    tests/test_local.py::test_a_state_file_with_a_malformed_pending_checkpoint_is_refused
    """
    payload = dict(good_remote(), phase=phase.value)
    if phase is Phase.BLOCKED or phase is Phase.FAILED:
        payload["block_reason"] = "the run stopped on an unverified launch"
    state_dir = tmp_path / ".autoforge"
    state_dir.mkdir()
    path = write(state_dir / "state.json", payload)
    holds_every_remote_invariant(load_state(path))
    payload.update(
        local_pending_phase=pending.value,
        local_pending_fingerprint="a" * 64,
        local_pending_attempts=1,
    )
    path = write(path, payload)
    raw = path.read_bytes()
    refusal = re.escape(
        f"state field 'local_pending_phase' is set ({pending.value!r}) on a REMOTE run, "
        "which has no local working-tree checkpoint"
    )
    with pytest.raises(StateError, match=refusal):
        load_state(path)
    engine = ControllerEngine(default_config(), state_dir=state_dir)
    with pytest.raises(StateError, match=refusal):
        engine.load()
    assert engine.state is None
    with pytest.raises(StateError, match=refusal):
        engine.existing_run()
    engine.close()
    assert path.read_bytes() == raw
