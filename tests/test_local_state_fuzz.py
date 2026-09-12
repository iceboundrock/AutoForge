"""The deserialize boundary as the place illegal state stops existing.

Persisted state is the one input to the controller that no live actor
produced: it arrives from a disk that a crash, a hand edit, a truncated
merge, a partial restore or an older controller may have touched. Everything
downstream -- which phase runs, whether a write-capable agent is launched
again, which fingerprint a review is bound to, whether a bound is enforced at
all -- reads it as fact.

So the invariant is: **a state object that exists satisfies every LOCAL
invariant.** There is no "loaded but questionable" state to check for later,
because `load_state` either produces a legal one or raises. The tests below
try to manufacture a counter-example, field by field and then at random.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from autoforge.errors import StateError
from autoforge.state import AutoForgeState, load_state, save_state, utcnow_iso
from autoforge.transitions import LOCAL_PHASES, LOCAL_WRITE_PHASES, Phase, WorkflowMode

from .conftest import sample_contract


def good_local() -> dict:
    state = AutoForgeState(
        run_id="20260101-000000-abcdef",
        mode=WorkflowMode.LOCAL,
        phase=Phase.REVIEW,
        feature_spec_path="features/add-filter.md",
        feature_spec_sha256="a" * 64,
        local_run_contract=sample_contract(),
        workspace_fingerprint="b" * 64,
        base_head_sha="c" * 40,
        base_branch="main",
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    return state.to_dict()


def write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def holds_every_invariant(s: AutoForgeState) -> None:
    """What a *loaded* LOCAL state is allowed to be. Nothing else may load."""
    assert s.run_id and "/" not in s.run_id and s.run_id not in (".", "..")
    assert s.created_at and s.updated_at
    if s.mode == WorkflowMode.LOCAL:
        assert s.phase in LOCAL_PHASES
        assert s.feature_spec_path and s.feature_spec_sha256
        assert s.local_run_contract
        s.local_contract()  # parses strictly, or the state would not exist
        if s.local_pending_phase:
            assert Phase(s.local_pending_phase) in LOCAL_WRITE_PHASES
            assert s.local_pending_fingerprint
            assert s.local_pending_attempts >= 1
        else:
            assert not s.local_pending_fingerprint
            assert s.local_pending_attempts == 0
    for counter in (
        s.review_round,
        s.step_count,
        s.attempt,
        s.local_fix_rounds,
        s.local_pending_attempts,
    ):
        assert isinstance(counter, int) and counter >= 0


def test_the_baseline_state_loads(tmp_path):
    """The matrix is only meaningful if the unmutated row is accepted."""
    path = write(tmp_path / "state.json", good_local())
    holds_every_invariant(load_state(path))


# -- one illegal combination per row -------------------------------------------
# Each entry is an override applied to a valid LOCAL state. None of them may
# load. They are grouped by what the corruption would buy an attacker or a
# careless hand edit -- because that, not the field name, is why each is
# refused.
ILLEGAL = {
    # Disabling a bound by removing the record that enforces it.
    "checkpoint without its phase": {"local_pending_fingerprint": "d" * 64},
    "checkpoint without its attempts": {
        "local_pending_phase": "ANALYZE_EXECUTE",
        "local_pending_fingerprint": "",
        "local_pending_attempts": 1,
    },
    "checkpoint with zero attempts": {
        "local_pending_phase": "FIX",
        "local_pending_fingerprint": "d" * 64,
        "local_pending_attempts": 0,
    },
    "attempts without a checkpoint": {"local_pending_attempts": 2},
    "negative attempts": {
        "local_pending_phase": "FIX",
        "local_pending_fingerprint": "d" * 64,
        "local_pending_attempts": -1,
    },
    "negative review round": {"review_round": -1},
    "negative fix rounds": {"local_fix_rounds": -1},
    "negative step count": {"step_count": -1},
    # Claiming a checkpoint for a phase that cannot write.
    "checkpoint on a read-only phase": {
        "local_pending_phase": "REVIEW",
        "local_pending_fingerprint": "d" * 64,
        "local_pending_attempts": 1,
    },
    "checkpoint on a phase that does not exist": {
        "local_pending_phase": "IMPLEMENTING",
        "local_pending_fingerprint": "d" * 64,
        "local_pending_attempts": 1,
    },
    "checkpoint on a remote run": {
        "mode": "REMOTE",
        "repository": "owner/repo",
        "epic_url": "https://github.com/owner/repo/issues/1",
        "phase": "ANALYZE_EXECUTE",
        "local_pending_phase": "ANALYZE_EXECUTE",
        "local_pending_fingerprint": "d" * 64,
        "local_pending_attempts": 1,
    },
    # Reaching a topology the mode does not have.
    "local run in a github phase": {"phase": "READY_FOR_MERGE"},
    "local run in the merge phase": {"phase": "MERGE"},
    "local run in a replan": {"phase": "REPLAN_REEXECUTE"},
    "a phase that is not a phase": {"phase": "ASCENDED"},
    "a mode that is not a mode": {"mode": "SEMI_LOCAL"},
    # Losing the run's identity, which is what binds it to a feature.
    "no feature specification": {"feature_spec_path": ""},
    "no specification hash": {"feature_spec_sha256": ""},
    # Losing the run contract the run was defined under: without it a resume
    # cannot tell that `local.exclude` (or anything else) moved the run, and
    # the controller must never rebuild it from the configuration of the day.
    "no run contract": {"local_run_contract": {}},
    "a list where the run contract belongs": {"local_run_contract": ["v1"]},
    "a pre-release policy string beside the contract": {
        "local_workspace_policy": "v1 exclude=[] max_entries=50000 max_bytes=536870912"
    },
    "a contract missing a field": {
        "local_run_contract": {k: v for k, v in sample_contract().items() if k != "max_fix_rounds"}
    },
    "a contract with a field from another controller": {
        "local_run_contract": {**sample_contract(), "max_total_steps": 300}
    },
    "a contract from another schema": {"local_run_contract": {**sample_contract(), "schema": 2}},
    "a contract whose policy digest lies": {
        "local_run_contract": {
            **sample_contract(),
            "workspace_policy": {**sample_contract()["workspace_policy"], "digest": "0" * 64},
        }
    },
    "a contract whose policy is the pre-release text": {
        "local_run_contract": {
            **sample_contract(),
            "workspace_policy": {
                "version": "v1",
                "policy": "v1 exclude=[] max_entries=50000 max_bytes=536870912",
                "digest": "0" * 64,
            },
        }
    },
    "a contract with a negative fix budget": {
        "local_run_contract": {**sample_contract(), "max_fix_rounds": -1}
    },
    "a contract with a boolean fix budget": {
        "local_run_contract": {**sample_contract(), "max_fix_rounds": True}
    },
    "a contract whose validation commands are strings": {
        "local_run_contract": {**sample_contract(), "validation_commands": ["pytest -q"]}
    },
    "a contract whose validation command is empty": {
        "local_run_contract": {**sample_contract(), "validation_commands": [[]]}
    },
    "a contract with an empty repository root": {
        "local_run_contract": {**sample_contract(), "repository_root": ""}
    },
    "a contract with a null state root": {
        "local_run_contract": {**sample_contract(), "state_root": None}
    },
    "no run id": {"run_id": ""},
    "a run id that is a path": {"run_id": "../elsewhere"},
    "a run id that is absolute": {"run_id": "/etc"},
    # Types that would survive as something else entirely.
    "a string where a counter belongs": {"review_round": "1"},
    "a list where a string belongs": {"feature_spec_path": ["features/a.md"]},
    "null where a string belongs": {"workspace_fingerprint": None},
    "findings that are not a list": {"open_findings": {"id": "R1-F1"}},
    # Fields from a controller this one is not.
    "a field this protocol does not define": {"local_pending_digest": "x"},
    "a protocol from the future": {"protocol_version": "999.0"},
}


@pytest.mark.parametrize("name", sorted(ILLEGAL), ids=lambda n: n.replace(" ", "-"))
def test_an_illegal_local_state_never_loads(tmp_path, name):
    path = write(tmp_path / "state.json", {**good_local(), **ILLEGAL[name]})
    with pytest.raises(StateError) as first:
        load_state(path)
    # Deterministic: the same bytes fail the same way, so an operator who
    # re-runs after reading the message sees the message again rather than a
    # different one -- and a retry loop can never wear the refusal down.
    with pytest.raises(StateError) as second:
        load_state(path)
    assert str(first.value) == str(second.value)
    assert str(first.value).strip(), "a refusal must say something"


def test_every_illegal_row_is_actually_a_mutation(tmp_path):
    """Guard against a row that silently matches the baseline and proves nothing."""
    base = good_local()

    def differs(a, b) -> bool:
        # `True == 1` in Python; a boolean where an int belongs *is* a mutation.
        if isinstance(a, dict) and isinstance(b, dict):
            return set(a) != set(b) or any(differs(a[k], b[k]) for k in a)
        return a != b or type(a) is not type(b)

    for name, override in ILLEGAL.items():
        assert any(differs(base.get(k), v) for k, v in override.items()), f"{name} changes nothing"


# -- the same question asked at random ------------------------------------------
JUNK = [
    None,
    True,
    False,
    -1,
    0,
    1,
    2**63,
    "",
    " ",
    "../escape",
    "NaN",
    "ANALYZE_EXECUTE",
    "MERGE",
    [],
    ["x"],
    {},
    {"a": 1},
    "\x00",
    "d" * 64,
]


@pytest.mark.parametrize("seed", range(200))
def test_random_corruption_either_fails_or_is_legal(tmp_path, seed):
    """No mutation may produce a state object that breaks a LOCAL invariant.

    This is the property the per-row matrix above is a readable sample of: it
    is not that the listed corruptions are refused, but that a *loaded* state
    is legal, whatever arrived on disk. A mutation that happens to be
    harmless (a different branch name, one more step) is allowed to load --
    it just has to load as something the invariants accept.
    """
    rng = random.Random(seed)
    payload = good_local()
    for _ in range(rng.randint(1, 3)):
        action = rng.choice(("set", "delete", "add"))
        if action == "delete" and payload:
            del payload[rng.choice(sorted(payload))]
        elif action == "add":
            payload[f"field_{rng.randrange(1000)}"] = rng.choice(JUNK)
        else:
            payload[rng.choice(sorted(payload))] = rng.choice(JUNK)

    path = write(tmp_path / "state.json", payload)
    try:
        state = load_state(path)
    except StateError:
        return  # a refusal is always an acceptable answer
    holds_every_invariant(state)


def test_a_state_file_that_is_not_json_is_corruption_not_a_fresh_run(tmp_path):
    """The most likely real corruption: a truncated write from an older scheme.

    Losing it silently would start a second implementation attempt against a
    tree that already holds the first one's work.
    """
    for text in ("", "{", '{"phase": ', "\x00\x00", "[]", '"a string"', "null"):
        path = write(tmp_path / "state.json", {})
        path.write_text(text, encoding="utf-8")
        with pytest.raises(StateError):
            load_state(path)


def test_a_round_trip_through_disk_preserves_every_field(tmp_path):
    """Validation that rejects illegal state is useless if it mangles legal state."""
    payload = good_local()
    payload.update(
        {
            "phase": "FIX",
            "review_round": 3,
            "local_fix_rounds": 2,
            "step_count": 11,
            "local_pending_phase": "FIX",
            "local_pending_fingerprint": "e" * 64,
            "local_pending_attempts": 2,
            "open_findings": [{"id": "R3-F1", "required_resolution": "do the thing"}],
        }
    )
    path = write(tmp_path / "state.json", payload)
    loaded = load_state(path)
    holds_every_invariant(loaded)
    save_state(loaded, tmp_path / "again.json")
    again = load_state(tmp_path / "again.json")
    assert again.to_dict() == loaded.to_dict()
