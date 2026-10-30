"""State: serialize/deserialize, atomic save/load, corruption, idempotency."""

import errno
import json

import pytest

from autoforge.claims import (
    REVIEW,
    render_follow_up_marker,
    render_progress_marker,
    render_review_marker,
)
from autoforge.effects import (
    LABEL_AGENT_PUBLISHES,
    LABEL_CONTROLLER_PUBLISHES,
    LABEL_NONE,
    MAX_CONFLICT_REASON_CHARS,
    MAX_EFFECT_STATE_CHARS,
    MAX_EFFECTS_PER_PLAN,
    EffectKind,
    EffectOwner,
    EffectRecord,
    FixContext,
    ReplanContext,
    ReviewContext,
    Stage,
    UpdateEpicContext,
    compose_append,
    follow_up_issue_body,
    payload_chars,
    progress_comment_body,
    replacement_pr_body,
    review_comment_body,
    sha256_text,
)
from autoforge.errors import StateError
from autoforge.replan_txn import ReplanAttestation, render_marker
from autoforge.result_parser import MAX_OPERATOR_ACTIONS_PER_FIX, Finding, ReviewResult
from autoforge.state import (
    MAX_ACCEPTANCE_OUTCOMES,
    MAX_DECLINES_PER_ROUND,
    MAX_OPERATOR_COMMENT_OUTCOMES,
    MIN_OPERATOR_COMMENT_OUTCOMES,
    OPERATOR_COMMENT_OUTCOMES,
    AutoForgeState,
    StatePaths,
    load_state,
    save_state,
)
from autoforge.transitions import Phase, WorkflowMode

from .conftest import DECODER_LIMIT_RECORDS, LOCAL_STATE_FIELDS, sample_contract

# Protocol 10's own fields (#172): no older label wrote any of them.
_VALIDATION_KEYS = (
    "validation_records",
    "validation_attempt",
    "review_evidence",
    "review_certificate",
    "producer_validation_failure",
    "validation_override_seq",
)


def _drop_validation(d: dict) -> dict:
    """``d`` without protocol 10's fields, as an older controller wrote it."""
    for key in _VALIDATION_KEYS:
        del d[key]
    return d


# Protocol 11's own field (#243): no older label wrote it.
_OPERATOR_KEYS = ("operator_comment_outcomes",)


def _drop_operator(d: dict) -> dict:
    """``d`` without protocol 11's operator-action state, as an older controller wrote it.

    The field itself, and the keys protocol 11 added inside older fields: a
    FIX entry observation's ``operator_targets``, a FIX context resolution's
    ``operator_sources`` and a REMOTE ``last_fix_resolutions`` entry's
    ``operator_evidence``.
    """
    for key in _OPERATOR_KEYS:
        del d[key]
    _drop_pin(d)  # no label before protocol 12 wrote it either (#280)
    observation = d.get("entry_observation")
    if isinstance(observation, dict):
        observation.pop("operator_targets", None)
    context = d.get("completion_context")
    for r in context.get("resolutions", []) if isinstance(context, dict) else ():
        r.pop("operator_sources", None)
    for r in d.get("last_fix_resolutions", []):
        r.pop("operator_evidence", None)
    return d


# Protocol 12's own field (#280): no older label wrote it.
_PIN_KEYS = ("sandbox_pin",)


def _drop_pin(d: dict) -> dict:
    """``d`` without protocol 12's sandbox pin, as an older controller wrote it."""
    for key in _PIN_KEYS:
        d.pop(key, None)
    return d


def make_state(**kw):
    base = dict(
        run_id="af-test-1",
        repository="owner/repo",
        epic_url="https://github.com/owner/repo/issues/1",
        current_issue_url="https://github.com/owner/repo/issues/2",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    base.update(kw)
    return AutoForgeState(**base)


def test_roundtrip_serialize_deserialize():
    s = make_state(
        phase=Phase.REVIEW,
        review_round=3,
        current_pr_url="https://github.com/owner/repo/pull/42",
        next_issue_rejections=["next issue .../issues/999 could not be verified"],
    )
    d = s.to_dict()
    assert d["phase"] == "REVIEW"
    back = AutoForgeState.from_dict(json.loads(json.dumps(d)))
    assert back == s


def test_save_load_atomic(tmp_path):
    p = tmp_path / ".autoforge" / "state.json"
    s = make_state()
    save_state(s, p)
    assert p.exists()
    loaded = load_state(p)
    assert loaded.run_id == "af-test-1"
    assert loaded.phase == Phase.INITIALIZING
    # no temp files left behind
    assert list(p.parent.glob("*.tmp")) == []


def test_save_overwrites_atomically(tmp_path):
    p = tmp_path / "state.json"
    save_state(make_state(review_round=0), p)
    save_state(make_state(review_round=5), p)
    assert load_state(p).review_round == 5


def test_load_missing_raises_helpful_error(tmp_path):
    with pytest.raises(StateError, match="no state file"):
        load_state(tmp_path / "nope" / "state.json")


def _saved_state_bytes(local_fields) -> bytes:
    """A whole REMOTE state file as a save writes it, shaped by ``local_fields``."""
    return json.dumps(local_fields(make_state(phase=Phase.REVIEW).to_dict())).encode("utf-8")


def test_load_corrupted_json_raises_and_does_not_clobber(tmp_path, local_fields):
    p = tmp_path / "state.json"
    torn = _saved_state_bytes(local_fields)[:-1]  # a whole state, one byte short
    for raw in (b'{"phase": "REVIEW", "run_id": ', torn):
        p.write_bytes(raw)
        with pytest.raises(StateError, match="[Cc]orrupt"):
            load_state(p)
        # file untouched — loader never writes
        assert p.read_bytes() == raw


def test_load_invalid_utf8_raises_state_error_and_does_not_clobber(tmp_path, local_fields):
    """Invalid UTF-8 is corruption, not a read error: it must surface as StateError."""
    p = tmp_path / "state.json"
    whole = _saved_state_bytes(local_fields)
    assert b'"af-test-1"' in whole
    for raw in (b'\xff\xfe{"phase": "REVIEW"}', whole.replace(b'"af-test-1"', b'"af-\xfftest-1"')):
        p.write_bytes(raw)
        with pytest.raises(StateError, match="[Cc]orrupt.*UTF-8"):
            load_state(p)
        assert p.read_bytes() == raw


def _past_the_decoders_limits(local_fields) -> dict[str, bytes]:
    """:data:`DECODER_LIMIT_RECORDS` again, each inside a whole state file."""
    whole = _saved_state_bytes(local_fields)
    assert whole.endswith(b"}")
    return {
        name: whole[:-1] + b', "n": ' + record[len(b'{"type": "x", "n": ') :]
        for name, record in DECODER_LIMIT_RECORDS.items()
    }


@pytest.mark.parametrize("name", DECODER_LIMIT_RECORDS.keys())
def test_load_past_the_decoders_limits_is_corrupt_state_and_does_not_clobber(
    tmp_path, name, local_fields
):
    """ValueError and RecursionError from the decoder are corruption too, never an escape."""
    p = tmp_path / "state.json"
    for raw in (DECODER_LIMIT_RECORDS[name], _past_the_decoders_limits(local_fields)[name]):
        p.write_bytes(raw)
        with pytest.raises(StateError, match="[Cc]orrupt.*integer or nesting limit") as info:
            load_state(p)
        assert len(str(info.value)) < len(str(p)) + 200
        assert p.read_bytes() == raw


def test_load_unknown_phase_raises(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"phase": "TELEPORT", "run_id": "x"}), encoding="utf-8")
    with pytest.raises(StateError, match="[Uu]nknown phase"):
        load_state(p)


def test_load_missing_required_field_raises(tmp_path):
    p = tmp_path / "state.json"
    # The effect fields every current-protocol save writes, at their empty values.
    effects = {
        "effect_records": [],
        "entry_observation": {},
        "completion_context": {},
        "launch_label": "",
        "acceptance_outcomes": [],
        "config_fingerprint": {},
        "validation_records": [],
        "validation_attempt": {},
        "review_evidence": {},
        "review_certificate": {},
        "producer_validation_failure": {},
        "validation_override_seq": None,
        "operator_comment_outcomes": [],
        "sandbox_pin": {},
    }
    p.write_text(json.dumps({"phase": "REVIEW", **effects}), encoding="utf-8")
    with pytest.raises(StateError, match="run_id"):
        load_state(p)


def test_load_rejects_non_list_next_issue_rejections(tmp_path):
    p = tmp_path / "state.json"
    d = make_state().to_dict()
    d["next_issue_rejections"] = "oops"
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(StateError, match="next_issue_rejections"):
        load_state(p)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("counted_merged_prs", [1]),
        ("open_findings", [1]),
        ("open_findings", [{"classification": "nit"}]),
        ("open_findings", [{"id": 1}]),
        ("open_findings", [{"id": "R1-F1\n"}]),
        ("open_findings", [{"id": "F1"}]),
        ("prior_findings", [1]),
        ("prior_findings", [{"classification": "nit"}]),
        ("prior_findings", [{"id": 1}]),
        ("prior_findings", [{"id": "R1-F1\n"}]),
        ("prior_findings", [{"id": "F1"}]),
        ("last_fix_resolutions", [1]),
        ("next_issue_rejections", [1]),
        ("superseded_prs", [1]),
        ("premerge_verified_commands", ["make check"]),
        ("premerge_verified_commands", [["make", 1]]),
        ("premerge_verified_commands", "make check"),
        ("unblock_history", "no"),
        ("unblock_history", [1]),
        ("unblock_history", [{"at": "x"}]),
        (
            "unblock_history",
            [{"at": "", "reason": "r", "block_reason": "b", "phase": "REVIEW", "detail": "d"}],
        ),
        (
            "unblock_history",
            [{"at": "t", "reason": 1, "block_reason": "b", "phase": "REVIEW", "detail": "d"}],
        ),
        (
            "unblock_history",
            [{"at": "t", "reason": "r", "block_reason": "b", "phase": "NOPE", "detail": "d"}],
        ),
    ],
)
def test_load_rejects_wrong_list_element_types(tmp_path, field, bad):
    p = tmp_path / "state.json"
    data = make_state().to_dict()
    data[field] = bad
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match=field):
        load_state(p)


def test_state_without_next_issue_rejections_loads_with_empty_list(tmp_path):
    p = tmp_path / "state.json"
    d = make_state().to_dict()
    del d["next_issue_rejections"]
    p.write_text(json.dumps(d), encoding="utf-8")
    assert load_state(p).next_issue_rejections == []


def test_state_without_prior_findings_loads_with_empty_list(tmp_path):
    """A protocol-3 file written before the carry existed has nothing to
    re-check; it loads with the old behaviour, not as corruption."""
    p = tmp_path / "state.json"
    d = make_state().to_dict()
    del d["prior_findings"]
    p.write_text(json.dumps(d), encoding="utf-8")
    assert load_state(p).prior_findings == []


def test_state_without_unblock_history_loads_with_empty_list(tmp_path):
    """A file written before `unblock` existed was never unblocked."""
    p = tmp_path / "state.json"
    d = make_state().to_dict()
    del d["unblock_history"]
    p.write_text(json.dumps(d), encoding="utf-8")
    assert load_state(p).unblock_history == []


def test_unblock_history_roundtrips_and_survives_an_issue_switch(tmp_path):
    entry = {
        "at": "2026-09-19T10:00:00+00:00",
        "reason": "operator reran the flaky check",
        "block_reason": "check ci failed",
        "phase": "READY_FOR_MERGE",
        "detail": "PR at the clean-reviewed HEAD",
    }
    s = make_state(unblock_history=[entry])
    p = tmp_path / "state.json"
    save_state(s, p)
    assert load_state(p).unblock_history == [entry]
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert s.unblock_history == [entry]


def test_reset_for_new_issue_clears_the_carried_findings():
    s = make_state(prior_findings=[{"id": "R1-F1", "required_resolution": "x"}])
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert s.prior_findings == [] and s.open_findings == []


def test_reset_for_new_issue_clears_next_issue_rejections():
    s = make_state(next_issue_rejections=["rejected"])
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert s.next_issue_rejections == []


def test_timestamps_touch_on_save(tmp_path):
    p = tmp_path / "state.json"
    s = make_state(updated_at="2000-01-01T00:00:00+00:00")
    save_state(s, p)
    assert load_state(p).updated_at != "2000-01-01T00:00:00+00:00"


def test_record_merge_idempotent():
    s = make_state()
    assert s.record_merge("https://github.com/owner/repo/pull/42") is True
    assert s.merged_since_epic_update == 1
    # retry after crash must not double-count
    assert s.record_merge("https://github.com/owner/repo/pull/42") is False
    assert s.merged_since_epic_update == 1
    assert s.record_merge("https://github.com/owner/repo/pull/43") is True
    assert s.merged_since_epic_update == 2
    s.record_epic_update()
    assert s.merged_since_epic_update == 0
    assert len(s.counted_merged_prs) == 2


def test_review_history_roundtrip_and_validation(tmp_path):
    hist = [
        {
            "round": 1,
            "reviewed_head_sha": "a" * 40,
            "result": "needs_fix",
            "finding_count": 1,
            "fingerprint": "abc",
        }
    ]
    s = make_state(review_history=hist, step_count=7)
    back = AutoForgeState.from_dict(json.loads(json.dumps(s.to_dict())))
    assert back.review_history == hist and back.step_count == 7
    p = tmp_path / "state.json"
    d = make_state().to_dict()
    del d["review_history"]  # state written before this field existed
    p.write_text(json.dumps(d), encoding="utf-8")
    assert load_state(p).review_history == []
    for field, bad in (("review_history", "oops"), ("step_count", "3"), ("review_round", True)):
        d = make_state().to_dict()
        d[field] = bad
        p.write_text(json.dumps(d), encoding="utf-8")
        with pytest.raises(StateError, match=field):
            load_state(p)


def test_load_rejects_malformed_review_history_entries(tmp_path):
    """A present but malformed entry is corruption, never "old controller" data."""
    p = tmp_path / "state.json"
    good = {
        "round": 1,
        "reviewed_head_sha": "a" * 40,
        "result": "needs_fix",
        "finding_count": 1,
        "fingerprint": "abc",
        "resolutions": ["d0"],
        "resolutions_truncated": False,
    }
    d = make_state(review_history=[good]).to_dict()
    p.write_text(json.dumps(d), encoding="utf-8")
    assert load_state(p).review_history == [good]
    # a *missing* resolutions key still loads (count-only stagnation rule)
    legacy = {k: v for k, v in good.items() if not k.startswith("resolutions")}
    p.write_text(json.dumps(make_state(review_history=[legacy]).to_dict()), encoding="utf-8")
    assert load_state(p).review_history == [legacy]
    for bad, match in (
        ({**good, "resolutions": "d0"}, "resolutions must be a list"),
        ({**good, "resolutions": {"d0": 1}}, "resolutions must be a list"),
        ({**good, "resolutions": [None]}, "non-empty digest strings"),
        ({**good, "resolutions": [""]}, "non-empty digest strings"),
        ({**good, "resolutions_truncated": "no"}, "resolutions_truncated must be a bool"),
        ({**good, "result": "done"}, "result must be one of"),
        ({**good, "round": "1"}, "round must be an integer"),
        ("not an entry", "must be an object"),
    ):
        p.write_text(json.dumps(make_state(review_history=[bad]).to_dict()), encoding="utf-8")
        with pytest.raises(StateError, match=match) as exc:
            load_state(p)
        assert "review_history" in str(exc.value)


def test_reset_for_new_issue_clears_review_history_but_keeps_step_budget():
    s = make_state(review_history=[{"round": 1}], review_round=1, step_count=9)
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert s.review_history == [] and s.review_round == 0
    assert s.step_count == 9  # cumulative budget survives the switch


def test_replan_state_defaults_and_reset_are_backward_compatible(tmp_path):
    s = make_state(
        execution_attempt=2,
        escalation_count=1,
        superseded_prs=[{"pr_url": "https://github.com/owner/repo/pull/42"}],
        replan_transaction={"stage": "prepared", "transaction_id": "a" * 32},
    )
    # An in-flight transaction belongs to the issue it was opened for: moving
    # to a new issue must not leave one behind for REPLAN_REEXECUTE to replay.
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert s.execution_attempt == 1 and s.escalation_count == 0
    assert s.superseded_prs == [] and s.replan_transaction == {}
    data = make_state().to_dict()
    for field in (
        "execution_attempt",
        "escalation_count",
        "superseded_prs",
        "replan_transaction",
        "verification_failures",
    ):
        del data[field]
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_state(path)
    assert (loaded.execution_attempt, loaded.escalation_count, loaded.superseded_prs) == (1, 0, [])
    assert loaded.replan_transaction == {}


def test_protocol_1_is_migrated_only_without_a_replan_in_flight(tmp_path, local_fields):
    """#66 R7-F1: the 1 -> 2 protocol change is confined to the replan journal.

    A protocol-1 file with an empty or terminal journal loads and is
    relabelled (to the current protocol; the 2 -> 3 rule of #68 is
    checked by ``test_protocol_2_is_migrated_only_outside_the_merge_phases``);
    one with a replan in flight is refused at the boundary with the
    transaction described; any other label is unsupported as before.
    """
    path = tmp_path / "state.json"
    base = make_state().to_dict()
    assert base["protocol_version"] == "12"
    local_fields(base)  # protocol 1 wrote none, some or all of LOCAL's (ADR 0009 D8)
    del base["acceptance_outcomes"]  # protocol 8's own field (#211)
    del base["config_fingerprint"]  # protocol 9's own field (#219)
    _drop_validation(base)  # protocol 10's own fields (#172)
    _drop_operator(base)  # protocol 11's own field (#243)
    for journal in ({}, {"stage": "rejected", "rejection_reason": "refused by verification"}):
        data = dict(base, protocol_version="1", replan_transaction=journal)
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = load_state(path)
        assert loaded.protocol_version == "12" and loaded.replan_transaction == journal
        save_state(loaded, path)
        assert json.loads(path.read_text())["protocol_version"] == "12"
    in_flight = {
        "stage": "prepared",
        "transaction_id": "a" * 32,
        "source_pr_url": "https://github.com/owner/repo/pull/42",
    }
    data = dict(base, protocol_version="1", replan_transaction=in_flight)
    data["controller_version"] = "0.1.0"
    raw = json.dumps(data)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(StateError) as info:
        load_state(path)
    message = str(info.value)
    assert "written by controller 0.1.0 under protocol_version '1'" in message
    assert "stage 'prepared'" in message and "pull/42" in message
    assert "replacement PR (none)" in message
    assert "corrupt" not in message
    assert path.read_text(encoding="utf-8") == raw
    data["protocol_version"] = "6"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="unsupported protocol_version '6'"):
        load_state(path)
    # The type check still owns a journal that is not an object, whatever
    # the label says.
    data["protocol_version"] = "1"
    data["replan_transaction"] = ["prepared"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="'replan_transaction' must be an object"):
        load_state(path)


PR42 = "https://github.com/owner/repo/pull/42"


def _clean_review_state(**kw):
    """A state whose clean review is bound the way the engine binds it (#68)."""
    return make_state(
        current_pr_url=PR42,
        current_head_sha="a" * 40,
        current_base_ref="main",
        reviewed_pr_url=PR42,
        reviewed_head_sha="a" * 40,
        reviewed_base_ref="main",
        reviewed_merge_base_sha="d" * 40,
        last_review_result="clean",
        review_round=2,
        **kw,
    )


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_protocol_2_is_refused_in_the_merge_phases(tmp_path, phase, local_fields):
    """#68: protocol 2 did not record which PR and base the clean review was
    posted on. A file parked before MERGE cannot be bound after the fact --
    filling the binding from ``current_pr_url`` would be exactly the trust
    the binding exists to remove -- so it is refused at the boundary, left
    unchanged, with the PR and HEAD named so the operator can look."""
    path = tmp_path / "state.json"
    data = local_fields(_clean_review_state(phase=phase).to_dict())
    data["protocol_version"] = "2"
    data["controller_version"] = "0.2.0"
    for missing in ("reviewed_pr_url", "reviewed_base_ref", "current_base_ref"):
        del data[missing]
    raw = json.dumps(data)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(StateError) as info:
        load_state(path)
    message = str(info.value)
    assert "written by controller 0.2.0 under protocol_version '2'" in message
    assert f"phase {phase.value}" in message and "pull/42" in message
    assert ("a" * 40) in message and "did not record which PR and base branch" in message
    assert "Nothing was merged or counted" in message and "corrupt" not in message
    assert path.read_text(encoding="utf-8") == raw
    # The same rule when the label is 1 (a 1 -> 3 jump): the journal rule
    # first, then the binding rule.
    data["protocol_version"] = "1"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="did not record which PR and base branch"):
        load_state(path)


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_protocol_1_is_refused_in_the_merge_phases(tmp_path, phase, local_fields):
    """ADR 0009 D8 (#385): protocol 1 recorded no more of the clean review's
    binding than protocol 2 did, so a protocol-1 file parked before MERGE is
    refused the same way, left unchanged, with the PR and HEAD named. The
    file is what protocol 1 wrote: none of the binding protocols 3 and 4
    added and none of the fields protocols 7 to 12 added. Its journal is
    empty, terminal or absent, so the journal rule lets it through and the
    binding rule decides."""
    path = tmp_path / "state.json"
    base = local_fields(_clean_review_state(phase=phase).to_dict())
    base["protocol_version"] = "1"
    base["controller_version"] = "0.1.0"
    for missing in (
        "reviewed_pr_url",
        "reviewed_base_ref",
        "current_base_ref",
        "reviewed_merge_base_sha",
        "current_merge_base_sha",
        *_EFFECT_KEYS,
        "acceptance_outcomes",
        "config_fingerprint",
        *_VALIDATION_KEYS,
        *_OPERATOR_KEYS,
        *_PIN_KEYS,
    ):
        del base[missing]
    rejected = {"stage": "rejected", "rejection_reason": "refused by verification"}
    for journal in ({}, rejected, None):
        data = dict(base, replan_transaction=journal)
        if journal is None:
            del data["replan_transaction"]
        raw = json.dumps(data)
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(StateError) as info:
            load_state(path)
        message = str(info.value)
        assert "written by controller 0.1.0 under protocol_version '1'" in message
        assert f"phase {phase.value}" in message and "pull/42" in message
        assert ("a" * 40) in message
        assert "protocol '1' did not record which PR and base branch that review was" in message
        assert "Nothing was merged or counted" in message and "corrupt" not in message
        assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    "phase",
    [p for p in Phase if p not in (Phase.READY_FOR_MERGE, Phase.MERGE)],
)
def test_protocol_2_is_relabelled_outside_the_merge_phases(tmp_path, phase):
    """Everywhere else the next review writes the binding, so a protocol-2
    file is a current file with an old label and an empty binding."""
    path = tmp_path / "state.json"
    data = _clean_review_state(phase=phase).to_dict()
    data["protocol_version"] = "2"
    for missing in (
        "reviewed_pr_url",
        "reviewed_base_ref",
        "current_base_ref",
        "reviewed_merge_base_sha",
        "current_merge_base_sha",
        "acceptance_outcomes",
        "config_fingerprint",
        *_VALIDATION_KEYS,
        *_OPERATOR_KEYS,
        *_PIN_KEYS,
    ):
        del data[missing]
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_state(path)
    assert loaded.protocol_version == "12" and loaded.phase == phase
    assert (loaded.reviewed_pr_url, loaded.reviewed_base_ref, loaded.current_base_ref) == (
        "",
        "",
        "",
    )
    assert (loaded.reviewed_merge_base_sha, loaded.current_merge_base_sha) == ("", "")
    assert loaded.reviewed_head_sha == "a" * 40  # what protocol 2 did record is kept
    save_state(loaded, path)
    assert json.loads(path.read_text())["protocol_version"] == "12"


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_protocol_3_is_refused_in_the_merge_phases(tmp_path, phase, local_fields):
    """#96: protocol 3 bound the clean review to its PR and base name but
    not to the merge base its diff was computed from. A file parked before
    MERGE cannot be bound after the fact -- reading the merge base now would
    bind the review to whatever the base is now, the rewrite the field
    exists to catch -- so it is refused at the boundary, left unchanged,
    with the PR and HEAD named. The journal rule applies first, as for
    every earlier protocol: a protocol-3 journal that is empty or rejected
    is not in flight, so the binding rule decides."""
    path = tmp_path / "state.json"
    data = local_fields(_clean_review_state(phase=phase).to_dict())
    data["protocol_version"] = "3"
    data["controller_version"] = "0.3.0"
    for missing in ("reviewed_merge_base_sha", "current_merge_base_sha"):
        del data[missing]
    raw = json.dumps(data)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(StateError) as info:
        load_state(path)
    message = str(info.value)
    assert "written by controller 0.3.0 under protocol_version '3'" in message
    assert f"phase {phase.value}" in message and "pull/42" in message
    assert ("a" * 40) in message and "did not record which merge base" in message
    assert "Nothing was merged or counted" in message and "corrupt" not in message
    assert path.read_text(encoding="utf-8") == raw
    data["replan_transaction"] = {"stage": "rejected", "rejection_reason": "refused"}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="did not record which merge base"):
        load_state(path)


@pytest.mark.parametrize(
    "phase",
    [p for p in Phase if p not in (Phase.READY_FOR_MERGE, Phase.MERGE)],
)
def test_protocol_3_is_relabelled_outside_the_merge_phases(tmp_path, phase):
    """#96: everywhere else the next review writes the merge base, so a
    protocol-3 file with no replan in flight is a current file with an
    old label, an empty merge base and the rest of the binding kept."""
    path = tmp_path / "state.json"
    rejected = {"stage": "rejected", "rejection_reason": "refused by verification"}
    for journal in ({}, rejected):
        data = _clean_review_state(phase=phase, replan_transaction=journal).to_dict()
        data["protocol_version"] = "3"
        for missing in (
            "reviewed_merge_base_sha",
            "current_merge_base_sha",
            "acceptance_outcomes",
            "config_fingerprint",
            *_VALIDATION_KEYS,
            *_OPERATOR_KEYS,
            *_PIN_KEYS,
        ):
            del data[missing]
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = load_state(path)
        assert loaded.protocol_version == "12" and loaded.phase == phase
        assert (loaded.reviewed_merge_base_sha, loaded.current_merge_base_sha) == ("", "")
        assert loaded.reviewed_pr_url == PR42 and loaded.reviewed_base_ref == "main"
        assert loaded.replan_transaction == journal
        save_state(loaded, path)
        assert json.loads(path.read_text())["protocol_version"] == "12"


@pytest.mark.parametrize("phase", list(Phase))
def test_protocol_3_is_refused_with_a_replan_in_flight(tmp_path, phase, local_fields):
    """#96: protocol 3 did not record the merge base the replan decision was
    bound to, so a protocol-3 file with a replan in flight is refused in
    every phase the way a protocol-1 or protocol-2 one is -- never migrated
    by reading the merge base GitHub reports now, and never handed to the
    journal loader to be called corrupt. The journal rule runs before the
    review-binding rule, so it decides even in the merge phases."""
    path = tmp_path / "state.json"
    in_flight = {
        "stage": "prepared",
        "transaction_id": "a" * 32,
        "source_pr_url": PR42,
    }
    data = local_fields(_clean_review_state(phase=phase, replan_transaction=in_flight).to_dict())
    data["protocol_version"] = "3"
    data["controller_version"] = "0.3.0"
    for missing in ("reviewed_merge_base_sha", "current_merge_base_sha"):
        del data[missing]
    raw = json.dumps(data)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(StateError) as info:
        load_state(path)
    message = str(info.value)
    assert "written by controller 0.3.0 under protocol_version '3'" in message
    assert "replan in flight" in message and "stage 'prepared'" in message
    assert "pull/42" in message and "replacement PR (none)" in message
    assert "did not record the merge base the review that decided the replan was bound to" in (
        message
    )
    assert "does not reconstruct it from the merge base GitHub reports now" in message
    assert "did not record which merge base" not in message
    assert "corrupt" not in message
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("phase", list(Phase))
def test_protocol_4_is_relabelled_in_every_phase_without_a_replan_past_the_write(tmp_path, phase):
    """#69: the 4 -> 5 step added the closed-event watermark and the attempt
    count to the replan close intent and changed nothing about the review
    binding, so a protocol-4 file is loaded in every phase -- the merge
    phases included -- when its journal is empty, terminal, or still before
    the write (a protocol-4 journal at PENDING, PREPARED or VERIFIED is
    byte-for-byte what protocol 5 writes there). The label is rewritten on
    the next save."""
    path = tmp_path / "state.json"
    rejected = {"stage": "rejected", "rejection_reason": "refused by verification"}
    in_flight_before_the_write = {
        "stage": "prepared",
        "transaction_id": "a" * 32,
        "issue_url": "https://github.com/owner/repo/issues/7",
        "decision_pr_url": PR42,
        "decision_head_sha": "a" * 40,
        "decision_branch": "autoforge/7",
        "decision_base_ref": "main",
        "decision_merge_base_sha": "b" * 40,
        "source_pr_url": PR42,
        "source_branch": "autoforge/7",
        "source_head_sha": "a" * 40,
        "source_base_ref": "main",
        "source_merge_base_sha": "b" * 40,
        "base_branch": "main",
        "evidence_finding_count": 1,
        "rendered_findings": "- R1-F1",
        "rendered_observations": "(none)",
        "rendered_verification_failures": "(none)",
        "preexisting_pr_urls": [PR42],
        "pr_number_watermark": 42,
        "expected_execution_attempt": 2,
        "escalation": {"trigger": "hard_review_round_threshold"},
    }
    for journal in ({}, rejected, in_flight_before_the_write):
        data = _clean_review_state(phase=phase, replan_transaction=journal).to_dict()
        data["protocol_version"] = "4"
        data["controller_version"] = "0.4.0"
        del data["acceptance_outcomes"]  # protocol 8's own field (#211)
        del data["config_fingerprint"]  # protocol 9's own field (#219)
        _drop_validation(data)  # protocol 10's own fields (#172)
        _drop_operator(data)  # protocol 11's own field (#243)
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = load_state(path)
        assert loaded.protocol_version == "12" and loaded.phase == phase
        assert loaded.replan_transaction == journal
        assert loaded.reviewed_merge_base_sha == "d" * 40  # the binding is kept whole
        save_state(loaded, path)
        assert json.loads(path.read_text())["protocol_version"] == "12"


@pytest.mark.parametrize("phase", list(Phase))
@pytest.mark.parametrize("stage", ["supersede_intent", "compensating", "superseded"])
def test_protocol_4_is_refused_with_a_replan_past_the_write(tmp_path, phase, stage, local_fields):
    """#69: protocol 4 did not record, with the close intent, the source PR's
    closed-event count or the number of close attempts, so a protocol-4 file
    whose replan has reached the write is refused in every phase -- never
    migrated by reading the count GitHub reports now (that would record the
    very close the watermark exists to detect as if it predated the intent),
    and never handed to the journal loader to be called corrupt."""
    path = tmp_path / "state.json"
    in_flight = {
        "stage": stage,
        "transaction_id": "a" * 32,
        "source_pr_url": PR42,
        "replacement_pr_url": "https://github.com/owner/repo/pull/43",
        "close_intent_at": "2026-01-01T00:00:00+00:00",
    }
    data = local_fields(_clean_review_state(phase=phase, replan_transaction=in_flight).to_dict())
    data["protocol_version"] = "4"
    data["controller_version"] = "0.4.0"
    raw = json.dumps(data)
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(StateError) as info:
        load_state(path)
    message = str(info.value)
    assert "written by controller 0.4.0 under protocol_version '4'" in message
    assert "replan in flight" in message and f"stage {stage!r}" in message
    assert "pull/42" in message and "pull/43" in message
    assert "did not record the source PR's closed-event count and the number of close" in message
    assert "does not reconstruct them from the events GitHub reports now" in message
    assert "merge base" not in message and "corrupt" not in message
    assert path.read_text(encoding="utf-8") == raw


def test_review_binding_round_trips_and_is_validated_on_load(tmp_path):
    path = tmp_path / "state.json"
    s = _clean_review_state(phase=Phase.READY_FOR_MERGE)
    save_state(s, path)
    assert load_state(path) == s
    data = s.to_dict()
    for bad, needle in (
        ("https://github.com/owner/repo/issues/42", "reviewed_pr_url"),
        ("not a url", "reviewed_pr_url"),
        (42, "must be str"),
        ("https://github.com/other/repo/pull/42", "is not in repository 'owner/repo'"),
    ):
        path.write_text(json.dumps(dict(data, reviewed_pr_url=bad)), encoding="utf-8")
        with pytest.raises(StateError, match=needle):
            load_state(path)
    for field in ("reviewed_base_ref", "current_base_ref"):
        path.write_text(json.dumps(dict(data, **{field: ["main"]})), encoding="utf-8")
        with pytest.raises(StateError, match=f"state field '{field}' must be str"):
            load_state(path)
    # The merge bases are compared to GitHub's lower-case full SHA (#96), so
    # any other shape could only ever differ from it; empty is "unbound".
    for field in ("reviewed_merge_base_sha", "current_merge_base_sha"):
        path.write_text(json.dumps(dict(data, **{field: ["d" * 40]})), encoding="utf-8")
        with pytest.raises(StateError, match=f"state field '{field}' must be str"):
            load_state(path)
        # R1-F2 of #111: a trailing control character is not "a full SHA"
        # either; ``re.match`` with ``$`` would have let the newline through.
        for bad in (
            "D" * 40,
            "d" * 39,
            "d" * 41,
            "g" * 40,
            "abc",
            "d" * 40 + "\n",
            "\n" + "d" * 40,
        ):
            path.write_text(json.dumps(dict(data, **{field: bad})), encoding="utf-8")
            with pytest.raises(StateError, match=f"state field '{field}' must be a full"):
                load_state(path)
        path.write_text(json.dumps(dict(data, **{field: ""})), encoding="utf-8")
        assert getattr(load_state(path), field) == ""
    # An equivalent spelling is the same PR; the repository check is by identity.
    spelled = dict(data, reviewed_pr_url="https://github.com/Owner/Repo/pull/42/")
    path.write_text(json.dumps(spelled), encoding="utf-8")
    assert load_state(path).reviewed_pr_url == "https://github.com/Owner/Repo/pull/42/"


def test_reset_for_new_issue_clears_the_review_binding():
    s = _clean_review_state(phase=Phase.UPDATE_EPIC)
    s.current_merge_base_sha = "d" * 40
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert (s.reviewed_pr_url, s.reviewed_head_sha, s.reviewed_base_ref) == ("", "", "")
    assert (s.current_pr_url, s.current_head_sha, s.current_base_ref) == ("", "", "")
    assert (s.reviewed_merge_base_sha, s.current_merge_base_sha) == ("", "")


def test_quarantine_state_file_renames_without_overwriting(tmp_path, monkeypatch):
    from datetime import datetime

    from autoforge import state as state_mod
    from autoforge.state import quarantine_state_file

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 6, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(state_mod, "datetime", FrozenDatetime)
    p = tmp_path / "state.json"
    p.write_text("garbage-1", encoding="utf-8")
    first = quarantine_state_file(p)
    assert first == tmp_path / "state.json.corrupt-20260906T120000Z"
    assert not p.exists() and first.read_text(encoding="utf-8") == "garbage-1"

    # same timestamp again: the earlier quarantined file is never overwritten
    p.write_text("garbage-2", encoding="utf-8")
    second = quarantine_state_file(p)
    assert second == tmp_path / "state.json.corrupt-20260906T120000Z.1"
    assert first.read_text(encoding="utf-8") == "garbage-1"
    assert second.read_text(encoding="utf-8") == "garbage-2"


def test_quarantine_missing_state_file_raises(tmp_path):
    from autoforge.state import quarantine_state_file

    with pytest.raises(StateError, match="cannot move"):
        quarantine_state_file(tmp_path / "state.json")


def test_quarantine_preserves_archive_created_after_candidate_selection(tmp_path, monkeypatch):
    """R2-F1: a destination that appears between candidate selection and the move is
    never overwritten; the move retries with the next numeric suffix."""
    import os
    from datetime import datetime

    from autoforge import state as state_mod
    from autoforge.state import quarantine_state_file

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 6, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(state_mod, "datetime", FrozenDatetime)
    p = tmp_path / "state.json"
    p.write_text("garbage-new", encoding="utf-8")
    expected_first = tmp_path / "state.json.corrupt-20260906T120000Z"
    assert not expected_first.exists()  # candidate selection would pick this name

    real_link = os.link
    attempts: list[str] = []

    def racing_link(src, dst, *args, **kwargs):
        # The reservation is made relative to an open directory descriptor, so
        # `dst` is a bare name; that is the point -- no pathname is re-resolved
        # between selecting a candidate and claiming it.
        attempts.append(os.fspath(dst))
        if len(attempts) == 1:
            # Another process wins the race for the selected name right before our move.
            assert os.fspath(dst) == expected_first.name
            expected_first.write_text("preexisting-archive", encoding="utf-8")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", racing_link)
    moved = quarantine_state_file(p)

    assert attempts == [
        expected_first.name,
        expected_first.name + ".1",
    ]
    assert moved == tmp_path / "state.json.corrupt-20260906T120000Z.1"
    assert expected_first.read_text(encoding="utf-8") == "preexisting-archive"
    assert moved.read_text(encoding="utf-8") == "garbage-new"
    assert not p.exists()


def test_quarantine_gives_up_after_bounded_attempts(tmp_path, monkeypatch):
    import os

    from autoforge import state as state_mod
    from autoforge.state import quarantine_state_file

    monkeypatch.setattr(state_mod, "_QUARANTINE_MAX_ATTEMPTS", 3)
    p = tmp_path / "state.json"
    p.write_text("garbage", encoding="utf-8")

    def always_taken(src, dst, *args, **kwargs):
        raise FileExistsError(17, "File exists", os.fspath(dst))

    monkeypatch.setattr(os, "link", always_taken)
    with pytest.raises(StateError, match="no free name after 3 attempts"):
        quarantine_state_file(p)
    assert p.read_text(encoding="utf-8") == "garbage"  # original untouched


def test_quarantine_unlink_failure_leaves_original_and_drops_reservation(tmp_path, monkeypatch):
    import os

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.write_text("garbage", encoding="utf-8")
    real_unlink = os.unlink

    def failing_unlink(path, *args, **kwargs):
        if os.fspath(path) == p.name:
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", failing_unlink)
    with pytest.raises(StateError, match="cannot move"):
        quarantine_state_file(p)
    assert p.read_text(encoding="utf-8") == "garbage"
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_quarantine_refuses_a_source_replaced_after_reservation(tmp_path, monkeypatch):
    import os

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.write_text("corrupt", encoding="utf-8")
    real_link = os.link
    replaced = False

    def racing_link(src, dst, *args, **kwargs):
        nonlocal replaced
        result = real_link(src, dst, *args, **kwargs)
        if not replaced:
            replaced = True
            p.unlink()
            p.write_text("fresh state", encoding="utf-8")
        return result

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(StateError, match="changed while being quarantined"):
        quarantine_state_file(p)
    assert p.read_text(encoding="utf-8") == "fresh state"
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]


# -- no hard links: the copy fallback (#30) ------------------------------------
def _without_hard_links(monkeypatch, err: int):
    """Make ``os.link`` report what a filesystem without hard links reports."""
    import os

    calls: list[str] = []

    def no_link(src, dst, *args, **kwargs):
        calls.append(os.fspath(dst))
        raise OSError(err, os.strerror(err), os.fspath(dst))

    monkeypatch.setattr(os, "link", no_link)
    return calls


@pytest.mark.parametrize(
    "err",
    sorted({errno.EPERM, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}),
    ids=errno.errorcode.__getitem__,
)
def test_quarantine_copies_the_file_where_hard_links_are_unavailable(tmp_path, monkeypatch, err):
    """#30: vfat/exFAT, some FUSE, SMB and overlay mounts have no link(2).

    The archive is then a byte-for-byte copy under a name created O_EXCL, the
    original is removed afterwards, and 'run --force' works there too instead
    of failing closed with a link error and leaving the operator to move the
    file by hand.
    """
    import os
    import stat

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    raw = b"{not json \xff\x00" + b"x" * (3 * 1024 * 1024)  # spans several copy chunks
    p.write_bytes(raw)
    p.chmod(0o640)
    calls = _without_hard_links(monkeypatch, err)

    moved = quarantine_state_file(p)

    assert len(calls) == 1  # one link attempt taught the fallback; no retry per name
    assert not os.path.lexists(p)
    assert [q.name for q in tmp_path.iterdir()] == [moved.name]
    assert moved.name.startswith("state.json.corrupt-")
    st = moved.lstat()
    assert stat.S_ISREG(st.st_mode) and st.st_nlink == 1
    assert stat.S_IMODE(st.st_mode) == 0o640
    assert moved.read_bytes() == raw


@pytest.mark.parametrize("err", [errno.ENOSYS, errno.EPERM], ids=errno.errorcode.__getitem__)
def test_quarantine_copies_the_file_where_chmod_is_unsupported_too(tmp_path, monkeypatch, err):
    """A filesystem without link(2) commonly has no chmod(2) either (a FUSE
    daemon implementing neither says ENOSYS to both; vfat says EPERM): the
    copy must still be made there, or 'run --force' fails closed on exactly
    the class of filesystem the fallback was written for.  The archive then
    holds the bytes and no bits the source lacked."""
    import os
    import stat

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    raw = b"{not json \xff\x00" + b"x" * (3 * 1024 * 1024)
    p.write_bytes(raw)
    p.chmod(0o664)
    _without_hard_links(monkeypatch, err)

    def no_fchmod(fd, mode):
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr(os, "fchmod", no_fchmod)
    previous = os.umask(0o077)
    try:
        moved = quarantine_state_file(p)
    finally:
        os.umask(previous)

    assert not os.path.lexists(p)
    assert [q.name for q in tmp_path.iterdir()] == [moved.name]
    st = moved.lstat()
    assert stat.S_ISREG(st.st_mode) and st.st_nlink == 1
    assert stat.S_IMODE(st.st_mode) & ~0o664 == 0
    assert moved.read_bytes() == raw


def test_quarantine_fallback_recreates_a_symlink_without_following_it(tmp_path, monkeypatch):
    import os

    from autoforge.state import quarantine_state_file

    target = tmp_path / "elsewhere.json"
    target.write_text("{not json", encoding="utf-8")
    p = tmp_path / "state.json"
    p.symlink_to(target.name)
    _without_hard_links(monkeypatch, errno.ENOTSUP)

    moved = quarantine_state_file(p)

    assert not os.path.lexists(p)
    assert moved.is_symlink() and os.readlink(moved) == target.name
    assert not target.is_symlink() and target.read_text(encoding="utf-8") == "{not json"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["elsewhere.json", moved.name]

    # A dangling link is archived as the same dangling link.
    p.symlink_to("missing-target.json")
    moved2 = quarantine_state_file(p)
    assert not os.path.lexists(p)
    assert moved2.is_symlink() and os.readlink(moved2) == "missing-target.json"


def test_quarantine_fallback_never_replaces_an_existing_archive(tmp_path, monkeypatch):
    """The copy keeps link(2)'s no-replace guarantee: an archive already at
    the selected name (here: the same second) is left intact and the move
    retries with the next numeric suffix, up to the same bound."""
    from datetime import datetime

    from autoforge import state as state_mod
    from autoforge.state import quarantine_state_file

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 6, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(state_mod, "datetime", FrozenDatetime)
    monkeypatch.setattr(state_mod, "_QUARANTINE_MAX_ATTEMPTS", 3)
    _without_hard_links(monkeypatch, errno.EPERM)
    p = tmp_path / "state.json"
    taken = tmp_path / "state.json.corrupt-20260906T120000Z"
    taken.write_text("earlier archive", encoding="utf-8")
    (tmp_path / "state.json.corrupt-20260906T120000Z.1").symlink_to("earlier link")

    p.write_text("garbage-1", encoding="utf-8")
    first = quarantine_state_file(p)
    assert first == tmp_path / "state.json.corrupt-20260906T120000Z.2"
    assert first.read_text(encoding="utf-8") == "garbage-1"

    # base, .1 and now .2 are taken: the bounded retry gives up, and nothing
    # already archived is touched.
    p.write_text("garbage-2", encoding="utf-8")
    with pytest.raises(StateError, match="no free name after 3 attempts"):
        quarantine_state_file(p)
    assert p.read_text(encoding="utf-8") == "garbage-2"  # original untouched
    assert taken.read_text(encoding="utf-8") == "earlier archive"
    assert first.read_text(encoding="utf-8") == "garbage-1"
    assert sorted(q.name for q in tmp_path.iterdir()) == [
        "state.json",
        "state.json.corrupt-20260906T120000Z",
        "state.json.corrupt-20260906T120000Z.1",
        "state.json.corrupt-20260906T120000Z.2",
    ]


def test_quarantine_fallback_refuses_a_fifo_and_leaves_it_untouched(tmp_path, monkeypatch):
    """A FIFO has no bytes to copy: without hard links it stays put with an
    error naming what to do, like a directory does everywhere."""
    import os
    import stat

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    os.mkfifo(p)
    _without_hard_links(monkeypatch, errno.EPERM)
    with pytest.raises(StateError, match="cannot move corrupted state file.*FIFO.*move it by hand"):
        _call_with_timeout(lambda: quarantine_state_file(p))
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]
    assert stat.S_ISFIFO(os.lstat(p).st_mode)


def _fail_fsync_of_regular_files(monkeypatch, err: int, hook=None):
    """Fail ``os.fsync`` on a regular file (the copy's data fsync), not on a
    directory, and run ``hook`` first when given."""
    import os
    import stat

    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            if hook is not None:
                hook()
            if err:
                raise OSError(err, os.strerror(err))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)


def test_quarantine_fallback_copy_failure_leaves_original_and_no_partial_archive(
    tmp_path, monkeypatch
):
    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.write_text("garbage", encoding="utf-8")
    _without_hard_links(monkeypatch, errno.EPERM)
    _fail_fsync_of_regular_files(monkeypatch, errno.ENOSPC)

    with pytest.raises(StateError, match="cannot move corrupted state file.*No space left"):
        quarantine_state_file(p)
    assert p.read_text(encoding="utf-8") == "garbage"
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_quarantine_fallback_refuses_a_source_replaced_during_the_copy(tmp_path, monkeypatch):
    """The copy path keeps the identity check: a state.json replaced while the
    archive was being written is never unlinked, and the archive (a copy of
    whichever bytes were read) is dropped rather than left as a false record."""
    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.write_text("corrupt", encoding="utf-8")
    _without_hard_links(monkeypatch, errno.EPERM)

    def replace_source():
        p.unlink()
        p.write_text("fresh state", encoding="utf-8")

    _fail_fsync_of_regular_files(monkeypatch, 0, hook=replace_source)
    with pytest.raises(StateError, match="changed while being quarantined"):
        quarantine_state_file(p)
    assert p.read_text(encoding="utf-8") == "fresh state"
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_quarantine_other_link_failures_are_not_retried_as_copies(tmp_path, monkeypatch):
    """Only 'no hard links here' selects the fallback. An I/O error from
    link(2) is this move's failure, reported as such with nothing copied."""
    import os

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.write_text("garbage", encoding="utf-8")

    def failing_link(src, dst, *args, **kwargs):
        raise OSError(errno.EIO, os.strerror(errno.EIO), os.fspath(dst))

    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(StateError, match="cannot move corrupted state file.*cannot link") as info:
        quarantine_state_file(p)
    assert "Input/output error" in str(info.value)
    assert p.read_text(encoding="utf-8") == "garbage"
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_a_run_id_that_is_a_path_is_corrupt_state(tmp_path):
    """R3-F2: `run_id` names `<state_dir>/logs/<run_id>`, so it is a write path.

    Loading only checked that it was non-empty, so a hand-edited or truncated
    state carrying "../escape" was accepted and every log artifact of the run
    was then written outside the state directory. A state file that cannot be
    turned into a path fails loudly here, naming the state file, rather than
    silently relocating the controller's own audit trail.
    """
    p = tmp_path / "state.json"
    for bad in ("../escape", "/absolute", "sub/dir", "..", "."):
        data = make_state().to_dict()
        data["run_id"] = bad
        p.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(StateError, match="invalid run_id"):
            load_state(p)
    # A generated identifier still round-trips.
    save_state(make_state(run_id="af-20260101T000000Z-abc123"), p)
    assert load_state(p).run_id == "af-20260101T000000Z-abc123"


def test_load_dangling_symlink_is_corrupt_state(tmp_path):
    """R4-F2: a dangling state.json symlink is an existing (unreadable) entry, not 'no state'.

    The refusal names the link, never what it points at: the controller reads
    and replaces its own regular file, so a link is refused whether its target
    exists, is a FIFO, or is someone else's file. Classifying the target would
    imply some targets are acceptable.
    """
    import os

    p = tmp_path / "state.json"
    p.symlink_to("missing-target.json")
    with pytest.raises(StateError, match="a symbolic link, not a regular file"):
        load_state(p)
    assert p.is_symlink() and os.readlink(p) == "missing-target.json"


def test_quarantine_moves_dangling_symlink_entry_itself(tmp_path):
    import os

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.symlink_to("missing-target.json")
    moved = quarantine_state_file(p)
    assert not os.path.lexists(p)
    assert moved.is_symlink() and os.readlink(moved) == "missing-target.json"


def test_quarantine_moves_symlink_without_following_it(tmp_path):
    """The link is archived as a link; the file it points to is never touched."""
    import os

    from autoforge.state import quarantine_state_file

    target = tmp_path / "elsewhere.json"
    target.write_text("{not json", encoding="utf-8")
    p = tmp_path / "state.json"
    p.symlink_to(target.name)
    moved = quarantine_state_file(p)
    assert not os.path.lexists(p)
    assert moved.is_symlink() and os.readlink(moved) == target.name
    assert not target.is_symlink() and target.read_text(encoding="utf-8") == "{not json"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["elsewhere.json", moved.name]


# -- non-regular state entries (R6-F2) -----------------------------------------
def _call_with_timeout(fn, seconds: float = 5.0):
    """Run ``fn`` in a daemon thread; fail the test instead of hanging forever."""
    import threading

    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"call blocked for more than {seconds}s"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_load_fifo_state_entry_fails_loudly_without_blocking(tmp_path):
    """R6-F2: a FIFO without a writer must not hang load_state; it is a corrupt entry."""
    import os

    p = tmp_path / "state.json"
    os.mkfifo(p)
    with pytest.raises(StateError, match="a FIFO, not a regular file"):
        _call_with_timeout(lambda: load_state(p))
    assert os.path.lexists(p) and [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_load_symlink_to_fifo_fails_loudly_without_blocking(tmp_path):
    import os

    os.mkfifo(tmp_path / "real.fifo")
    p = tmp_path / "state.json"
    p.symlink_to("real.fifo")
    with pytest.raises(StateError, match="a symbolic link, not a regular file"):
        _call_with_timeout(lambda: load_state(p))
    assert p.is_symlink() and os.readlink(p) == "real.fifo"


def test_load_oversized_state_file_is_corrupt_state_without_materialising_it(
    tmp_path, monkeypatch, local_fields
):
    """R10-F3: an oversized or sparse ``state.json`` is refused, never read whole.

    The file is a sparse hole far past the budget: on any filesystem that
    supports holes it costs nothing to create and would cost the whole
    apparent size to ``read()``.  The loader must refuse it as corrupt (so
    ``--force`` quarantines it like any other unreadable state) and must not
    have asked for more than the budget plus a byte.
    """
    import os

    import autoforge.safefs as safefs
    from autoforge.state import MAX_STATE_FILE_BYTES

    p = tmp_path / "state.json"
    asked: list[int] = []
    real_fdopen = safefs.os.fdopen

    def fdopen_with_counted_reads(fd, *args, **kwargs):
        fh = real_fdopen(fd, *args, **kwargs)
        real_read = fh.read

        def read(n=-1):
            asked.append(n)
            return real_read(n)

        fh.read = read  # type: ignore[method-assign]
        return fh

    monkeypatch.setattr(safefs.os, "fdopen", fdopen_with_counted_reads)
    for head in (b'{"phase": "REVIEW"}', _saved_state_bytes(local_fields)):
        p.write_bytes(head)
        os.truncate(p, 16 * MAX_STATE_FILE_BYTES)
        assert p.stat().st_size == 16 * MAX_STATE_FILE_BYTES
        asked.clear()
        with pytest.raises(
            StateError, match="[Cc]orrupt.*larger than .* bytes.*refusing to overwrite"
        ):
            load_state(p)
        assert asked and max(asked) == MAX_STATE_FILE_BYTES + 1, asked
        assert p.stat().st_size == 16 * MAX_STATE_FILE_BYTES, "loader never writes"
        with open(p, "rb") as f:
            assert f.read(len(head)) == head


def test_load_state_file_at_the_budget_still_loads(tmp_path):
    """The bound is a ceiling on what is read, not on what is valid."""
    from autoforge.state import MAX_STATE_FILE_BYTES

    p = tmp_path / "state.json"
    st = make_state()
    body = json.dumps(st.to_dict()).encode("utf-8")
    padding = b" " * (MAX_STATE_FILE_BYTES - len(body))
    p.write_bytes(body + padding)
    assert p.stat().st_size == MAX_STATE_FILE_BYTES
    assert load_state(p).run_id == st.run_id


def test_load_directory_state_entry_is_corrupt_state(tmp_path):
    p = tmp_path / "state.json"
    p.mkdir()
    (p / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(StateError, match="a directory, not a regular file.*by hand"):
        load_state(p)
    assert p.is_dir() and (p / "keep").read_text(encoding="utf-8") == "x"


def test_load_socket_state_entry_is_corrupt_state(tmp_path, monkeypatch):
    import socket

    monkeypatch.chdir(tmp_path)  # AF_UNIX paths are short-limited; bind relative
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind("state.json")
        with pytest.raises(StateError, match="a socket, not a regular file"):
            _call_with_timeout(lambda: load_state(tmp_path / "state.json"))
    finally:
        sock.close()
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]


def test_load_rejects_entry_swapped_for_a_fifo_after_inspection(tmp_path, monkeypatch):
    """The open descriptor is re-checked, so a swap between lstat and open is caught."""
    import os

    from autoforge import state as state_mod

    p = tmp_path / "state.json"
    p.write_text("{}", encoding="utf-8")
    real_lstat = os.lstat

    swapped = False

    def swapping_lstat(path, *a, **kw):
        nonlocal swapped
        st = real_lstat(path, *a, **kw)
        if not swapped and os.fspath(path) == p.name:
            swapped = True
            p.unlink()
            os.mkfifo(p)
        return st

    monkeypatch.setattr(state_mod.os, "lstat", swapping_lstat)
    with pytest.raises(StateError, match="a FIFO, not a regular file"):
        _call_with_timeout(lambda: load_state(p))


def test_quarantine_moves_fifo_entry_without_opening_it(tmp_path):
    import os
    import stat

    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    os.mkfifo(p)
    moved = _call_with_timeout(lambda: quarantine_state_file(p))
    assert not os.path.lexists(p)
    assert stat.S_ISFIFO(os.lstat(moved).st_mode)
    assert [q.name for q in tmp_path.iterdir()] == [moved.name]


def test_quarantine_moves_symlink_to_fifo_as_a_link(tmp_path):
    import os
    import stat

    from autoforge.state import quarantine_state_file

    os.mkfifo(tmp_path / "real.fifo")
    p = tmp_path / "state.json"
    p.symlink_to("real.fifo")
    moved = _call_with_timeout(lambda: quarantine_state_file(p))
    assert moved.is_symlink() and os.readlink(moved) == "real.fifo"
    assert stat.S_ISFIFO(os.lstat(tmp_path / "real.fifo").st_mode)


def test_quarantine_refuses_directory_and_leaves_it_untouched(tmp_path):
    from autoforge.state import quarantine_state_file

    p = tmp_path / "state.json"
    p.mkdir()
    (p / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(StateError, match="it is a directory.*by hand"):
        quarantine_state_file(p)
    assert [q.name for q in tmp_path.iterdir()] == ["state.json"]
    assert (p / "keep").read_text(encoding="utf-8") == "x"


def test_save_state_fsyncs_the_directory_entry_after_the_rename(tmp_path, monkeypatch):
    """PR #44, O3: fsyncing the bytes says nothing about the rename that publishes them.

    A crash right after `save_state` returned could leave the previous
    `state.json` in place — losing the pending-invocation checkpoint a LOCAL
    write phase persists *before* launching an agent.
    """
    import os

    from autoforge import state as state_mod

    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])

    p = tmp_path / "run" / "state.json"
    save_state(make_state(), p)
    assert len(synced) >= 2, "the temp file and its directory must both be flushed"
    assert load_state(p).run_id == "af-test-1"

    # Best effort: a filesystem that cannot fsync a directory must not fail the
    # run. The replace is atomic either way; only its durability is weaker.
    import stat as stat_mod

    def refusing_fsync(fd):
        if stat_mod.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(22, "no directory fsync here")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", refusing_fsync)
    save_state(make_state(review_round=4), p)
    assert load_state(p).review_round == 4
    assert state_mod is not None  # the module under test, imported above


def test_save_state_does_not_swallow_fatal_directory_fsync(tmp_path, monkeypatch):
    import os
    import stat as stat_mod

    p = tmp_path / "state.json"
    save_state(make_state(), p)
    real_fsync = os.fsync

    def failing_fsync(fd):
        if stat_mod.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "I/O error")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    # Not swallowed, and not raw either: reported through the controller's
    # error type, as every other failure of the write is (PR #91 review).
    with pytest.raises(StateError, match="cannot durably publish .*state.json.*I/O error") as exc:
        save_state(make_state(review_round=4), p)
    assert isinstance(exc.value.__cause__, OSError)


def test_the_engine_holds_its_state_root_and_refuses_a_replacement(tmp_path):
    """The state directory is opened once per run and only ever re-verified.

    `StatePaths.open_root` is a pathname operation, so the engine performs it
    once and holds the capability; a later access checks that the pathname
    still names the held inode and never re-resolves it into a new binding.
    An ordinary directory moved into the name is therefore a refusal, not a
    redirection -- and the same for a symbolic link, which `lstat` reports as
    what it is rather than following it.
    """
    from autoforge.config import default_config
    from autoforge.engine import ControllerEngine
    from autoforge.safefs import UnsafePathError

    engine = ControllerEngine(default_config(), state_dir=tmp_path / "state")
    first = engine.state_root(create=True)
    assert engine.state_root() is first, "one capability for the engine's lifetime"

    (tmp_path / "state").rename(tmp_path / "moved")
    (tmp_path / "state").mkdir()
    with pytest.raises(StateError, match="state directory .* replaced"):
        engine.state_root()
    assert list((tmp_path / "state").iterdir()) == []

    (tmp_path / "state").rmdir()
    (tmp_path / "state").symlink_to(tmp_path / "moved")
    with pytest.raises(UnsafePathError, match="symbolic link"):
        engine.state_root()

    # Re-pointing the engine releases the capability; a fresh one is bound
    # to whatever the new location names, once.
    engine.paths = StatePaths.from_state_dir(tmp_path / "other")
    assert first.identity != engine.state_root(create=True).identity
    engine.close()
    with pytest.raises(StateError, match="closed"):
        _ = first.fd


def test_premerge_verification_pass_round_trips_and_is_optional(tmp_path):
    """#42: the pass is bound to a HEAD and a command list; an old state has neither."""
    p = tmp_path / "state.json"
    s = make_state(premerge_verified_head_sha="a" * 40, premerge_verified_commands=[["make"]])
    save_state(s, p)
    loaded = load_state(p)
    assert loaded.premerge_verified_head_sha == "a" * 40
    assert loaded.premerge_verified_commands == [["make"]]
    d = s.to_dict()
    del d["premerge_verified_head_sha"]
    del d["premerge_verified_commands"]
    p.write_text(json.dumps(d), encoding="utf-8")
    loaded = load_state(p)
    assert loaded.premerge_verified_head_sha == "" and loaded.premerge_verified_commands == []


# -- block_reason bound (#88) --------------------------------------------------------------------
def test_bound_block_reason_leaves_a_reason_within_the_bound_alone():
    from autoforge.state import MAX_BLOCK_REASON_CHARS, bound_block_reason

    assert bound_block_reason("") == ""
    short = "merge gate closed. Nothing was merged."
    assert bound_block_reason(short) is short
    exact = "x" * MAX_BLOCK_REASON_CHARS
    assert bound_block_reason(exact) == exact


@pytest.mark.parametrize(
    "length",
    [
        8001,  # one over the bound
        50 * 6000 + 500,  # 50 LOCAL 'unresolved' rationales at the redacted bound (#88)
        1024 * 1024,  # an agent 'message' at the CONTROL_RESULT block bound
        64 * 1024 * 1024,  # the state file's own byte bound: the marker's widest count
    ],
)
def test_bound_block_reason_keeps_the_head_and_the_tail_under_the_bound(length):
    """The head says what happened and the tail says what the operator must
    do; the middle (the concatenated detail) is what is dropped, and the
    result is never over the bound whatever the omitted count's width."""
    from autoforge.state import BLOCK_REASON_TAIL_CHARS, MAX_BLOCK_REASON_CHARS, bound_block_reason

    head_text = "local fix round 1 left 50 of 50 finding(s) explicitly unresolved ("
    tail_text = "). A human must inspect the open findings and start a new run."
    filler = "R" * (length - len(head_text) - len(tail_text))
    reason = head_text + filler + tail_text
    assert len(reason) == length

    bounded = bound_block_reason(reason)
    assert len(bounded) <= MAX_BLOCK_REASON_CHARS
    assert bounded.startswith(head_text)
    assert bounded.endswith(tail_text)
    assert bounded.endswith(reason[-BLOCK_REASON_TAIL_CHARS:])
    marker_start = bounded.index(" [autoforge: ")
    marker_end = bounded.index(" were kept] ") + len(" were kept] ")
    head, marker, tail = (
        bounded[:marker_start],
        bounded[marker_start:marker_end],
        bounded[marker_end:],
    )
    assert reason.startswith(head) and reason.endswith(tail)
    assert len(tail) == BLOCK_REASON_TAIL_CHARS
    omitted = len(reason) - len(head) - len(tail)
    assert marker == (
        f" [autoforge: {omitted} characters of the block reason omitted; the bound is "
        f"{MAX_BLOCK_REASON_CHARS} characters, the first {len(head)} and last {len(tail)} "
        "were kept] "
    )
    # Nothing is dropped twice: what was kept plus what was omitted is the input.
    assert len(head) + omitted + len(tail) == length


def test_bound_block_reason_is_idempotent():
    from autoforge.state import bound_block_reason

    once = bound_block_reason("y" * 20_000)
    assert bound_block_reason(once) == once


def test_a_state_file_with_an_over_long_block_reason_still_loads(tmp_path):
    """The bound is applied by the writers, not the loader: a file an older
    controller wrote with a longer reason is not corrupt, and the load does
    not rewrite it."""
    from autoforge.state import MAX_BLOCK_REASON_CHARS

    p = tmp_path / "state.json"
    long_reason = "z" * (MAX_BLOCK_REASON_CHARS * 3)
    s = make_state(phase=Phase.BLOCKED, block_reason=long_reason)
    save_state(s, p)
    before = p.read_bytes()
    loaded = load_state(p)
    assert loaded.block_reason == long_reason
    assert p.read_bytes() == before


# -- controller-owned effect state (#160; ADR 0004 D2, D4.4, D4.6, D13) -----------------
#
# The four protocol-7 fields (``effect_records``, ``entry_observation``,
# ``completion_context``, ``launch_label``) are validated on every load
# against the state they are loaded with. The fixtures below build each piece
# through the production types, so a fixture that stops validating is a
# change in the schema, not in the test.

EFFECT_ISSUE = "https://github.com/owner/repo/issues/2"
EFFECT_EPIC = "https://github.com/owner/repo/issues/1"
FOLLOW_UP_ISSUE = "https://github.com/owner/repo/issues/9"
EFFECT_TXN = "0123456789abcdef0123456789abcdef"
FIX_REF = "refs/heads/autoforge/2-effects"
BASE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
EPIC_COMMENT = f"{EFFECT_EPIC}#issuecomment-7"
# A credential-shaped string the redactor rewrites; never a real token.
CREDENTIAL_SAMPLE = "ghp_" + "A" * 36


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _owner(phase, *, txn=""):
    return EffectOwner(
        run_id="af-test-1",
        phase=phase,
        issue_url=EFFECT_ISSUE,
        pr_url=PR42,
        transaction_id=txn,
    )


def _progress_marker():
    return render_progress_marker(EFFECT_ISSUE, PR42)


def _progress_record(stage=Stage.INTENDED):
    """K8, the UPDATE_EPIC progress comment, advanced to ``stage`` the way the engine does."""
    marker = _progress_marker()
    record = EffectRecord.plan(
        0,
        EffectKind.PROGRESS_COMMENT,
        _owner(Phase.UPDATE_EPIC),
        identity={"epic_url": EFFECT_EPIC, "marker": marker},
        target={"epic_url": EFFECT_EPIC},
        precondition={"absent": True},
        payload={"body": progress_comment_body("Merged PR #42 for issue #2.", marker)},
    )
    if stage == Stage.INTENDED:
        return record
    record = record.attempting()
    if stage == Stage.OBSERVED:
        return record.observing({"url": EPIC_COMMENT})
    if stage == Stage.CONFLICT:
        return record.conflicting("two comments on the EPIC carry the progress marker")
    return record


def _update_epic_context():
    return UpdateEpicContext(
        issue_url=EFFECT_ISSUE,
        pr_url=PR42,
        roadmap_section="- [x] #2 (merged in #42)\n- [ ] #3",
        next_issue_url="https://github.com/owner/repo/issues/3",
        entry_outside_sha256=sha256_text("outside the markers, before"),
        spliced_outside_sha256=sha256_text("outside the markers, after"),
    )


def _observation(phase, **kw):
    data = dict(
        phase=phase.value,
        issue_url=EFFECT_ISSUE,
        pr_url=PR42,
        refs={},
        base_sha=None,
        objects={},
    )
    data.update(kw)
    return data


def _update_epic_state(stage=Stage.ATTEMPTED, **kw):
    """An UPDATE_EPIC entry with all three pieces and a launch label."""
    base = dict(
        phase=Phase.UPDATE_EPIC,
        current_pr_url=PR42,
        attempt=1,
        launch_label=LABEL_CONTROLLER_PUBLISHES,
        effect_records=[_progress_record(stage).to_dict()],
        entry_observation=_observation(Phase.UPDATE_EPIC, objects={_progress_marker(): None}),
        completion_context=_update_epic_context().to_dict(),
    )
    base.update(kw)
    return make_state(**base)


FIX_FINDINGS = [
    {
        "id": f"R1-F{n}",
        "classification": "blocked",
        "required_resolution": f"Resolve problem {n}.",
        "title": "",
        "location": "",
    }
    for n in range(1, 5)
]


def _follow_up_record(position, finding_id, *, text="Deferred from review round 1."):
    marker = render_follow_up_marker(PR42, finding_id)
    return EffectRecord.plan(
        position,
        EffectKind.FOLLOW_UP_ISSUE,
        _owner(Phase.FIX),
        identity={"repository": "owner/repo", "marker": marker},
        target={"repository": "owner/repo"},
        precondition={"absent": True, "watermark": 41},
        payload={
            "title": f"Follow-up for {finding_id}",
            "body": follow_up_issue_body(text, PR42, finding_id),
        },
    )


def _fix_plan():
    """One FIX plan in every stage: K1 observed, K5 attempted, K6 intended, K5 conflict."""
    push = (
        EffectRecord.plan(
            0,
            EffectKind.PUSH,
            _owner(Phase.FIX),
            identity={"repository": "owner/repo", "ref": FIX_REF, "candidate_sha": CANDIDATE_SHA},
            target={"repository": "owner/repo", "ref": FIX_REF},
            precondition={"expected_old": BASE_SHA, "base_sha": BASE_SHA},
            payload={"sha": CANDIDATE_SHA},
        )
        .attempting()
        .observing({"sha": CANDIDATE_SHA})
    )
    follow_up = _follow_up_record(1, "R1-F1").attempting()
    block = render_follow_up_marker(PR42, "R1-F2")
    base_body = "An earlier follow-up issue."
    append = EffectRecord.plan(
        2,
        EffectKind.FOLLOW_UP_APPEND,
        _owner(Phase.FIX),
        identity={"issue_url": FOLLOW_UP_ISSUE, "markers": [block]},
        target={"issue_url": FOLLOW_UP_ISSUE},
        precondition={"base_sha256": sha256_text(base_body)},
        payload={"body": compose_append(base_body, block), "block": block},
    )
    conflict = (
        _follow_up_record(3, "R1-F3")
        .attempting()
        .attempting()
        .conflicting("two issues carry the follow-up marker")
    )
    return [push, follow_up, append, conflict]


def _fix_context(resolutions=None):
    if resolutions is None:
        resolutions = [
            ("R1-F1", "follow_up_created", "", 1),
            ("R1-F2", "follow_up_created", "", 2),
            ("R1-F3", "follow_up_created", "", 3),
            ("R1-F4", "fixed", CANDIDATE_SHA, None),
        ]
    return FixContext(
        issue_url=EFFECT_ISSUE,
        pr_url=PR42,
        round=1,
        resolutions=tuple(
            {
                "finding_id": fid,
                "resolution": resolution,
                "rationale": f"{fid}: handled as {resolution}.",
                "commit_sha": commit,
                "follow_up_source": source,
                "acceptance_source": None,
                "operator_sources": None,
            }
            for fid, resolution, commit, source in resolutions
        ),
    )


def _fix_state(**kw):
    markers = {render_follow_up_marker(PR42, f"R1-F{n}"): None for n in range(1, 4)}
    base = dict(
        phase=Phase.FIX,
        current_pr_url=PR42,
        review_round=1,
        open_findings=[dict(f) for f in FIX_FINDINGS],
        attempt=1,
        launch_label=LABEL_AGENT_PUBLISHES,
        effect_records=[r.to_dict() for r in _fix_plan()],
        entry_observation=_observation(
            Phase.FIX, refs={FIX_REF: BASE_SHA}, base_sha=BASE_SHA, objects=markers
        ),
        completion_context=_fix_context().to_dict(),
    )
    base.update(kw)
    return make_state(**base)


def _review_marker(round_=1, needs_fix_round=True, finding_ids=("R1-F1",)):
    return render_review_marker(
        round_, CANDIDATE_SHA, "main", BASE_SHA, needs_fix_round, list(finding_ids)
    )


# The reviewer's prose sections, in the parser's stored form (#162).
REVIEW_SECTIONS = {
    "spec": "Meets the issue's acceptance criteria.",
    "standards": "Follows the repository's conventions.",
    "assessment": "One problem remains.",
    "observations": "None.",
    "verification": "Ran `make check`.",
    "summary": "Needs a fix round.",
}


def _review_body(findings=(FIX_FINDINGS[0],), sections=REVIEW_SECTIONS):
    """The comment the controller renders from a round-1 result at the bound revision."""
    result = ReviewResult(
        round=1,
        reviewed_head_sha=CANDIDATE_SHA,
        needs_fix_round=bool(findings),
        findings=[Finding(**f) for f in findings],
        sections=dict(sections),
    )
    return review_comment_body(result, CANDIDATE_SHA, "main", BASE_SHA)


def _review_record(marker=None, position=0, findings=(FIX_FINDINGS[0],), body=None):
    """K4, the round's review comment, planned the way the engine's REVIEW does (#162).

    The body is the controller's rendering of ``findings`` with the shared
    sections, ending in ``marker`` (the rendering's own by default).
    """
    rendered = _review_body(findings) if body is None else body
    own = _review_marker(1, bool(findings), tuple(f["id"] for f in findings))
    if marker is not None:
        assert rendered.endswith(own)
        rendered = rendered[: -len(own)] + marker
    return EffectRecord.plan(
        position,
        EffectKind.REVIEW_COMMENT,
        _owner(Phase.REVIEW),
        identity={"pr_url": PR42, "marker": own if marker is None else marker},
        target={"pr_url": PR42},
        precondition={"absent": True},
        payload={"body": rendered},
    )


def _review_context(findings=(FIX_FINDINGS[0],), sections=REVIEW_SECTIONS):
    return ReviewContext(
        issue_url=EFFECT_ISSUE,
        pr_url=PR42,
        round=1,
        needs_fix_round=bool(findings),
        findings=tuple(dict(f) for f in findings),
        sections=dict(sections),
    )


def _review_state(**kw):
    """A REVIEW entry: the K4 plan saved with the context whose verdict its marker is."""
    base = dict(
        phase=Phase.REVIEW,
        current_pr_url=PR42,
        review_round=0,
        attempt=1,
        launch_label=LABEL_CONTROLLER_PUBLISHES,
        effect_records=[_review_record().to_dict()],
        entry_observation=_observation(Phase.REVIEW),
        completion_context=_review_context().to_dict(),
    )
    base.update(kw)
    return make_state(**base)


REPLAN_BRANCH = f"autoforge/2-replan-{EFFECT_TXN}"
REPLAN_REF = f"refs/heads/{REPLAN_BRANCH}"


def _replan_state(**kw):
    """A REPLAN_REEXECUTE entry (#164): the K1 push of the candidate and the K7
    replacement PR, both owned by the transaction, and the counts its marker attests."""
    push = EffectRecord.plan(
        0,
        EffectKind.PUSH,
        _owner(Phase.REPLAN_REEXECUTE, txn=EFFECT_TXN),
        identity={"repository": "owner/repo", "ref": REPLAN_REF, "candidate_sha": CANDIDATE_SHA},
        target={"repository": "owner/repo", "ref": REPLAN_REF},
        precondition={"expected_old": None, "base_sha": BASE_SHA},
        payload={"sha": CANDIDATE_SHA},
    )
    marker = render_marker(ReplanAttestation(EFFECT_TXN, 1, 3, 2, True))
    create = EffectRecord.plan(
        1,
        EffectKind.REPLACEMENT_PR,
        _owner(Phase.REPLAN_REEXECUTE, txn=EFFECT_TXN),
        identity={
            "repository": "owner/repo",
            "transaction_marker": marker,
            "head_branch": REPLAN_BRANCH,
        },
        target={"repository": "owner/repo", "base": "main", "head": REPLAN_BRANCH},
        precondition={"absent": True, "watermark": 42},
        payload={
            "title": "Reimplement issue #2",
            "body": replacement_pr_body("A fresh reimplementation.", EFFECT_ISSUE, marker),
        },
    )
    context = ReplanContext(
        issue_url=EFFECT_ISSUE,
        pr_url=PR42,
        transaction_id=EFFECT_TXN,
        historical_findings_considered=3,
        unique_failure_constraints=2,
    )
    base = dict(
        phase=Phase.REPLAN_REEXECUTE,
        current_pr_url=PR42,
        replan_transaction={"transaction_id": EFFECT_TXN},
        attempt=1,
        launch_label=LABEL_CONTROLLER_PUBLISHES,
        effect_records=[push.to_dict(), create.to_dict()],
        entry_observation=_observation(
            Phase.REPLAN_REEXECUTE,
            refs={REPLAN_REF: None, "refs/heads/main": BASE_SHA},
            base_sha=BASE_SHA,
        ),
        completion_context=context.to_dict(),
    )
    base.update(kw)
    return make_state(**base)


def _local_state(**kw):
    base = dict(
        run_id="20260101-000000-abcdef",
        mode=WorkflowMode.LOCAL,
        phase=Phase.REVIEW,
        feature_spec_path="features/add-filter.md",
        feature_spec_sha256="a" * 64,
        local_run_contract=sample_contract(),
        workspace_fingerprint="b" * 64,
        base_head_sha="c" * 40,
        base_branch="main",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    base.update(kw)
    return AutoForgeState(**base)


_EFFECT_KEYS = ("effect_records", "entry_observation", "completion_context", "launch_label")


def _effect_fields(source):
    """The four protocol-7 fields of a state object or of its dict form."""
    if isinstance(source, dict):
        return {key: source[key] for key in _EFFECT_KEYS}
    return {key: getattr(source, key) for key in _EFFECT_KEYS}


# -- round trips under the current protocol --------------------------------------------


def test_the_controller_speaks_protocol_12():
    """ADR 0004 D13.1: #160 is protocol 7, #211 (the acceptance comment, K9) is 8,
    #219 (the recorded configuration) is 9, #172 (validation evidence) is 10,
    #243 (operator actions, K10) is 11, #280 (the sandbox pin) is 12; 6 is
    skipped for good."""
    from autoforge import __protocol_version__

    assert __protocol_version__ == "12"


@pytest.mark.parametrize(
    ("stage", "phase"),
    [
        (Stage.INTENDED, Phase.UPDATE_EPIC),
        (Stage.ATTEMPTED, Phase.UPDATE_EPIC),
        (Stage.OBSERVED, Phase.UPDATE_EPIC),
        (Stage.CONFLICT, Phase.BLOCKED),
    ],
)
def test_effect_state_round_trips_in_every_record_stage_under_the_current_protocol(
    tmp_path, stage, phase
):
    """D2.1-D2.3, D4.4, D4.6, D13.3: a record in each stage, the entry observation,
    the completion context and the launch label survive save and load unchanged,
    in a file labelled with the current protocol. A conflict is persisted with
    the run BLOCKED in the phase it stopped, which keeps the pieces bound to that
    phase."""
    p = tmp_path / "state.json"
    s = _update_epic_state(stage, phase=phase)
    save_state(s, p)

    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12"
    assert raw["effect_records"][0]["stage"] == stage.value
    loaded = load_state(p)
    assert loaded == s
    assert _effect_fields(loaded) == _effect_fields(s)
    effects = loaded.phase_effects()
    assert [r.stage for r in effects.records] == [stage]
    assert effects.records[0] == _progress_record(stage)
    assert effects.observation is not None and effects.observation.phase == Phase.UPDATE_EPIC
    assert effects.context == _update_epic_context()
    assert effects.phase == Phase.UPDATE_EPIC
    assert loaded.launch_label == LABEL_CONTROLLER_PUBLISHES


@pytest.mark.parametrize("phase", [Phase.FIX, Phase.BLOCKED, Phase.FAILED])
def test_a_fix_plan_in_every_stage_round_trips_with_its_completion_context(tmp_path, phase):
    """D2.3, D4.6: a FIX plan holding a push (observed), a follow-up issue
    (attempted), a follow-up append (intended) and a follow-up issue at its
    attempt bound (conflict) round-trips with the resolutions whose deferred
    findings name those plan positions; BLOCKED and FAILED keep it bound to FIX."""
    p = tmp_path / "state.json"
    s = _fix_state(phase=phase)
    save_state(s, p)
    loaded = load_state(p)
    assert loaded == s
    effects = loaded.phase_effects()
    assert [r.stage for r in effects.records] == [
        Stage.OBSERVED,
        Stage.ATTEMPTED,
        Stage.INTENDED,
        Stage.CONFLICT,
    ]
    assert [r.attempts for r in effects.records] == [1, 1, 0, 2]
    assert effects.records == tuple(_fix_plan())
    assert effects.context == _fix_context()
    assert effects.observation is not None
    assert effects.observation.refs == {FIX_REF: BASE_SHA}
    assert effects.phase == Phase.FIX


def test_review_and_replan_completion_contexts_round_trip_bound_to_their_state(tmp_path):
    """D4.6: the REVIEW context (round, needs_fix_round, findings) with the one
    review comment (K4) its marker binds (#162), and the REPLAN_REEXECUTE
    context with its push and replacement PR records (bound to the state's
    replan transaction id, #164), round-trip."""
    for name, s in (("review", _review_state()), ("replan", _replan_state())):
        p = tmp_path / f"{name}.json"
        save_state(s, p)
        loaded = load_state(p)
        assert loaded == s
        effects = loaded.phase_effects()
        assert effects.context is not None
        assert effects.context.to_dict() == s.completion_context
    review = load_state(tmp_path / "review.json").phase_effects()
    assert review.records == (_review_record(),)
    assert [r.kind for r in review.records] == [EffectKind.REVIEW_COMMENT]
    assert review.context == _review_context() and review.context.round == 1
    replan = load_state(tmp_path / "replan.json").phase_effects()
    assert [r.kind for r in replan.records] == [EffectKind.PUSH, EffectKind.REPLACEMENT_PR]
    assert {r.owner.transaction_id for r in replan.records} == {EFFECT_TXN}
    assert replan.context.transaction_id == EFFECT_TXN


# -- corruption fails loudly -------------------------------------------------------------


def _set(path, value):
    def mutate(d):
        target = d
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _pop(path):
    def mutate(d):
        target = d
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


def _both(*mutations):
    def mutate(d):
        for m in mutations:
            m(d)

    return mutate


def _many(*mutations):
    def mutate(d):
        for m in mutations:
            m(d)

    return mutate


R0 = ("effect_records", 0)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_set(("effect_records",), {}), "effect_records must be a list, got dict"),
        (_set(R0, "record"), "must be an object, got str"),
        (_pop((*R0, "payload")), "is missing key(s) ['payload']"),
        (_set((*R0, "extra"), 1), "has unknown key(s) ['extra']"),
        (_set((*R0, "position"), 1), "is at position 1, not 0"),
        (
            _set((*R0, "position"), MAX_EFFECTS_PER_PLAN),
            f"position must be in [0, {MAX_EFFECTS_PER_PLAN - 1}], got {MAX_EFFECTS_PER_PLAN}",
        ),
        (_set((*R0, "kind"), "comment"), "is not a Wave 1 effect kind: 'comment'"),
        (_set((*R0, "kind"), "push"), "is a push effect, which UPDATE_EPIC never plans"),
        (_set((*R0, "stage"), "done"), "stage is not a stage: 'done'"),
        (_set((*R0, "stage"), 1), "stage must be a string, got int"),
        (_set((*R0, "attempts"), "1"), "attempts must be an integer, got str"),
        (_set((*R0, "attempts"), True), "attempts must be an integer, got bool"),
        (_set((*R0, "attempts"), 3), "attempts must be in [0, 2], got 3"),
        (_set((*R0, "attempts"), -1), "attempts must be in [0, 2], got -1"),
        (
            _set((*R0, "stage"), "intended"),
            "is intended but records an issued attempt",
        ),
        (_set((*R0, "attempts"), 0), "is attempted but records no attempt"),
        (_set((*R0, "completion"), "trust_me"), "completion must be"),
        (_set((*R0, "observed"), {"url": EPIC_COMMENT}), "must be null while the record is"),
        (_set((*R0, "stage"), "observed"), "observed must be an object, got NoneType"),
        (
            _many(
                _set((*R0, "stage"), "observed"),
                _set((*R0, "observed"), {"url": f"{FOLLOW_UP_ISSUE}#issuecomment-7"}),
            ),
            "is observed on another issue than its EPIC",
        ),
        (_set((*R0, "stage"), "conflict"), "reason is set exactly when the record is a conflict"),
        (_set((*R0, "reason"), "stale"), "reason is set exactly when the record is a conflict"),
        (_set((*R0, "owner"), None), "owner must be an object, got NoneType"),
        (_pop((*R0, "owner", "run_id")), "owner is missing key(s) ['run_id']"),
        (_set((*R0, "owner", "run_id"), 5), "owner.run_id must be a string, got int"),
        (_set((*R0, "owner", "phase"), "TELEPORT"), "owner.phase is not a phase: 'TELEPORT'"),
        (
            _set((*R0, "owner", "transaction_id"), EFFECT_TXN),
            "must be empty outside REPLAN_REEXECUTE",
        ),
        (_set((*R0, "owner", "issue_url"), "issues/2"), "is not a GitHub URL"),
        (_set((*R0, "identity", "epic_url"), FOLLOW_UP_ISSUE), "differs between its identity"),
        (
            _set(
                (*R0, "identity", "marker"),
                render_progress_marker("https://github.com/owner/repo/issues/3", PR42),
            ),
            "carries a marker for another issue than its owner's",
        ),
        (
            _set((*R0, "identity", "marker"), "<!-- not a marker -->"),
            "must be exactly one well-formed ai-epic-progress marker",
        ),
        (_set((*R0, "precondition", "absent"), False), "precondition.absent must be true"),
        (
            _set((*R0, "payload", "body"), "Merged PR #42."),
            "has a payload body that does not end with its identity's marker",
        ),
        (
            _set((*R0, "payload", "body"), f"token {CREDENTIAL_SAMPLE}\n\n{_progress_marker()}"),
            "payload.body is not redaction-invariant",
        ),
        (
            _set((*R0, "payload", "body"), "x" * 65537),
            "payload.body is 65537 characters, over its bound of 65536",
        ),
        (
            _set((*R0, "payload", "body"), f"nul \x00 byte\n\n{_progress_marker()}"),
            "carries a NUL or DEL character",
        ),
        (
            _set((*R0, "payload", "body"), f"Merged PR #42.{_progress_marker()}"),
            "has a payload body that is not its progress text, a blank line and its marker",
        ),
        # The progress text gets the parser's rules again: a resumed write
        # publishes it with no agent result in between.
        (
            _set((*R0, "payload", "body"), f"\n\n{_progress_marker()}"),
            "has an invalid progress text: CONTROL_RESULT for UPDATE_EPIC missing required "
            "field 'progress'",
        ),
        (
            _set((*R0, "payload", "body"), f"Merged, thanks @octocat\n\n{_progress_marker()}"),
            "has an invalid progress text: UPDATE_EPIC: field 'progress' contains an @-mention",
        ),
        (
            _set((*R0, "payload", "body"), f"Merged.\nCloses #99\n\n{_progress_marker()}"),
            "has an invalid progress text: UPDATE_EPIC: field 'progress' contains a closing "
            "keyword",
        ),
        (
            _set((*R0, "payload", "body"), f"bad\x01text\n\n{_progress_marker()}"),
            "has an invalid progress text: UPDATE_EPIC: result field 'progress' contains a "
            "control character (U+0001",
        ),
        (
            _set((*R0, "payload", "body"), f"see https://example.com\n\n{_progress_marker()}"),
            "has an invalid progress text: UPDATE_EPIC: field 'progress' contains a URL",
        ),
    ],
)
def test_a_corrupt_effect_record_fails_loudly(tmp_path, mutate, needle):
    """D2.1-D2.3: a record that is not exactly the closed schema of its kind and
    stage -- a missing or unknown key, a wrong type, an unknown kind or stage, a
    stage that disagrees with its attempt count, observation or reason, an owner,
    identity or payload that fails its cross-checks -- is corruption, and the
    load raises StateError instead of reconciling from it."""
    d = _update_epic_state(Stage.ATTEMPTED).to_dict()
    mutate(d)
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert needle in str(exc.value)
    assert p.read_bytes() == before


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_set(("entry_observation",), []), "entry_observation must be an object, got list"),
        (_pop(("entry_observation", "refs")), "entry_observation is missing key(s) ['refs']"),
        (_set(("entry_observation", "extra"), 1), "entry_observation has unknown key(s)"),
        (_set(("entry_observation", "phase"), "TELEPORT"), "is not a phase: 'TELEPORT'"),
        (_set(("entry_observation", "phase"), "MERGE"), "names MERGE, which publishes nothing"),
        (_set(("entry_observation", "refs"), []), "refs must be an object of at most 4 refs"),
        (
            _set(
                ("entry_observation", "refs"),
                {f"refs/heads/b{n}": None for n in range(5)},
            ),
            "refs must be an object of at most 4 refs",
        ),
        (_set(("entry_observation", "refs"), {"main": None}), "must be a full refs/heads/"),
        (
            _set(("entry_observation", "refs"), {FIX_REF: "abc"}),
            "must be a full 40-character lowercase commit SHA",
        ),
        (
            _set(("entry_observation", "base_sha"), "A" * 40),
            "base_sha must be a full 40-character lowercase commit SHA",
        ),
        (
            _set(("entry_observation", "objects"), {"not a marker": None}),
            "must be one controller marker",
        ),
        (
            _set(("entry_observation", "objects"), {_progress_marker(): "issues/9"}),
            "objects value is not a GitHub URL",
        ),
        (
            _set(("entry_observation", "pr_url"), EFFECT_ISSUE),
            "entry_observation.pr_url is not a GitHub URL of the expected kind",
        ),
        (
            _set(("entry_observation", "prs"), []),
            "prs must be a non-empty object of at most 4 refs when stored",
        ),
        (
            _set(("entry_observation", "prs"), {}),
            "prs must be a non-empty object of at most 4 refs when stored",
        ),
        (
            _set(("entry_observation", "prs"), {f"refs/heads/b{n}": None for n in range(5)}),
            "prs must be a non-empty object of at most 4 refs when stored",
        ),
        (_set(("entry_observation", "prs"), {"main": None}), "must be a full refs/heads/"),
        (
            _set(("entry_observation", "prs"), {FIX_REF: None}),
            f"records a PR on {FIX_REF}, a ref the observation did not read",
        ),
        (
            _both(
                _set(("entry_observation", "refs"), {FIX_REF: None}),
                _set(("entry_observation", "prs"), {FIX_REF: EFFECT_ISSUE}),
            ),
            f"entry_observation.prs[{FIX_REF}] is not a GitHub URL of the expected kind",
        ),
    ],
)
def test_a_corrupt_entry_observation_fails_loudly(tmp_path, mutate, needle):
    """D4.4: the entry observation is a closed, bounded schema (refs, base,
    marker-keyed objects, the open PR read on a ref it read); anything else
    is corruption and refused on load."""
    d = _update_epic_state().to_dict()
    mutate(d)
    p = _write(tmp_path / "state.json", d)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert needle in str(exc.value)


CTX = ("completion_context",)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_set(CTX, []), "completion_context must be an object, got list"),
        (_set((*CTX, "phase"), "MERGE"), "completion_context.phase must name a publishing phase"),
        (_set((*CTX, "phase"), ["UPDATE_EPIC"]), "completion_context.phase must name a publishing"),
        (_pop((*CTX, "section_void")), "is missing key(s) ['section_void']"),
        (_set((*CTX, "extra"), 1), "has unknown key(s) ['extra']"),
        (_set((*CTX, "selection_void"), "no"), "selection_void must be a boolean, got str"),
        (_set((*CTX, "pr_url"), EFFECT_ISSUE), "pr_url is not a GitHub URL of the expected kind"),
        (
            _set((*CTX, "spliced_outside_sha256"), None),
            "must hold both outside-markers digests or neither",
        ),
        (
            _set((*CTX, "entry_outside_sha256"), "0" * 63),
            "must be a 64-character lowercase SHA-256 hex digest",
        ),
        (
            _many(
                _set((*CTX, "entry_outside_sha256"), None),
                _set((*CTX, "spliced_outside_sha256"), None),
            ),
            "holds the outside-markers digests exactly when it holds a section",
        ),
        (
            _set((*CTX, "roadmap_section"), f"- [x] #2 {CREDENTIAL_SAMPLE}"),
            "roadmap_section is not redaction-invariant",
        ),
        (_set((*CTX, "roadmap_section"), "   "), "must be non-empty when present"),
        # The parser's rules for the field, applied again to its stored form (D4.6).
        (
            _set((*CTX, "roadmap_section"), "- [x] #2 thanks @octocat"),
            "roadmap_section is invalid: UPDATE_EPIC: field 'roadmap_section' contains an "
            "@-mention",
        ),
        (
            _set((*CTX, "roadmap_section"), "- [x] #2\n\nCloses #99"),
            "roadmap_section is invalid: UPDATE_EPIC: field 'roadmap_section' contains a "
            "closing keyword",
        ),
        (
            _set((*CTX, "roadmap_section"), "- [x] bad\x01text"),
            "roadmap_section is invalid: UPDATE_EPIC: result field 'roadmap_section' contains "
            "a control character (U+0001",
        ),
        (
            _set((*CTX, "roadmap_section"), "- [x] #2\n<!-- ai-controller-roadmap:end -->"),
            "roadmap_section is invalid: UPDATE_EPIC: field 'roadmap_section' must not contain "
            "a controller marker",
        ),
        (
            _set((*CTX, "roadmap_section"), "- [x] #2\n<!-- autoforge-replan -->"),
            "roadmap_section is invalid: UPDATE_EPIC: field 'roadmap_section' contains a "
            "controller marker opener",
        ),
        (_set((*CTX, "selection_void"), True), "voids its selection but still holds one"),
        (_set((*CTX, "section_void"), True), "voids its roadmap section but still holds one"),
    ],
)
def test_a_corrupt_completion_context_fails_loudly(tmp_path, mutate, needle):
    """D4.6: the completion context is the closed schema of the phase it names;
    recovery completes from it, so a context that is not exactly that schema is
    refused on load rather than consumed."""
    d = _update_epic_state().to_dict()
    mutate(d)
    p = _write(tmp_path / "state.json", d)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert needle in str(exc.value)


def _only(piece):
    """An UPDATE_EPIC state carrying just one piece (records travel with their context)."""
    full = _update_epic_state()
    keep = {
        "records": ("effect_records", "completion_context"),
        "observation": ("entry_observation",),
        "context": ("completion_context",),
    }[piece]
    empty = {"effect_records": [], "entry_observation": {}, "completion_context": {}}
    return make_state(
        phase=full.phase,
        current_pr_url=full.current_pr_url,
        attempt=full.attempt,
        launch_label=full.launch_label,
        **{k: (getattr(full, k) if k in keep else v) for k, v in empty.items()},
    )


@pytest.mark.parametrize("piece", ["records", "observation", "context"])
@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (
            {"current_issue_url": "https://github.com/owner/repo/issues/3"},
            "is bound to another issue than the state's current issue",
        ),
        (
            {"current_pr_url": "https://github.com/owner/repo/pull/43"},
            "is bound to another PR than the state's current PR",
        ),
        ({"phase": Phase.ANALYZE_EXECUTE}, "is bound to UPDATE_EPIC but the state is in"),
        ({"phase": Phase.DONE}, "is bound to UPDATE_EPIC but the state is in DONE"),
    ],
)
def test_effect_state_bound_to_another_issue_pr_or_phase_is_refused(
    tmp_path, piece, change, needle
):
    """D2.2, D4.6: records, the observation and the context each agree with the
    state they are loaded with -- its current issue, its current PR and its
    phase. A piece bound to another is never adopted by this entry."""
    s = _only(piece)
    for key, value in change.items():
        setattr(s, key, value)
    p = tmp_path / "state.json"
    save_state(s, p)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert needle in str(exc.value)


@pytest.mark.parametrize(
    ("build", "change", "needle"),
    [
        (_update_epic_state, {"run_id": "af-test-2"}, "is bound to another run than the state's"),
        (
            _replan_state,
            {"replan_transaction": {"transaction_id": "f" * 32}},
            "effect_records[0] is bound to another replan transaction than the state's",
        ),
        (
            lambda: _replan_state(effect_records=[]),
            {"replan_transaction": {"transaction_id": "f" * 32}},
            "transaction_id is not the state's replan transaction id",
        ),
        (
            lambda: _replan_state(effect_records=[]),
            {"replan_transaction": {}},
            "transaction_id is not the state's replan transaction id",
        ),
        (
            _review_state,
            {"review_round": 1},
            "REVIEW completion context.round is 1 but the round under review is 2",
        ),
        (
            _fix_state,
            {"review_round": 2},
            "FIX completion context.round is 1 but the round being fixed is 2",
        ),
        (
            _fix_state,
            {"open_findings": [dict(f) for f in FIX_FINDINGS[:3]]},
            "must resolve exactly the open findings",
        ),
    ],
)
def test_effect_state_bound_to_another_run_round_findings_or_transaction_is_refused(
    tmp_path, build, change, needle
):
    """D2.2, D4.6: a record is bound to its run (and, in REPLAN_REEXECUTE, to the
    replan transaction id); a context to the round under review or being fixed,
    the open findings it resolves and the state's transaction id."""
    s = build()
    for key, value in change.items():
        setattr(s, key, value)
    p = tmp_path / "state.json"
    save_state(s, p)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert needle in str(exc.value)


def test_effect_state_naming_two_phases_is_refused_even_when_blocked(tmp_path):
    """D2.4 with D13: BLOCKED relaxes the phase binding only to "the phase it
    stopped"; records, observation and context naming two phases are refused."""
    s = _update_epic_state(Stage.CONFLICT, phase=Phase.BLOCKED)
    s.entry_observation = _observation(Phase.FIX)
    p = tmp_path / "state.json"
    save_state(s, p)
    with pytest.raises(StateError, match="names more than one phase"):
        load_state(p)


_NO_OBSERVATION: dict = {}


@pytest.mark.parametrize("phase", [Phase.UPDATE_EPIC, Phase.BLOCKED])
@pytest.mark.parametrize(
    ("records", "observation", "needle"),
    [
        # The entry saw no comment, so the context was saved with its K8 record.
        (
            [],
            _observation(Phase.UPDATE_EPIC, objects={_progress_marker(): None}),
            "must be saved with the one progress-comment record of its marker",
        ),
        (
            [],
            _NO_OBSERVATION,
            "is persisted without the entry observation read before its launch",
        ),
        (
            [],
            _observation(Phase.UPDATE_EPIC),
            "has an entry observation that does not record its progress marker",
        ),
        (
            [],
            _observation(
                Phase.UPDATE_EPIC,
                objects={
                    render_progress_marker(
                        EFFECT_ISSUE, "https://github.com/owner/repo/pull/41"
                    ): EPIC_COMMENT
                },
            ),
            "has an entry observation that does not record its progress marker",
        ),
        # The entry adopted a legacy comment, so it plans none of its own.
        (
            [_progress_record(Stage.INTENDED).to_dict()],
            _observation(Phase.UPDATE_EPIC, objects={_progress_marker(): EPIC_COMMENT}),
            f"plans a progress comment beside the adopted {EPIC_COMMENT}",
        ),
    ],
    ids=["empty-plan", "no-observation", "no-marker", "another-pr-marker", "plan-and-adoption"],
)
def test_update_epic_context_without_its_progress_comment_is_refused(
    tmp_path, phase, records, observation, needle
):
    """D2.2, D4.6, D13.7: UPDATE_EPIC completes from its context and publishes
    only what its plan holds, so the context is loaded only beside the K8
    record of its marker (the entry saw no comment) or an empty plan whose
    entry observation adopted the one legacy comment. An emptied plan is not
    "nothing left to publish": it is refused, and the file left unchanged."""
    s = _update_epic_state(
        Stage.INTENDED, phase=phase, effect_records=records, entry_observation=observation
    )
    p = _write(tmp_path / "state.json", s.to_dict())
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"UPDATE_EPIC completion context {needle}" in str(exc.value)
    assert p.read_bytes() == before


@pytest.mark.parametrize("phase", [Phase.UPDATE_EPIC, Phase.BLOCKED])
def test_update_epic_context_of_an_adopted_legacy_comment_loads_with_no_record(tmp_path, phase):
    """D13.7: the one legacy progress comment the entry observation adopted is
    the phase's publication; its context is saved with no record and loads."""
    s = _update_epic_state(
        phase=phase,
        effect_records=[],
        entry_observation=_observation(
            Phase.UPDATE_EPIC, objects={_progress_marker(): EPIC_COMMENT}
        ),
    )
    p = tmp_path / "state.json"
    save_state(s, p)
    effects = load_state(p).phase_effects()
    assert effects.records == () and effects.published
    assert effects.observation is not None
    assert effects.observation.objects == {_progress_marker(): EPIC_COMMENT}
    assert isinstance(effects.context, UpdateEpicContext)


# The REVIEW context and its review comment (#162): REVIEW completes from its
# context by posting the round's one comment (K4), whose marker is the verdict
# the context holds.


def _follow_up_owned_by_review():
    """A follow-up issue record relabelled as REVIEW's, which REVIEW never plans."""
    d = _follow_up_record(0, "R1-F1").to_dict()
    d["owner"]["phase"] = Phase.REVIEW.value
    return d


@pytest.mark.parametrize("phase", [Phase.REVIEW, Phase.BLOCKED])
@pytest.mark.parametrize(
    ("records", "needle"),
    [
        pytest.param(
            [],
            "REVIEW completion context must be saved with exactly one review comment",
            id="no-record",
        ),
        pytest.param(
            [_review_record().to_dict(), _review_record(position=1).to_dict()],
            "REVIEW completion context must be saved with exactly one review comment",
            id="two-records",
        ),
        pytest.param(
            [_follow_up_owned_by_review()],
            "effect record 0 is a follow_up_issue effect, which REVIEW never plans",
            id="a-kind-review-never-plans",
        ),
    ],
)
def test_review_context_without_its_one_review_comment_is_refused(tmp_path, phase, records, needle):
    """#162, D4.6: the context is saved in the same save as the round's one K4
    record and completes by posting it, so it loads only beside exactly that
    record. No record, two of them, or a record of another kind is corruption:
    refused on load, and the file left unchanged."""
    s = _review_state(phase=phase, effect_records=records)
    p = _write(tmp_path / "state.json", s.to_dict())
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert needle in str(exc.value)
    assert p.read_bytes() == before


_NO_FINDING_IDS_MARKER = REVIEW.render(
    {
        "round": 1,
        "reviewed_head_sha": CANDIDATE_SHA,
        "reviewed_base_ref": "main",
        "reviewed_merge_base_sha": BASE_SHA,
        "needs_fix_round": True,
    }
)


@pytest.mark.parametrize(
    ("findings", "marker"),
    [
        pytest.param((), _review_marker(2, False, ()), id="round"),
        pytest.param((FIX_FINDINGS[0],), _review_marker(1, False, ("R1-F1",)), id="needs-fix"),
        pytest.param((), _review_marker(1, True, ()), id="needs-fix-on-a-clean-round"),
        pytest.param((FIX_FINDINGS[0],), _review_marker(1, True, ("R1-F2",)), id="finding-id"),
        pytest.param(
            FIX_FINDINGS[:2], _review_marker(1, True, ("R1-F2", "R1-F1")), id="finding-order"
        ),
        pytest.param(FIX_FINDINGS[:2], _review_marker(1, True, ("R1-F1",)), id="a-finding-short"),
        pytest.param((FIX_FINDINGS[0],), _NO_FINDING_IDS_MARKER, id="no-finding-ids"),
    ],
)
def test_review_context_disagreeing_with_its_comments_marker_is_refused(tmp_path, findings, marker):
    """#162: the comment's marker is the context's verdict -- the same round, the
    same needs_fix_round, and the findings' ids in the same order. A pair that
    disagrees would publish one verdict and route the run on another, so it
    is refused on load; the same context beside its own marker loads."""
    context = _review_context(findings).to_dict()
    ids = tuple(f["id"] for f in findings)
    agreeing = _review_record(_review_marker(1, bool(findings), ids), findings=findings)
    p = tmp_path / "state.json"
    save_state(_review_state(effect_records=[agreeing.to_dict()], completion_context=context), p)
    effects = load_state(p).phase_effects()
    assert effects.records == (agreeing,) and effects.context == _review_context(findings)

    disagreeing = _review_record(marker, findings=findings)
    s = _review_state(effect_records=[disagreeing.to_dict()], completion_context=context)
    p = _write(tmp_path / "state.json", s.to_dict())
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert (
        "REVIEW completion context disagrees with its review comment's marker on the round's "
        "verdict" in str(exc.value)
    )
    assert p.read_bytes() == before


def _edit_body(old, new):
    """Replace the one occurrence of ``old`` in the persisted K4 body with ``new``."""

    def mutate(d):
        payload = d["effect_records"][0]["payload"]
        assert payload["body"].count(old) == 1
        payload["body"] = payload["body"].replace(old, new)

    return mutate


@pytest.mark.parametrize("phase", [Phase.REVIEW, Phase.BLOCKED])
@pytest.mark.parametrize(
    "mutate",
    [
        # Each edit leaves the record valid on its own (bounded, redaction-
        # invariant, ending in its marker): only its context can tell.
        pytest.param(
            _edit_body("One problem remains.", "One problem remains, @octocat."),
            id="mention-in-a-section",
        ),
        pytest.param(
            _edit_body("Needs a fix round.", "Needs a fix round.\n<!-- ai-review-result: {} -->"),
            id="marker-opener-in-a-section",
        ),
        pytest.param(
            _edit_body("Resolve problem 1.", "Resolve problem 1. Closes #3"),
            id="closing-keyword-in-a-finding",
        ),
        pytest.param(
            _edit_body("### R1-F1 [blocked]", "### R1-F1 [blocked] thanks @octocat"),
            id="mention-in-a-finding-line",
        ),
        pytest.param(
            _edit_body(
                "# AI Code Review — Round 1\n", "# AI Code Review — Round 1\n\ncc @org/team\n"
            ),
            id="text-outside-every-field",
        ),
        pytest.param(
            _edit_body(f"Reviewed HEAD: `{CANDIDATE_SHA}`", f"Reviewed HEAD: `{BASE_SHA}`"),
            id="binding-line",
        ),
        pytest.param(_edit_body("## Observations\n\nNone.\n\n", ""), id="a-section-dropped"),
        pytest.param(
            _edit_body("Needs another fix round: YES", "Needs another fix round: NO"),
            id="needs-fix-line",
        ),
        pytest.param(
            lambda d: d["effect_records"][0]["payload"].update(body=_review_marker()),
            id="the-marker-alone",
        ),
    ],
)
def test_a_persisted_review_comment_that_is_not_its_contexts_rendering_is_refused(
    tmp_path, phase, mutate
):
    """#162, D4.6: a resumed K4 posts its persisted body with no reviewer result
    in between, so the body must be exactly what the controller renders from
    the context saved with it (the findings and prose sections the parser
    accepted) at the revision its marker binds. A body altered after the
    save -- an @-mention, a marker opener or a closing keyword added to a
    field or around them, the binding line, a section or the needs-fix line
    changed -- would publish what no validated result holds: refused on
    load, before recovery can issue it, and the file left unchanged."""
    d = _review_state(phase=phase).to_dict()
    mutate(d)
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert (
        "REVIEW completion context is saved with a review comment whose body is not the "
        "comment the controller renders from it" in str(exc.value)
    )
    assert p.read_bytes() == before


def _sections_with(**change):
    sections = dict(REVIEW_SECTIONS)
    sections.update(change)
    return sections


def _section_edit(key, value):
    """Set section ``key`` in the context and re-render the body to agree with it.

    The pair is consistent, so only the parser's rules for the section can
    refuse it: an edit of state that changed both is still caught.
    """

    def mutate(d):
        sections = _sections_with(**{key: value})
        d["completion_context"]["sections"] = sections
        record = d["effect_records"][0]
        record["payload"]["body"] = _review_body(sections=sections)

    return mutate


def _sections_raw(change):
    """Change the context's raw sections only (no body can render the result)."""

    def mutate(d):
        change(d["completion_context"]["sections"])

    return mutate


def _drop(key):
    def change(sections):
        del sections[key]

    return change


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        pytest.param(
            _section_edit("assessment", "One problem remains, @octocat."),
            "sections.assessment is invalid: REVIEW: field 'assessment' contains an @-mention",
            id="mention",
        ),
        pytest.param(
            _section_edit("summary", "Done.\n<!-- ai-review-result: {} -->"),
            "sections.summary is invalid: REVIEW: field 'summary' contains a controller marker "
            "opener",
            id="marker-opener",
        ),
        pytest.param(
            _section_edit("spec", "Closes #3"),
            "sections.spec is invalid: REVIEW: field 'spec' contains a closing keyword",
            id="closing-keyword",
        ),
        pytest.param(
            _section_edit("verification", "bad\x01text"),
            "sections.verification is invalid: REVIEW: result field 'verification' contains a "
            "control character (U+0001",
            id="control-character",
        ),
        pytest.param(
            _section_edit("summary", "x" * 2001),
            "sections.summary is invalid: REVIEW: result field 'summary' is 2001 characters; "
            "the controller accepts at most 2000",
            id="over-its-bound",
        ),
        pytest.param(
            _section_edit("summary", "  Needs a fix round.  "),
            "sections.summary is not in the parser's stored form",
            id="unstripped",
        ),
        pytest.param(
            _section_edit("observations", "   "),
            "sections.observations is invalid: CONTROL_RESULT for REVIEW missing required "
            "field 'observations'",
            id="blank",
        ),
        pytest.param(
            _sections_raw(lambda sections: sections.update(summary=5)),
            "sections.summary is invalid: REVIEW: field 'summary' must be a string",
            id="not-a-string",
        ),
        pytest.param(
            _sections_raw(lambda sections: sections.update(standards=CREDENTIAL_SAMPLE)),
            "sections.standards is invalid: REVIEW: field 'standards' contains a "
            "credential-shaped string",
            id="credential",
        ),
        pytest.param(
            _sections_raw(_drop("summary")),
            "sections is missing key(s) ['summary']",
            id="missing-section",
        ),
        pytest.param(
            _sections_raw(lambda sections: sections.update(notes="More.")),
            "sections has unknown key(s) ['notes']",
            id="unknown-section",
        ),
        pytest.param(
            lambda d: d["completion_context"].pop("sections"),
            "REVIEW completion context is missing key(s) ['sections']",
            id="no-sections",
        ),
    ],
)
def test_review_context_sections_not_in_the_parsers_stored_form_are_refused(
    tmp_path, mutate, needle
):
    """#162, D4.6: the prose sections the K4 body renders are saved with it and
    get the parser's rules again on load, so a state edited consistently
    (section and body alike) still cannot make recovery publish text the
    result path would have refused. Refused, never normalized, the message
    never quoting the refused text, and the file left unchanged."""
    d = _review_state().to_dict()
    mutate(d)
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert needle in str(exc.value)
    assert CREDENTIAL_SAMPLE not in str(exc.value) and "@octocat" not in str(exc.value)
    assert p.read_bytes() == before


def test_a_review_context_and_its_comment_round_trip_with_every_published_field(tmp_path):
    """#162: findings with a title and a location (rendered in a code span,
    backticks and all) and multi-line prose round-trip: the body re-rendered
    on load is byte-identical to the one saved."""
    findings = (
        dict(FIX_FINDINGS[0], title="A title", location="src/a.py:1 `x`"),
        dict(FIX_FINDINGS[1], required_resolution="First line.\n\n- then a list\n\tand a tab"),
    )
    sections = _sections_with(spec="Line one.\n\n## A heading of its own\n\nLine two.")
    record = _review_record(findings=findings, body=_review_body(findings, sections))
    context = _review_context(findings, sections)
    p = tmp_path / "state.json"
    save_state(
        _review_state(effect_records=[record.to_dict()], completion_context=context.to_dict()), p
    )
    effects = load_state(p).phase_effects()
    assert effects.records == (record,) and effects.context == context
    assert effects.context.sections == sections


def _review_plan(findings, sections=REVIEW_SECTIONS):
    """A REVIEW state whose context and K4 body agree: only the rules can refuse it."""
    record = _review_record(findings=findings, body=_review_body(findings, sections))
    return _review_state(
        effect_records=[record.to_dict()],
        completion_context=_review_context(findings, sections).to_dict(),
    ).to_dict()


@pytest.mark.parametrize(
    ("findings", "sections"),
    [
        pytest.param(
            (
                dict(FIX_FINDINGS[0], required_resolution="Rename it:\n~~~"),
                dict(FIX_FINDINGS[1], required_resolution="See\n~~~\n@octocat\n~~~"),
            ),
            REVIEW_SECTIONS,
            id="a-fence-closed-by-the-next-finding",
        ),
        pytest.param(
            (FIX_FINDINGS[0],),
            _sections_with(observations="Logs:\n```", verification="Ran:\n```\n@octocat"),
            id="a-fence-closed-by-the-next-section",
        ),
        pytest.param(
            (dict(FIX_FINDINGS[0], title="Use `@octocat`"),),
            _sections_with(verification="<pre>"),
            id="raw-html-beside-a-mention-in-code",
        ),
    ],
)
def test_a_review_plan_whose_fields_compose_a_mention_outside_code_is_refused(
    tmp_path, findings, sections
):
    """#162 (R2-F1), D4.6: every field passes the parser's rules alone and the
    body is exactly the context's rendering, but in the composed comment one
    field's fence or raw HTML leaves another's mention outside code. The
    result path refuses such a comment before planning it, so a saved plan
    holding one was never accepted: loading it is refused, never replayed,
    the message never quoting the text, and the file left unchanged."""
    p = _write(tmp_path / "state.json", _review_plan(findings, sections))
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert (
        "REVIEW completion context is saved with a review comment the controller may not "
        "publish: the rendered 'review comment' contains an @-mention" in str(exc.value)
    )
    assert "@octocat" not in str(exc.value)
    assert p.read_bytes() == before


def test_a_review_plan_keeps_each_fields_code_in_the_composed_comment(tmp_path):
    """#162 (R2-F1): a title ending in an unpaired backtick and a resolution
    that opens with a fence are blocks of their own in the comment, so the
    mentions they hold stay code (in a list item they did not: the backtick
    paired with the resolution's, and the prefix pushed the fence off its
    column). The plan loads as saved."""
    findings = (
        dict(FIX_FINDINGS[0], title="A `", required_resolution="`@octocat`"),
        dict(FIX_FINDINGS[1], required_resolution="~~~\n@octocat\n~~~"),
    )
    body = _review_body(findings)
    assert "### R1-F1 [blocked] — A `\n\nRequired resolution:\n\n`@octocat`\n\n" in body
    assert "### R1-F2 [blocked]\n\nRequired resolution:\n\n~~~\n@octocat\n~~~\n\n" in body
    p = tmp_path / "state.json"
    _write(p, _review_plan(findings))
    effects = load_state(p).phase_effects()
    assert effects.context == _review_context(findings)
    assert effects.records[0].body == body


def _set_finding(**change):
    def mutate(finding):
        finding.update(change)

    return mutate


def _drop_finding_key(key):
    def mutate(finding):
        del finding[key]

    return mutate


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        pytest.param(
            _set_finding(required_resolution="  Resolve problem 1.  "),
            "findings[0] is not in the parser's stored form",
            id="unstripped",
        ),
        pytest.param(
            _drop_finding_key("title"),
            "findings[0] is not in the parser's stored form",
            id="missing-key",
        ),
        pytest.param(
            _set_finding(severity="high"),
            "findings[0] is not in the parser's stored form",
            id="extra-key",
        ),
        pytest.param(
            _set_finding(classification="maybe"),
            "findings[0] is invalid: REVIEW: finding R1-F1 classification must be one of",
            id="classification",
        ),
        pytest.param(
            _set_finding(title="two\nlines"),
            "findings[0] is invalid: REVIEW:",
            id="multi-line-title",
        ),
        # The comment published the findings: the parser's publication rules
        # are its rules for these fields too (#162).
        pytest.param(
            _set_finding(title="thanks @octocat"),
            "findings[0] is invalid: REVIEW: field 'R1-F1.title'",
            id="mention",
        ),
        pytest.param(
            _set_finding(required_resolution="Closes #3"),
            "findings[0] is invalid: REVIEW: field 'R1-F1.required_resolution'",
            id="closing-keyword",
        ),
        pytest.param(
            _set_finding(title="<!-- ai-review-result: x -->"),
            "findings[0] is invalid: REVIEW: field 'R1-F1.title'",
            id="marker",
        ),
        pytest.param(
            _set_finding(location="ghp_" + "a" * 36),
            "findings[0] is invalid: REVIEW: field 'R1-F1.location'",
            id="credential",
        ),
    ],
)
def test_review_context_findings_not_in_the_parsers_stored_form_are_refused(
    tmp_path, mutate, needle
):
    """D4.6: a stored finding is exactly what the parser returns for it, and
    the next FIX round's open findings come from it; one the parser would
    refuse or store differently is corruption, never normalized on load."""
    d = _review_state().to_dict()
    mutate(d["completion_context"]["findings"][0])
    p = _write(tmp_path / "state.json", d)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"REVIEW completion context.{needle}" in str(exc.value)
    assert "a" * 36 not in str(exc.value)


def test_records_persisted_without_their_completion_context_are_refused(tmp_path):
    """D4.6: records are written in the same save as the context they complete;
    records without one could never complete the phase, and are corruption."""
    s = _update_epic_state(completion_context={})
    p = tmp_path / "state.json"
    save_state(s, p)
    with pytest.raises(StateError, match="are persisted without the completion context"):
        load_state(p)


def test_effect_records_over_the_per_plan_count_are_refused(tmp_path):
    """D2.4: a plan holds at most one push, one effect per deferred finding and
    (#243) the operator comments of one more finding; a longer list is refused
    before any record in it is parsed."""
    assert MAX_EFFECTS_PER_PLAN == 54
    s = _update_epic_state()
    s.effect_records = [{}] * (MAX_EFFECTS_PER_PLAN + 1)
    p = tmp_path / "state.json"
    save_state(s, p)
    with pytest.raises(
        StateError, match=f"holds {MAX_EFFECTS_PER_PLAN + 1} records, over {MAX_EFFECTS_PER_PLAN}"
    ):
        load_state(p)


def _big_fix_state(count):
    """A FIX entry deferring ``count`` findings, each to a ~60K-character follow-up."""
    filler = "word " * 12_000
    records = [_follow_up_record(i, f"R1-F{i + 1}", text=filler.rstrip()) for i in range(count)]
    context = _fix_context([(f"R1-F{i + 1}", "follow_up_created", "", i) for i in range(count)])
    state = make_state(
        phase=Phase.FIX,
        current_pr_url=PR42,
        review_round=1,
        open_findings=[
            {"id": f"R1-F{i + 1}", "classification": "nit", "required_resolution": "x"}
            for i in range(count)
        ],
        attempt=1,
        launch_label=LABEL_AGENT_PUBLISHES,
        effect_records=[r.to_dict() for r in records],
        completion_context=context.to_dict(),
    )
    return state, payload_chars(records) + FixContext.STORED_BOUND


def test_effect_state_over_the_total_character_bound_is_refused(tmp_path):
    """D2.4: the plan's payload characters plus its context's stored bound stay
    within MAX_EFFECT_STATE_CHARS. One more follow-up than fits is refused on
    load with the total named; the plan just under the bound loads."""
    fits, fits_total = _big_fix_state(11)
    over, over_total = _big_fix_state(12)
    assert fits_total <= MAX_EFFECT_STATE_CHARS < over_total

    p = tmp_path / "fits.json"
    save_state(fits, p)
    assert len(load_state(p).phase_effects().records) == 11

    p = tmp_path / "over.json"
    save_state(over, p)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert (
        f"the effect plan holds {over_total} characters with its completion context, over "
        f"the bound of {MAX_EFFECT_STATE_CHARS}"
    ) in str(exc.value)


# -- LOCAL mode carries no effect state ----------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("effect_records", [{}]),
        ("effect_records", "UPDATE_EPIC"),
        ("entry_observation", _observation(Phase.REVIEW)),
        ("completion_context", {"phase": "REVIEW"}),
        ("launch_label", LABEL_AGENT_PUBLISHES),
        ("launch_label", LABEL_CONTROLLER_PUBLISHES),
    ],
)
def test_a_local_run_carrying_any_effect_state_is_refused(tmp_path, field, value):
    """D13.2: LOCAL performs no external effects, so a LOCAL state carrying any
    effect field (or a launch label) is corruption, not a run to resume."""
    d = _local_state().to_dict()
    d[field] = value
    p = _write(tmp_path / "state.json", d)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"state field(s) '{field}' are set on a LOCAL run" in str(exc.value)


def test_a_local_run_with_empty_effect_fields_loads(tmp_path):
    """D13.2: the empty values are what every protocol-7 LOCAL save writes."""
    p = tmp_path / "state.json"
    save_state(_local_state(attempt=1), p)
    loaded = load_state(p)
    assert _effect_fields(loaded) == {
        "effect_records": [],
        "entry_observation": {},
        "completion_context": {},
        "launch_label": LABEL_NONE,
    }
    assert loaded.phase_effects().empty


# -- protocol migration (D13.1, D13.2) ---------------------------------------------------


def _legacy(protocol, **kw):
    """The dict an older controller wrote: no protocol-7, -8, -9, -10, -11 or -12 field at all."""
    d = make_state(**kw).to_dict()
    d["protocol_version"] = protocol
    for key in (
        *_EFFECT_KEYS,
        "acceptance_outcomes",
        "config_fingerprint",
        *_VALIDATION_KEYS,
        *_OPERATOR_KEYS,
        *_PIN_KEYS,
    ):
        del d[key]
    return d


@pytest.mark.parametrize("protocol", ["1", "2", "3", "4", "5"])
def test_a_legacy_file_loads_with_no_effect_state_and_is_relabelled_on_save(tmp_path, protocol):
    """D13.2: protocol 5 (the label before effects) and the older legacy labels
    load with no records, no observation and no context, and the next save writes
    the current protocol with the four fields at their empty values."""
    p = _write(
        tmp_path / "state.json",
        _legacy(protocol, phase=Phase.REVIEW, current_pr_url=PR42, review_round=1),
    )
    loaded = load_state(p)
    assert loaded.protocol_version == "12"
    assert loaded.phase_effects().empty
    assert _effect_fields(loaded) == {
        "effect_records": [],
        "entry_observation": {},
        "completion_context": {},
        "launch_label": LABEL_NONE,
    }
    save_state(loaded, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12"
    assert raw["acceptance_outcomes"] == [] and raw["config_fingerprint"] == {}
    assert raw["validation_records"] == [] and raw["validation_override_seq"] is None
    assert raw["operator_comment_outcomes"] == []
    assert _effect_fields(raw) == _effect_fields(loaded)
    assert load_state(p) == loaded


@pytest.mark.parametrize(
    ("phase", "attempt", "label"),
    [
        (Phase.UPDATE_EPIC, 1, LABEL_AGENT_PUBLISHES),
        (Phase.FIX, 2, LABEL_AGENT_PUBLISHES),
        (Phase.REVIEW, 1, LABEL_AGENT_PUBLISHES),
        (Phase.ANALYZE_EXECUTE, 1, LABEL_AGENT_PUBLISHES),
        (Phase.REPLAN_REEXECUTE, 1, LABEL_AGENT_PUBLISHES),
        (Phase.UPDATE_EPIC, 0, LABEL_NONE),
        (Phase.INITIALIZING, 1, LABEL_NONE),
        (Phase.BLOCKED, 1, LABEL_NONE),
        (Phase.DONE, 1, LABEL_NONE),
    ],
)
def test_a_legacy_remote_resume_is_labelled_by_the_contract_it_launched_under(
    tmp_path, phase, attempt, label
):
    """D13.2/D13.3: a protocol-5 REMOTE file with ``attempt >= 1`` in a publishing
    phase launched under the agent-publishing contract and is labelled
    ``agent_publishes``; before a launch, or outside a publishing phase, it gets
    no label. The label survives the save that relabels the file. A legacy
    re-entry is one into a phase the controller now publishes for: UPDATE_EPIC
    (#160), ANALYZE_EXECUTE (#161), REVIEW (#162), FIX (#163) and
    REPLAN_REEXECUTE (#164)."""
    from autoforge.effects import is_legacy_reentry

    p = _write(
        tmp_path / "state.json",
        _legacy("5", phase=phase, attempt=attempt, current_pr_url=PR42),
    )
    loaded = load_state(p)
    assert loaded.launch_label == label
    assert loaded.phase_effects().empty
    assert is_legacy_reentry(loaded.phase, loaded.attempt, loaded.launch_label) == (
        phase
        in (
            Phase.ANALYZE_EXECUTE,
            Phase.REVIEW,
            Phase.FIX,
            Phase.REPLAN_REEXECUTE,
            Phase.UPDATE_EPIC,
        )
        and attempt >= 1
    )
    save_state(loaded, p)
    assert json.loads(p.read_text(encoding="utf-8"))["launch_label"] == label
    assert load_state(p).launch_label == label


def test_a_legacy_local_state_gets_no_launch_label(tmp_path):
    """D13.2: the label is a REMOTE concept; a protocol-5 LOCAL file mid-launch
    loads with no label, so its protocol-7 save stays loadable as LOCAL."""
    d = _local_state(attempt=1).to_dict()
    d["protocol_version"] = "5"
    for key in (
        *_EFFECT_KEYS,
        "acceptance_outcomes",
        "config_fingerprint",
        *_VALIDATION_KEYS,
        *_OPERATOR_KEYS,
        *_PIN_KEYS,
    ):
        del d[key]
    p = _write(tmp_path / "state.json", d)
    loaded = load_state(p)
    assert loaded.launch_label == LABEL_NONE
    save_state(loaded, p)
    assert load_state(p).launch_label == LABEL_NONE


@pytest.mark.parametrize("protocol", ["4", "5"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("effect_records", [_progress_record().to_dict()]),
        ("effect_records", None),
        ("entry_observation", _observation(Phase.UPDATE_EPIC)),
        ("completion_context", {"phase": "UPDATE_EPIC"}),
        ("launch_label", LABEL_AGENT_PUBLISHES),
        ("launch_label", LABEL_CONTROLLER_PUBLISHES),
    ],
)
def test_a_legacy_file_carrying_effect_state_is_refused_and_left_unchanged(
    tmp_path, protocol, field, value, local_fields
):
    """D13.2: a legacy label never wrote effect state, so a legacy file carrying
    any non-empty effect field is a hand edit or a foreign file: refused, never
    merged, and the file is not rewritten."""
    d = local_fields(_legacy(protocol, phase=Phase.UPDATE_EPIC, attempt=1, current_pr_url=PR42))
    d[field] = value
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"labelled protocol_version {protocol!r}, which never wrote {field!r}" in str(exc.value)
    assert p.read_bytes() == before


@pytest.mark.parametrize(
    ("protocol", "missing"),
    [("7", key) for key in _EFFECT_KEYS]
    + [("8", key) for key in (*_EFFECT_KEYS, "acceptance_outcomes")],
)
@pytest.mark.parametrize("stage", [Stage.INTENDED, Stage.ATTEMPTED])
def test_a_protocol_7_or_8_file_missing_an_effect_field_is_refused_and_left_unchanged(
    tmp_path, protocol, missing, stage, local_fields
):
    """D13.2 loads only a legacy label with no effect state. A protocol-7 save
    writes all four fields, and a protocol-8 one ``acceptance_outcomes`` too
    (#211), so one that is absent is corruption: read as its empty default, a
    missing ``effect_records`` would turn a pending progress comment into no
    plan at all, and the phase would complete from its context without
    publishing it."""
    d = _update_epic_state(stage).to_dict()
    assert d["protocol_version"] == "12"
    d["protocol_version"] = protocol
    del d["config_fingerprint"]  # protocol 9's own field (#219)
    _drop_validation(d)  # protocol 10's own fields (#172)
    _drop_operator(d)  # protocol 11's own field (#243)
    if protocol == "7":
        del d["acceptance_outcomes"]
    del d[missing]
    p = _write(tmp_path / "state.json", local_fields(d))
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    needle = f"labelled protocol_version {protocol!r} but is missing required field(s) {missing!r}"
    assert needle in str(exc.value)
    assert p.read_bytes() == before


def test_an_unlabelled_file_missing_an_effect_field_is_refused(tmp_path, local_fields):
    """Every controller has written ``protocol_version``; a file without one is
    read as the current protocol, so deleting the label as well as
    ``effect_records`` does not turn a pending progress comment into no plan."""
    d = _update_epic_state(Stage.INTENDED).to_dict()
    del d["protocol_version"], d["effect_records"]
    p = _write(tmp_path / "state.json", local_fields(d))
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert "is missing required field(s) 'effect_records'" in str(exc.value)


@pytest.mark.parametrize("missing", _EFFECT_KEYS)
def test_a_protocol_7_local_file_missing_an_effect_field_is_refused(tmp_path, missing):
    """LOCAL carries no effect state, but every protocol-7 LOCAL save still writes
    the four fields at their empty values; one that is absent is not that save."""
    d = _local_state().to_dict()
    del d[missing]
    p = _write(tmp_path / "state.json", d)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"is missing required field(s) {missing!r}" in str(exc.value)


def test_a_legacy_file_with_effect_fields_at_their_empty_values_loads(tmp_path):
    """D13.2: the label decides, not the shape -- empty values are no effect state."""
    d = _legacy("5", phase=Phase.UPDATE_EPIC, attempt=1, current_pr_url=PR42)
    d.update(effect_records=[], entry_observation={}, completion_context={}, launch_label="")
    loaded = load_state(_write(tmp_path / "state.json", d))
    assert loaded.protocol_version == "12"
    assert loaded.phase_effects().empty
    assert loaded.launch_label == LABEL_AGENT_PUBLISHES


@pytest.mark.parametrize("protocol", ["6", "13", 7, 8, 9, 10, 11, 12])
def test_protocol_6_and_other_unknown_labels_are_refused_and_left_unchanged(
    tmp_path, protocol, local_fields
):
    """D13.1: protocol 6 is skipped for good (it was once reserved for #126) and
    was never written by any released controller, so it is not a legacy label
    this controller migrates; it is refused exactly like a future label (and a
    non-string label), and the file is not rewritten. Pinned so that adopting 6
    is a deliberate change."""
    d = local_fields(make_state().to_dict())
    d["protocol_version"] = protocol
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"unsupported protocol_version {protocol!r} (controller speaks '12')" in str(exc.value)
    assert p.read_bytes() == before


_EVERY_LABEL = [*(str(n) for n in range(1, 14)), None]


@pytest.mark.parametrize("label", _EVERY_LABEL, ids=lambda label: f"label-{label}")
@pytest.mark.parametrize("mode", ["SATELLITE", "remote", "", None, 1, ["REMOTE"]], ids=repr)
def test_a_mode_other_than_remote_is_refused_under_every_label_and_left_unchanged(
    tmp_path, mode, label, local_fields
):
    """ADR 0009 D4 and D8 step 1 (#385): ``mode`` absent or ``"REMOTE"`` loads,
    and any other value, a non-string one included, is refused with the
    loader's own error. The mode is read before the label, so it decides
    under every label: one that loads today, 6, one no controller wrote yet
    (``13``) and none."""
    d = local_fields(make_state(phase=Phase.REVIEW).to_dict())
    d["mode"] = mode
    if label is None:
        del d["protocol_version"]
    else:
        d["protocol_version"] = label
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert str(exc.value) == f"state file has unknown mode {mode!r}"
    assert p.read_bytes() == before


def test_the_local_fields_are_the_twelve_every_remote_save_writes_empty():
    """``conftest.LOCAL_STATE_FIELDS`` names exactly LOCAL's twelve fields
    (ADR 0009 §1.1) at the value a REMOTE save writes, so the pins that run
    with and without them (#385) test the file a REMOTE run has today."""
    from dataclasses import fields

    saved = make_state().to_dict()
    assert {name: saved[name] for name in LOCAL_STATE_FIELDS} == LOCAL_STATE_FIELDS
    defaults = {
        f.name: f.default_factory() if callable(f.default_factory) else f.default
        for f in fields(AutoForgeState)
    }
    assert {name: defaults[name] for name in LOCAL_STATE_FIELDS} == LOCAL_STATE_FIELDS
    names = [f.name for f in fields(AutoForgeState)]
    first = names.index("feature_spec_path")
    assert names[first : first + len(LOCAL_STATE_FIELDS)] == list(LOCAL_STATE_FIELDS)
    assert len(LOCAL_STATE_FIELDS) == 12


# -- record lifetime: settle_phase_effects (D2.4) ------------------------------------------


@pytest.mark.parametrize("leave_to", [Phase.DONE, Phase.ANALYZE_EXECUTE, Phase.REVIEW])
def test_settling_drops_the_effect_state_of_a_phase_the_run_has_left(leave_to):
    """D2.4: records, observation and context belong to one phase entry; a save
    in another phase drops all three, and the launch label with them."""
    s = _update_epic_state(Stage.OBSERVED, phase=leave_to, attempt=0)
    s.settle_phase_effects()
    assert _effect_fields(s) == {
        "effect_records": [],
        "entry_observation": {},
        "completion_context": {},
        "launch_label": LABEL_NONE,
    }


@pytest.mark.parametrize("phase", [Phase.BLOCKED, Phase.FAILED])
@pytest.mark.parametrize("attempt", [0, 1])
def test_settling_keeps_effect_state_while_blocked_or_failed(tmp_path, phase, attempt):
    """D2.4: BLOCKED and FAILED keep the stopped entry's effect state whole --
    label included, whatever the attempt count -- for the operator and for
    ``unblock`` back into the phase; the kept state still loads."""
    s = _update_epic_state(Stage.CONFLICT, phase=phase, attempt=attempt)
    before = _effect_fields(s)
    s.settle_phase_effects()
    assert _effect_fields(s) == before
    p = tmp_path / "state.json"
    save_state(s, p)
    assert _effect_fields(load_state(p)) == before


def test_settling_in_the_entry_phase_keeps_records_and_drops_the_label_only_before_a_launch():
    """D2.4, D13.3: a save in the phase the pieces name keeps them; the label
    describes this entry's launches, so it is dropped while ``attempt`` is 0
    (every transition resets it) and kept once a launch was made."""
    launched = _update_epic_state(attempt=1)
    before = _effect_fields(launched)
    launched.settle_phase_effects()
    assert _effect_fields(launched) == before

    fresh = _update_epic_state(attempt=0)
    fresh.settle_phase_effects()
    assert fresh.effect_records == before["effect_records"]
    assert fresh.entry_observation == before["entry_observation"]
    assert fresh.completion_context == before["completion_context"]
    assert fresh.launch_label == LABEL_NONE

    bare = make_state(phase=Phase.FIX, attempt=0, launch_label=LABEL_AGENT_PUBLISHES)
    bare.settle_phase_effects()
    assert bare.launch_label == LABEL_NONE
    bare = make_state(phase=Phase.FIX, attempt=1, launch_label=LABEL_AGENT_PUBLISHES)
    bare.settle_phase_effects()
    assert bare.launch_label == LABEL_AGENT_PUBLISHES


@pytest.mark.parametrize(
    "pieces",
    [
        {"completion_context": _update_epic_context().to_dict()},
        {"entry_observation": _observation(Phase.UPDATE_EPIC)},
        {
            "effect_records": [_progress_record().to_dict()],
            "completion_context": _update_epic_context().to_dict(),
            "entry_observation": _observation(Phase.FIX),
        },
    ],
    ids=["lone-context", "lone-observation", "mixed-phases"],
)
def test_settling_drops_any_piece_naming_another_phase(pieces):
    """D2.4: one piece of another phase is enough -- a lone context, a lone
    observation, or pieces that disagree with each other -- and all are dropped."""
    s = make_state(
        phase=Phase.UPDATE_EPIC if "effect_records" in pieces else Phase.FIX,
        current_pr_url=PR42,
        attempt=1,
        launch_label=LABEL_AGENT_PUBLISHES,
        **pieces,
    )
    s.settle_phase_effects()
    assert _effect_fields(s) == {
        "effect_records": [],
        "entry_observation": {},
        "completion_context": {},
        "launch_label": LABEL_NONE,
    }


def test_the_engine_save_settles_effect_state_and_save_state_alone_does_not(tmp_path):
    """D2.4: the drop happens in ``ControllerEngine.save`` (every engine save),
    not in the low-level ``save_state``. A transition saved through the engine
    leaves a loadable file with no effect state; the same state written by
    ``save_state`` keeps records of a phase the run has left, which the load
    then refuses -- so a writer bypassing the engine cannot leak them silently."""
    from autoforge.config import default_config
    from autoforge.engine import ControllerEngine

    left = _update_epic_state(Stage.OBSERVED, phase=Phase.DONE, attempt=0)
    raw = tmp_path / "raw.json"
    save_state(left, raw)
    with pytest.raises(StateError, match="is bound to UPDATE_EPIC but the state is in DONE"):
        load_state(raw)

    engine = ControllerEngine(default_config(), state_dir=tmp_path / "state")
    try:
        engine.state = _update_epic_state(Stage.OBSERVED, phase=Phase.DONE, attempt=0)
        engine.save()
        loaded = load_state(engine.paths.state_file)
    finally:
        engine.close()
    assert loaded.phase == Phase.DONE
    assert loaded.phase_effects().empty
    assert loaded.launch_label == LABEL_NONE


def test_reset_for_new_issue_drops_the_effect_state():
    """D2.4: moving to another issue ends every entry of the previous one."""
    s = _update_epic_state(Stage.OBSERVED)
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert _effect_fields(s) == {
        "effect_records": [],
        "entry_observation": {},
        "completion_context": {},
        "launch_label": LABEL_NONE,
    }


# -- the launch label (D13.3) --------------------------------------------------------------


@pytest.mark.parametrize("label", [LABEL_NONE, LABEL_AGENT_PUBLISHES, LABEL_CONTROLLER_PUBLISHES])
def test_every_launch_label_in_the_closed_set_round_trips(tmp_path, label):
    """D13.3: the label is one of exactly three values."""
    p = tmp_path / "state.json"
    save_state(make_state(phase=Phase.UPDATE_EPIC, attempt=1, launch_label=label), p)
    assert load_state(p).launch_label == label


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("agent", "state field 'launch_label' must be one of"),
        ("AGENT_PUBLISHES", "state field 'launch_label' must be one of"),
        (" agent_publishes", "state field 'launch_label' must be one of"),
        ("controller", "state field 'launch_label' must be one of"),
        (1, "state field 'launch_label' must be str"),
        (None, "state field 'launch_label' must be str"),
        (["agent_publishes"], "state field 'launch_label' must be str"),
    ],
)
def test_an_invalid_launch_label_is_refused_on_load(tmp_path, value, needle):
    """D13.3: a label outside the closed set (or not a string) is corruption."""
    d = make_state(phase=Phase.UPDATE_EPIC, attempt=1).to_dict()
    d["launch_label"] = value
    p = _write(tmp_path / "state.json", d)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert needle in str(exc.value)


# -- acceptance-action state (#211) --------------------------------------------------------

ACTION_ID = "A-" + "0123456789abcdef"
ACCEPTANCE_COMMENT_URL = f"{EFFECT_ISSUE}#issuecomment-5"


def _outcome(**kw):
    """One ``acceptance_outcomes`` entry (declined unless ``kw`` says otherwise)."""
    entry = {
        "action_id": ACTION_ID,
        "round": 1,
        "finding_id": "R1-F1",
        "outcome": "declined",
        "at": "2026-01-01T00:00:00+00:00",
        "comment_url": "",
        "reason": "the comment overstates the test coverage",
    }
    entry.update(kw)
    return entry


FIXED_ENTRY = {
    "finding_id": "R1-F2",
    "resolution": "fixed",
    "rationale": "",
    "follow_up_issue_url": "",
    "commit_sha": CANDIDATE_SHA,
    "acceptance_comment_url": "",
    "operator_evidence": [],
}
ACCEPTED_ENTRY = {
    "finding_id": "R1-F1",
    "resolution": "acceptance_action",
    "rationale": "The finding asks for evidence on the issue, not for a code change.",
    "follow_up_issue_url": "",
    "commit_sha": "",
    "acceptance_comment_url": ACCEPTANCE_COMMENT_URL,
    "operator_evidence": [],
}
UNBLOCK_ENTRY = {
    "at": "2026-01-01T00:00:00+00:00",
    "reason": "approved",
    "block_reason": "awaits operator action",
    "phase": "FIX",
    "detail": "approved",
    "action": f"approved {ACTION_ID}",
}


def _drop_operator_evidence(entry: dict) -> dict:
    """A copy of a ``last_fix_resolutions`` entry as a controller before protocol 11 wrote it."""
    return {k: v for k, v in entry.items() if k != "operator_evidence"}


def _after_fix(**kw):
    """A REMOTE run in REVIEW after a FIX round with an acceptance action."""
    return make_state(
        phase=Phase.REVIEW,
        current_pr_url=PR42,
        review_round=1,
        last_fix_resolutions=[dict(ACCEPTED_ENTRY), dict(FIXED_ENTRY)],
        acceptance_outcomes=[_outcome(outcome="verified", comment_url=ACCEPTANCE_COMMENT_URL)],
        unblock_history=[dict(UNBLOCK_ENTRY)],
        **kw,
    )


def _as_protocol_7(d: dict) -> dict:
    """``d`` as a protocol-7 controller wrote it: no acceptance-action field."""
    d["protocol_version"] = "7"
    del d["acceptance_outcomes"]
    del d["config_fingerprint"]
    _drop_validation(d)
    _drop_operator(d)
    for r in d["completion_context"].get("resolutions", []):
        del r["acceptance_source"]
    for r in d["last_fix_resolutions"]:
        del r["acceptance_comment_url"]
    return d


def test_acceptance_state_round_trips_under_protocol_8(tmp_path):
    p = tmp_path / "state.json"
    save_state(_after_fix(), p)
    loaded = load_state(p)
    assert loaded.acceptance_outcomes[0]["comment_url"] == ACCEPTANCE_COMMENT_URL
    assert loaded.last_fix_resolutions[0]["acceptance_comment_url"] == ACCEPTANCE_COMMENT_URL
    assert loaded.unblock_history[0]["action"] == f"approved {ACTION_ID}"
    assert loaded == _after_fix(updated_at=loaded.updated_at)


def test_a_protocol_7_file_loads_migrated_and_is_relabelled_on_save(tmp_path):
    """Protocol 7 loads: its FIX plan's resolutions name no acceptance
    comment and no operator comment, its recorded resolutions verified none,
    and it has no outcomes. The next save writes the current protocol with
    those values."""
    d = _fix_state().to_dict()
    d["last_fix_resolutions"] = [dict(FIXED_ENTRY)]
    p = _write(tmp_path / "state.json", _as_protocol_7(d))
    loaded = load_state(p)
    assert loaded.protocol_version == "12" and loaded.acceptance_outcomes == []
    assert loaded.operator_comment_outcomes == []
    assert loaded.last_fix_resolutions == [FIXED_ENTRY]
    context = loaded.phase_effects().context
    assert isinstance(context, FixContext)
    assert all(r["acceptance_source"] is None for r in context.resolutions)
    assert all(r["operator_sources"] is None for r in context.resolutions)
    save_state(loaded, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12" and raw["acceptance_outcomes"] == []
    assert load_state(p) == loaded


@pytest.mark.parametrize("protocol", ["5", "7"])
@pytest.mark.parametrize(
    ("plant", "named"),
    [
        (lambda d: d.update(acceptance_outcomes=[_outcome()]), "'acceptance_outcomes'"),
        (lambda d: d.update(acceptance_outcomes=[]), "'acceptance_outcomes'"),
        (lambda d: d.update(unblock_history=[dict(UNBLOCK_ENTRY)]), "an 'action' in"),
        (
            lambda d: d.update(last_fix_resolutions=[_drop_operator_evidence(ACCEPTED_ENTRY)]),
            "'acceptance_comment_url' in 'last_fix_resolutions'",
        ),
    ],
)
def test_an_older_file_carrying_acceptance_state_is_refused_and_left_unchanged(
    tmp_path, protocol, plant, named
):
    """No controller before protocol 8 wrote acceptance-action state, so an
    older label carrying any is a hand edit or a foreign file: refused, never
    merged, and the file is not rewritten. The label decides, not the value:
    ``acceptance_outcomes`` is refused even as the empty list a protocol-8
    save writes (PR #221 R1-F3)."""
    if protocol == "7":
        d = make_state(phase=Phase.REVIEW, current_pr_url=PR42).to_dict()
        d["protocol_version"] = "7"
        del d["acceptance_outcomes"], d["config_fingerprint"]
        _drop_validation(d)
        _drop_operator(d)
    else:
        d = _legacy(protocol, phase=Phase.REVIEW, current_pr_url=PR42)
    plant(d)
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"labelled protocol_version {protocol!r}, which never wrote" in str(exc.value)
    assert named in str(exc.value)
    assert p.read_bytes() == before


@pytest.mark.parametrize("planted", ["record", "source"])
def test_a_protocol_7_plan_carrying_an_acceptance_comment_is_refused(
    tmp_path, planted, local_fields
):
    d = local_fields(_as_protocol_7(_fix_state().to_dict()))
    if planted == "record":
        d["effect_records"][0]["kind"] = "acceptance_comment"
        named = "an 'acceptance_comment' effect record"
    else:
        d["completion_context"]["resolutions"][0]["acceptance_source"] = 0
        named = "'acceptance_source' in 'completion_context'"
    p = _write(tmp_path / "state.json", d)
    with pytest.raises(StateError, match="labelled protocol_version '7', which never wrote") as e:
        load_state(p)
    assert named in str(e.value)


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        ([_outcome(note="x")], "must hold exactly"),
        ([_outcome(action_id="A-123")], "action id of the form"),
        ([_outcome(round=0)], r"round must be an integer >= 1"),
        ([_outcome(round=True)], r"round must be an integer >= 1"),
        ([_outcome(finding_id="R2-F1")], "must be a finding id of its round"),
        ([_outcome(outcome="approved")], "outcome must be one of"),
        ([_outcome(at="")], "non-empty timestamp"),
        ([_outcome(reason="r" * (MAX_CONFLICT_REASON_CHARS + 1))], "at most"),
        ([_outcome(outcome="verified")], r"\.comment_url"),
        (
            [_outcome(outcome="verified", comment_url=f"{PR42}#issuecomment-5")],
            "not a comment on the current issue",
        ),
        ([_outcome(comment_url=ACCEPTANCE_COMMENT_URL)], "must be empty"),
        ([_outcome()] * (MAX_ACCEPTANCE_OUTCOMES + 1), "over"),
        (
            [_outcome()] * (MAX_DECLINES_PER_ROUND + 1),
            f"holds {MAX_DECLINES_PER_ROUND + 1} declines of review round 1, over",
        ),
        (["declined"], "must be a list of objects"),
    ],
)
def test_a_malformed_acceptance_outcome_is_refused(tmp_path, entries, match):
    d = make_state(phase=Phase.REVIEW, current_pr_url=PR42).to_dict()
    d["acceptance_outcomes"] = entries
    with pytest.raises(StateError, match=match):
        load_state(_write(tmp_path / "state.json", d))


def test_a_round_holding_the_most_declines_loads(tmp_path):
    d = make_state(phase=Phase.REVIEW, current_pr_url=PR42, review_round=2).to_dict()
    d["acceptance_outcomes"] = [_outcome()] * MAX_DECLINES_PER_ROUND + [
        _outcome(round=2, finding_id="R2-F1")
    ] * MAX_DECLINES_PER_ROUND
    assert len(load_state(_write(tmp_path / "state.json", d)).acceptance_outcomes) == 32


def _declined(round_: int, n: int) -> dict:
    """The ``n``-th distinct decline of ``round_``."""
    return _outcome(round=round_, finding_id=f"R{round_}-F1", action_id=f"A-{round_:08x}{n:08x}")


def test_the_outcome_bound_drops_earlier_rounds_never_a_decline_of_the_round_being_fixed():
    """PR #221 R3-F1: a decline re-enters FIX without advancing the round,
    so one round's declines run past the bound. The oldest entries go, an
    earlier round's declines among them, but never one of the current
    round, wherever it stands: it still refuses its action."""
    older = [_declined(1, n) for n in range(MAX_ACCEPTANCE_OUTCOMES)]
    s = make_state(phase=Phase.FIX, current_pr_url=PR42, review_round=2)
    s.acceptance_outcomes = list(older)
    active: list[dict] = []
    for n in range(MAX_DECLINES_PER_ROUND):
        active.append(_declined(2, n))
        s.acceptance_outcomes.append(active[-1])
        s.bound_acceptance_outcomes()
        assert len(s.acceptance_outcomes) == MAX_ACCEPTANCE_OUTCOMES
        assert s.acceptance_outcomes == older[n + 1 :] + active
    # The round's declines stay even as the oldest entries of the list.
    s.acceptance_outcomes = active + older
    s.acceptance_outcomes.append(_outcome(round=2, finding_id="R2-F1", outcome="stale"))
    s.bound_acceptance_outcomes()
    assert s.acceptance_outcomes[:MAX_DECLINES_PER_ROUND] == active
    assert s.acceptance_outcomes[MAX_DECLINES_PER_ROUND:-1] == older[MAX_DECLINES_PER_ROUND + 1 :]
    assert s.acceptance_outcomes[-1]["outcome"] == "stale"
    # Within the bound nothing is dropped.
    s.acceptance_outcomes = older[:3]
    s.bound_acceptance_outcomes()
    assert s.acceptance_outcomes == older[:3]


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({**ACCEPTED_ENTRY, "acceptance_comment_url": ""}, r"acceptance_comment_url"),
        ({**FIXED_ENTRY, "acceptance_comment_url": ACCEPTANCE_COMMENT_URL}, "must be empty"),
        ({k: v for k, v in FIXED_ENTRY.items() if k != "acceptance_comment_url"}, "is missing"),
        (
            {**ACCEPTED_ENTRY, "acceptance_comment_url": f"{PR42}#issuecomment-5"},
            "not a comment on the current issue",
        ),
    ],
)
def test_a_recorded_resolution_names_a_verified_comment_exactly_for_an_acceptance_action(
    tmp_path, entry, match
):
    d = make_state(phase=Phase.REVIEW, current_pr_url=PR42).to_dict()
    d["last_fix_resolutions"] = [entry]
    with pytest.raises(StateError, match=match):
        load_state(_write(tmp_path / "state.json", d))


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("acceptance_outcomes", [_outcome()], "'acceptance_outcomes' is set on a LOCAL run"),
        ("unblock_history", [UNBLOCK_ENTRY], "acceptance 'action' on a LOCAL run"),
    ],
)
def test_a_local_run_carries_no_acceptance_state(tmp_path, field, value, match):
    """LOCAL runs perform no external writes, so they have no acceptance actions."""
    d = _local_state().to_dict()
    d[field] = value
    with pytest.raises(StateError, match=match):
        load_state(_write(tmp_path / "state.json", d))


def test_acceptance_outcomes_belong_to_the_issue_they_were_recorded_for():
    state = _after_fix()
    state.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert state.acceptance_outcomes == [] and state.last_fix_resolutions == []


# -- the recorded configuration (#219, protocol 9) --------------------------------------


def _recorded_config(**over) -> dict:
    from autoforge.config import default_config
    from autoforge.config_fingerprint import ConfigFingerprint, RecordedConfiguration

    record = RecordedConfiguration(
        fingerprint=ConfigFingerprint.from_config(default_config(), "/srv/worktrees"),
        config_file="/home/me/autoforge.yaml",
        source="created",
        recorded_at="2026-10-14T00:00:00+00:00",
    ).to_dict()
    record.update(over)
    return record


def test_a_recorded_configuration_round_trips_under_the_current_protocol(tmp_path):
    p = tmp_path / "state.json"
    s = make_state(phase=Phase.REVIEW, config_fingerprint=_recorded_config())
    save_state(s, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12" and raw["config_fingerprint"] == _recorded_config()
    assert load_state(p) == s


def test_a_protocol_8_file_loads_with_no_recorded_configuration_and_is_relabelled(tmp_path):
    """No recorded configuration means "not checked": the file loads in any
    phase, its own acceptance state untouched (it is protocol 8's, not
    migrated), and the next save writes the current protocol with ``{}``."""
    d = _after_fix().to_dict()
    d["protocol_version"] = "8"
    del d["config_fingerprint"]
    _drop_validation(d)
    _drop_operator(d)
    p = _write(tmp_path / "state.json", d)
    loaded = load_state(p)
    assert loaded.protocol_version == "12" and loaded.config_fingerprint == {}
    assert loaded.last_fix_resolutions[0]["acceptance_comment_url"] == ACCEPTANCE_COMMENT_URL
    assert loaded.acceptance_outcomes == _after_fix().acceptance_outcomes
    save_state(loaded, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12" and raw["config_fingerprint"] == {}
    assert load_state(p) == loaded


@pytest.mark.parametrize("protocol", ["4", "5", "7", "8"])
@pytest.mark.parametrize("value", [{}, _recorded_config()], ids=["empty", "recorded"])
def test_an_older_file_carrying_a_recorded_configuration_is_refused_and_left_unchanged(
    tmp_path, protocol, value
):
    """No controller before protocol 9 wrote ``config_fingerprint``: an older
    label carrying it, even empty, is a hand edit or a foreign file."""
    if protocol in ("4", "5"):
        d = _legacy(protocol, phase=Phase.REVIEW, current_pr_url=PR42)
    else:
        d = make_state(phase=Phase.REVIEW, current_pr_url=PR42).to_dict()
        d["protocol_version"] = protocol
        _drop_validation(d)
        _drop_operator(d)
        if protocol == "7":
            del d["acceptance_outcomes"]
    d["config_fingerprint"] = value
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"labelled protocol_version {protocol!r}, which never wrote 'config_fingerprint'" in str(
        exc.value
    )
    assert p.read_bytes() == before


@pytest.mark.parametrize("protocol", ["9", "10", "11", "12"])
def test_a_protocol_9_file_missing_its_recorded_configuration_is_refused(
    tmp_path, protocol, local_fields
):
    """Every protocol-9 save writes the field, ``{}`` included, and so does
    every later one: read as absent it would switch the check off for the
    rest of the run."""
    d = make_state(config_fingerprint=_recorded_config()).to_dict()
    del d["config_fingerprint"]
    if protocol in ("9", "10"):
        d["protocol_version"] = protocol
        _drop_operator(d)
    if protocol == "11":
        d["protocol_version"] = protocol
        _drop_pin(d)
    if protocol == "9":
        _drop_validation(d)
    p = _write(tmp_path / "state.json", local_fields(d))
    with pytest.raises(StateError) as exc:
        load_state(p)
    message = str(exc.value)
    assert f"labelled protocol_version {protocol!r}" in message
    assert "missing required field(s) 'config_fingerprint'" in message
    assert "not a run with none recorded" in message


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ([], "state field 'config_fingerprint' must be an object"),
        ("x", "state field 'config_fingerprint' must be an object"),
        ({"schema": 1}, "state field 'config_fingerprint': the recorded configuration must hold"),
        (_recorded_config(schema=2), "state field 'config_fingerprint': schema 2 is not 1"),
        (_recorded_config(source=""), "state field 'config_fingerprint': source must be one of"),
        (
            _recorded_config(worktree_root="wt"),
            "state field 'config_fingerprint': worktree_root must be an absolute path",
        ),
    ],
)
def test_a_malformed_recorded_configuration_is_refused(tmp_path, value, needle):
    d = make_state().to_dict()
    d["config_fingerprint"] = value
    p = _write(tmp_path / "state.json", d)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert needle in str(exc.value)


def test_a_local_run_carrying_a_recorded_configuration_is_refused(tmp_path):
    d = _local_state().to_dict()
    d["config_fingerprint"] = _recorded_config()
    p = _write(tmp_path / "state.json", d)
    with pytest.raises(StateError, match="'config_fingerprint' is set on a LOCAL run"):
        load_state(p)


def test_a_new_issue_keeps_the_recorded_configuration():
    """The configuration is the run's, not the issue's."""
    s = make_state(config_fingerprint=_recorded_config())
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert s.config_fingerprint == _recorded_config()


# -- validation evidence (#172, protocol 10) --------------------------------------------


def _validation_record(seq: int = 1, *, outcome_code: int = 0) -> dict:
    from autoforge.evidence import (
        NO_SANDBOX_TEMPLATE,
        Definition,
        Policy,
        RemoteCandidate,
        WorktreeProvenance,
        observed_record,
    )
    from autoforge.executor import ExecutionResult

    return observed_record(
        seq=seq,
        candidate=RemoteCandidate(
            "owner/repo", "sha1", "a" * 40, "b" * 40, WorktreeProvenance(2, "2", True)
        ),
        definition=Definition(0, ("make", "test")),
        policy=Policy("disabled", NO_SANDBOX_TEMPLATE, ("PATH",), 600, True, "byte-exact"),
        result=ExecutionResult(
            command=["make", "test"],
            cwd="/x",
            exit_code=outcome_code,
            stdout="ok\n",
            stderr="",
            started_at="2026-10-15T00:00:00+00:00",
            finished_at="2026-10-15T00:00:01+00:00",
        ),
        run_log=f"af-test-1/{seq:04d}-analyze_execute-candidate-validation-1",
    )


def _validation_marker() -> dict:
    record = _validation_record()
    return {k: record[k] for k in ("candidate", "definition", "policy", "run_log")} | {
        "marked_at": "2026-10-15T00:00:00+00:00"
    }


def test_validation_evidence_round_trips_under_the_current_protocol(tmp_path):
    p = tmp_path / "state.json"
    s = make_state(
        phase=Phase.REVIEW,
        validation_records=[_validation_record(1), _validation_record(2, outcome_code=1)],
        validation_attempt=_validation_marker(),
        validation_override_seq=1,
    )
    save_state(s, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12"
    assert [r["outcome"] for r in raw["validation_records"]] == ["PASS", "FAIL"]
    loaded = load_state(p)
    assert loaded == s
    assert [v.seq for v in loaded.validation_evidence()] == [1, 2]


def test_loading_a_pending_marker_is_read_only(tmp_path):
    """D11.3: the load reports the marker; only a step converts it."""
    p = tmp_path / "state.json"
    save_state(make_state(validation_attempt=_validation_marker()), p)
    before = p.read_bytes()
    loaded = load_state(p)
    assert loaded.validation_attempt == _validation_marker()
    assert loaded.validation_records == []
    assert p.read_bytes() == before


@pytest.mark.parametrize("phase", list(Phase))
@pytest.mark.parametrize("protocol", ["5", "7", "8", "9"])
def test_an_older_file_loads_in_every_phase_with_no_validation_evidence(tmp_path, phase, protocol):
    """D15.4: a file from before protocol 10 loads with every field at its
    empty value, fabricating no record, marker, binding or certificate, and
    the next save writes the current protocol."""
    if protocol == "5":
        d = _legacy("5", phase=phase)
    else:
        d = _drop_operator(_drop_validation(make_state(phase=phase).to_dict()))
        d["protocol_version"] = protocol
        if protocol in ("7", "8"):
            del d["config_fingerprint"]
        if protocol == "7":
            del d["acceptance_outcomes"]
    p = _write(tmp_path / "state.json", d)
    loaded = load_state(p)
    assert loaded.protocol_version == "12" and loaded.phase == phase
    assert {key: getattr(loaded, key) for key in _VALIDATION_KEYS} == {
        "validation_records": [],
        "validation_attempt": {},
        "review_evidence": {},
        "review_certificate": {},
        "producer_validation_failure": {},
        "validation_override_seq": None,
    }
    save_state(loaded, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12" and raw["validation_records"] == []


@pytest.mark.parametrize("protocol", ["5", "7", "8", "9"])
@pytest.mark.parametrize("key", _VALIDATION_KEYS)
def test_an_older_file_carrying_validation_evidence_is_refused(
    tmp_path, protocol, key, local_fields
):
    """The label decides, not the value: protocol 10's fields are refused in
    an older file even at their empty values."""
    if protocol == "5":
        d = _legacy("5", phase=Phase.REVIEW)
    else:
        d = _drop_operator(_drop_validation(make_state(phase=Phase.REVIEW).to_dict()))
        d["protocol_version"] = protocol
        if protocol in ("7", "8"):
            del d["config_fingerprint"]
        if protocol == "7":
            del d["acceptance_outcomes"]
    d[key] = make_state().to_dict()[key]
    p = _write(tmp_path / "state.json", local_fields(d))
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"labelled protocol_version {protocol!r}, which never wrote {key!r}" in str(exc.value)
    assert p.read_bytes() == before


@pytest.mark.parametrize("protocol", ["10", "11", "12"])
@pytest.mark.parametrize("key", _VALIDATION_KEYS)
def test_a_protocol_10_file_missing_a_validation_field_is_refused(
    tmp_path, key, protocol, local_fields
):
    """Every protocol-10 save writes all six, and so does every later one."""
    d = make_state().to_dict()
    if protocol == "10":
        d["protocol_version"] = "10"
        _drop_operator(d)
    if protocol == "11":
        d["protocol_version"] = "11"
        _drop_pin(d)
    del d[key]
    p = _write(tmp_path / "state.json", local_fields(d))
    with pytest.raises(StateError) as exc:
        load_state(p)
    message = str(exc.value)
    needle = f"labelled protocol_version {protocol!r} but is missing required field(s) {key!r}"
    assert needle in message
    assert "not a run with no evidence" in message


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"validation_records": {}}, "must be a list"),
        ({"validation_attempt": []}, "must be an object"),
        ({"validation_override_seq": "1"}, "must be an integer"),
        ({"validation_override_seq": 1}, "above the highest retained record seq 0"),
        ({"review_certificate": {"kind": "remote"}}, "review_certificate"),
    ],
)
def test_malformed_validation_evidence_is_refused(tmp_path, change, needle):
    d = make_state().to_dict()
    d.update(change)
    p = _write(tmp_path / "state.json", d)
    with pytest.raises(StateError, match=needle):
        load_state(p)


def test_a_record_of_another_run_is_refused(tmp_path):
    record = _validation_record()
    d = make_state(run_id="af-test-2").to_dict()
    d["validation_records"] = [record]
    with pytest.raises(StateError, match="this run's log directory"):
        load_state(_write(tmp_path / "state.json", d))


def test_a_local_run_refuses_a_remote_record(tmp_path):
    d = _local_state().to_dict()
    d["validation_records"] = [_validation_record()]
    with pytest.raises(StateError, match="remote candidate in a LOCAL run"):
        load_state(_write(tmp_path / "state.json", d))


def test_validation_evidence_belongs_to_the_issue_it_was_recorded_for():
    s = make_state(
        validation_records=[_validation_record()],
        validation_attempt=_validation_marker(),
        validation_override_seq=1,
    )
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert {key: getattr(s, key) for key in _VALIDATION_KEYS} == {
        "validation_records": [],
        "validation_attempt": {},
        "review_evidence": {},
        "review_certificate": {},
        "producer_validation_failure": {},
        "validation_override_seq": None,
    }


# -- operator actions (#243, protocol 11) -------------------------------------------------
#
# ``operator_comment_outcomes`` records what became of each operator action
# (K10) of the current PR, and a REMOTE ``last_fix_resolutions`` entry's
# ``operator_evidence`` the operator comments verified for its finding. A
# target is another issue of the run's repository, never the run's own.

OPERATOR_ACTION_ID = "X-" + "0123456789abcdef"
OTHER_OPERATOR_ACTION_ID = "X-" + "fedcba9876543210"
OPERATOR_TARGET = "https://github.com/owner/repo/issues/5"
OPERATOR_COMMENT_URL = f"{OPERATOR_TARGET}#issuecomment-11"
OPERATOR_BODY_SHA256 = "c" * 64
OPERATOR_PAYLOAD_SHA256 = "e" * 64


def _operator_outcome(**kw):
    """One ``operator_comment_outcomes`` entry (declined unless ``kw`` says otherwise)."""
    entry = {
        "action_id": OPERATOR_ACTION_ID,
        "round": 1,
        "finding_id": "R1-F1",
        "target": OPERATOR_TARGET,
        "outcome": "declined",
        "evidence": "",
        "comment_url": "",
        "body_sha256": "",
        "payload_sha256": OPERATOR_PAYLOAD_SHA256,
        "at": "2026-01-01T00:00:00+00:00",
        "reason": "that issue is not the place for this note",
    }
    entry.update(kw)
    return entry


def _verified_outcome(**kw):
    """A ``verified`` entry: the operator's comment observed on its target."""
    return _operator_outcome(
        **{
            "outcome": "verified",
            "evidence": "exact",
            "comment_url": OPERATOR_COMMENT_URL,
            "body_sha256": OPERATOR_BODY_SHA256,
            "reason": "",
            **kw,
        }
    )


def _operator_evidence(**kw):
    """One ``operator_evidence`` item of a REMOTE ``last_fix_resolutions`` entry."""
    item = {
        "action_id": OPERATOR_ACTION_ID,
        "target": OPERATOR_TARGET,
        "comment_url": OPERATOR_COMMENT_URL,
        "evidence": "exact",
        "body_sha256": OPERATOR_BODY_SHA256,
        "payload_sha256": OPERATOR_PAYLOAD_SHA256,
    }
    item.update(kw)
    return item


OPERATOR_ENTRY = {
    "finding_id": "R1-F1",
    "resolution": "operator_acceptance",
    "rationale": "The finding asks for a note on another issue, which the operator posted.",
    "follow_up_issue_url": "",
    "commit_sha": "",
    "acceptance_comment_url": "",
    "operator_evidence": [_operator_evidence()],
}


def _after_operator_fix(**kw):
    """A REMOTE run in REVIEW after a FIX round with a verified operator action."""
    base = dict(
        phase=Phase.REVIEW,
        current_pr_url=PR42,
        review_round=1,
        last_fix_resolutions=[dict(OPERATOR_ENTRY), dict(FIXED_ENTRY)],
        operator_comment_outcomes=[_verified_outcome()],
    )
    base.update(kw)
    return make_state(**base)


def _as_protocol(d: dict, protocol: str) -> dict:
    """``d`` as the controller of an older ``protocol`` (7 to 10) wrote it."""
    if protocol == "7":
        return _as_protocol_7(d)
    d["protocol_version"] = protocol
    _drop_operator(d)  # protocol 11's own field (#243)
    if protocol in ("8", "9"):
        _drop_validation(d)  # protocol 10's own fields (#172)
    if protocol == "8":
        del d["config_fingerprint"]  # protocol 9's own field (#219)
    return d


def test_every_operator_outcome_round_trips_under_the_current_protocol(tmp_path):
    """A verified action (``exact`` or ``bound`` evidence), a declined, a
    withdrawn and a stale one, and the evidence a resolution names, survive
    save and load unchanged in a file labelled with the current protocol."""
    outcomes = [
        _verified_outcome(),
        _verified_outcome(
            action_id=OTHER_OPERATOR_ACTION_ID,
            evidence="bound",
            comment_url=f"{OPERATOR_TARGET}#issuecomment-12",
        ),
        _operator_outcome(),
        _operator_outcome(outcome="withdrawn", reason=""),
        _operator_outcome(outcome="stale", reason=""),
    ]
    assert sorted({o["outcome"] for o in outcomes}) == sorted(OPERATOR_COMMENT_OUTCOMES)
    p = tmp_path / "state.json"
    save_state(_after_operator_fix(operator_comment_outcomes=outcomes), p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12" and raw["operator_comment_outcomes"] == outcomes
    loaded = load_state(p)
    assert loaded.operator_comment_outcomes == outcomes
    assert loaded.last_fix_resolutions[0]["operator_evidence"] == [_operator_evidence()]
    assert loaded.last_fix_resolutions[1]["operator_evidence"] == []
    assert loaded == _after_operator_fix(
        operator_comment_outcomes=outcomes, updated_at=loaded.updated_at
    )


@pytest.mark.parametrize("phase", list(Phase))
def test_a_protocol_10_file_loads_in_every_phase_with_no_operator_state(tmp_path, phase):
    """A protocol-10 file is this protocol's shape without the operator-action
    state: it loads in every phase with no outcome, and the next save writes
    the current protocol with ``[]``."""
    d = _as_protocol(make_state(phase=phase, current_pr_url=PR42).to_dict(), "10")
    assert "operator_comment_outcomes" not in d
    p = _write(tmp_path / "state.json", d)
    loaded = load_state(p)
    assert loaded.protocol_version == "12" and loaded.phase == phase
    assert loaded.operator_comment_outcomes == []
    save_state(loaded, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12" and raw["operator_comment_outcomes"] == []
    assert load_state(p) == loaded


def test_a_protocol_10_fix_plan_loads_migrated_and_is_relabelled_on_save(tmp_path):
    """A protocol-10 FIX entry observed no operator targets, its plan's
    resolutions name no operator comment and its recorded resolutions
    verified none: it loads with ``operator_sources: null`` and
    ``operator_evidence: []``, and offers no operator action."""
    d = _fix_state().to_dict()
    d["last_fix_resolutions"] = [dict(FIXED_ENTRY)]
    p = _write(tmp_path / "state.json", _as_protocol(d, "10"))
    loaded = load_state(p)
    assert loaded.protocol_version == "12" and loaded.operator_comment_outcomes == []
    assert loaded.last_fix_resolutions == [FIXED_ENTRY]
    effects = loaded.phase_effects()
    assert isinstance(effects.context, FixContext)
    assert all(r["operator_sources"] is None for r in effects.context.resolutions)
    assert effects.observation is not None and effects.observation.operator_targets is None
    save_state(loaded, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12" and raw["operator_comment_outcomes"] == []
    assert raw["last_fix_resolutions"][0]["operator_evidence"] == []
    assert load_state(p) == loaded


def test_a_protocol_10_local_file_loads_and_is_relabelled(tmp_path):
    """LOCAL has no operator actions: a protocol-10 LOCAL file loads with no
    outcome, its resolutions gain no evidence, and the next save loads."""
    resolutions = [{"finding_id": "R1-F1", "resolution": "fixed"}]
    d = _as_protocol(_local_state(last_fix_resolutions=resolutions).to_dict(), "10")
    p = _write(tmp_path / "state.json", d)
    loaded = load_state(p)
    assert loaded.protocol_version == "12" and loaded.operator_comment_outcomes == []
    assert loaded.last_fix_resolutions == resolutions
    save_state(loaded, p)
    assert json.loads(p.read_text(encoding="utf-8"))["operator_comment_outcomes"] == []
    assert load_state(p) == loaded


@pytest.mark.parametrize("protocol", ["5", "7", "8", "9", "10"])
@pytest.mark.parametrize(
    ("plant", "named"),
    [
        (
            lambda d: d.update(operator_comment_outcomes=[_operator_outcome()]),
            "'operator_comment_outcomes'",
        ),
        (lambda d: d.update(operator_comment_outcomes=[]), "'operator_comment_outcomes'"),
        (
            lambda d: d.update(
                last_fix_resolutions=[
                    {k: v for k, v in FIXED_ENTRY.items() if k != "acceptance_comment_url"}
                ]
            ),
            "'operator_evidence' in 'last_fix_resolutions'",
        ),
    ],
    ids=["outcomes", "empty-outcomes", "evidence"],
)
def test_an_older_file_carrying_operator_state_is_refused_and_left_unchanged(
    tmp_path, protocol, plant, named
):
    """No controller before protocol 11 wrote operator-action state, so an
    older label carrying any is a hand edit or a foreign file: refused, never
    merged, and the file is not rewritten. The label decides, not the value:
    ``operator_comment_outcomes`` is refused even as the empty list a
    protocol-11 save writes, and ``operator_evidence`` even as ``[]``."""
    if protocol == "5":
        d = _legacy(protocol, phase=Phase.REVIEW, current_pr_url=PR42)
    else:
        d = _as_protocol(make_state(phase=Phase.REVIEW, current_pr_url=PR42).to_dict(), protocol)
    plant(d)
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    message = str(exc.value)
    assert f"labelled protocol_version {protocol!r}, which never wrote" in message
    assert named in message and "protocol-11 operator-action state" in message
    assert p.read_bytes() == before


@pytest.mark.parametrize("protocol", ["7", "8", "9", "10"])
@pytest.mark.parametrize("planted", ["record", "sources", "targets"])
def test_an_older_fix_plan_carrying_operator_state_is_refused_and_left_unchanged(
    tmp_path, protocol, planted
):
    """A K10 record, a context resolution's ``operator_sources`` (even null)
    and an entry observation's ``operator_targets`` are protocol 11's too."""
    d = _as_protocol(_fix_state().to_dict(), protocol)
    if planted == "record":
        d["effect_records"][0]["kind"] = "operator_comment"
        named = "an 'operator_comment' effect record"
    elif planted == "sources":
        d["completion_context"]["resolutions"][0]["operator_sources"] = None
        named = "'operator_sources' in 'completion_context'"
    else:
        d["entry_observation"]["operator_targets"] = None
        named = "'operator_targets' in 'entry_observation'"
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"labelled protocol_version {protocol!r}, which never wrote" in str(exc.value)
    assert named in str(exc.value)
    assert p.read_bytes() == before


def test_a_protocol_11_file_missing_its_operator_outcomes_is_refused(tmp_path, local_fields):
    """Every protocol-11 save writes the field, ``[]`` included, and so does
    every later one: read as the empty default, an absent one would drop the
    round's declined actions and let the same request through again. An
    unlabelled file is read as the current protocol."""
    older = local_fields(_drop_pin(_after_operator_fix().to_dict()))
    older["protocol_version"] = "11"
    del older["operator_comment_outcomes"]
    p = _write(tmp_path / "state.json", older)
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert (
        "labelled protocol_version '11' but is missing required field(s) "
        "'operator_comment_outcomes'"
    ) in str(exc.value)
    d = local_fields(_after_operator_fix().to_dict())
    del d["operator_comment_outcomes"]
    for label in ("labelled protocol_version '12'", "no protocol_version label (read as '12')"):
        if "no protocol_version" in label:
            del d["protocol_version"]
        p = _write(tmp_path / "state.json", d)
        before = p.read_bytes()
        with pytest.raises(StateError) as exc:
            load_state(p)
        message = str(exc.value)
        assert f"{label} but is missing required field(s) 'operator_comment_outcomes'" in message
        assert "not a PR with none" in message
        assert p.read_bytes() == before


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        ([_operator_outcome(note="x")], "must hold exactly"),
        (
            [{k: v for k, v in _operator_outcome().items() if k != "payload_sha256"}],
            "must hold exactly",
        ),
        ([_operator_outcome(action_id=ACTION_ID)], "action id of the form X-<16 hex>"),
        ([_operator_outcome(action_id="X-0123")], "action id of the form X-<16 hex>"),
        ([_operator_outcome(round=0)], r"round must be an integer >= 1"),
        ([_operator_outcome(round=True)], r"round must be an integer >= 1"),
        ([_operator_outcome(finding_id="R2-F1")], "must be a finding id of its round"),
        ([_operator_outcome(outcome="approved")], "outcome must be one of"),
        ([_operator_outcome(outcome="verified")], "evidence must be one of"),
        ([_verified_outcome(evidence="")], "evidence must be one of"),
        ([_verified_outcome(evidence="observed")], "evidence must be one of"),
        ([_verified_outcome(comment_url="")], r"\.comment_url: "),
        (
            [_verified_outcome(comment_url=f"{EFFECT_ISSUE}#issuecomment-11")],
            "is not a canonical comment URL on its target",
        ),
        (
            [
                _verified_outcome(
                    comment_url="https://GitHub.com/owner/repo/issues/5#issuecomment-11"
                )
            ],
            "is not a canonical comment URL on its target",
        ),
        ([_verified_outcome(body_sha256="")], r"body_sha256 must be a 64-character"),
        ([_verified_outcome(body_sha256="C" * 64)], r"body_sha256 must be a 64-character"),
        ([_operator_outcome(payload_sha256="")], r"payload_sha256 must be a 64-character"),
        ([_operator_outcome(comment_url=OPERATOR_COMMENT_URL)], "no operator comment was verified"),
        ([_operator_outcome(evidence="exact")], "evidence must be empty for a declined action"),
        (
            [_operator_outcome(outcome="withdrawn", evidence="bound")],
            "evidence must be empty for a withdrawn action",
        ),
        (
            [_operator_outcome(outcome="stale", comment_url=OPERATOR_COMMENT_URL)],
            "no operator comment was verified",
        ),
        (
            [_operator_outcome(outcome="stale", body_sha256=OPERATOR_BODY_SHA256)],
            r"body_sha256 must be empty",
        ),
        ([_operator_outcome(target=EFFECT_ISSUE)], "is the run's own issue"),
        (
            [_operator_outcome(target="https://github.com/Owner/Repo/issues/2")],
            "is the run's own issue",
        ),
        (
            [_operator_outcome(target="https://github.com/other/repo/issues/5")],
            "is not an issue of repository 'owner/repo'",
        ),
        (
            [_operator_outcome(target="https://github.com/owner/repo/issues/05")],
            "must be stored in its canonical form",
        ),
        ([_operator_outcome(target=PR42)], r"\.target: expected a GitHub issue URL"),
        ([_operator_outcome(at="")], "non-empty timestamp"),
        ([_operator_outcome(reason="r" * (MAX_CONFLICT_REASON_CHARS + 1))], "at most"),
        (
            [_verified_outcome(), _verified_outcome(evidence="bound")],
            rf"\[1\] verifies action {OPERATOR_ACTION_ID} on one comment twice",
        ),
        (
            [_operator_outcome(outcome="stale")] * (MAX_OPERATOR_COMMENT_OUTCOMES + 1),
            f"holds {MAX_OPERATOR_COMMENT_OUTCOMES + 1} entries, over "
            f"{MAX_OPERATOR_COMMENT_OUTCOMES}",
        ),
        (
            [_operator_outcome()] * (MAX_DECLINES_PER_ROUND + 1),
            f"hold {MAX_DECLINES_PER_ROUND + 1} declines of review round 1, over",
        ),
        (["declined"], "must be a list of objects"),
        ({}, "must be a list of objects"),
    ],
)
def test_a_malformed_operator_outcome_is_refused(tmp_path, entries, match):
    d = make_state(phase=Phase.REVIEW, current_pr_url=PR42).to_dict()
    d["operator_comment_outcomes"] = entries
    with pytest.raises(StateError, match=match):
        load_state(_write(tmp_path / "state.json", d))


def test_a_verified_outcome_is_unique_per_action_and_comment(tmp_path):
    """One action verified on two comments, or two actions on one comment,
    is two outcomes; only the same action on the same comment is one twice."""
    d = make_state(phase=Phase.REVIEW, current_pr_url=PR42).to_dict()
    d["operator_comment_outcomes"] = [
        _verified_outcome(),
        _verified_outcome(evidence="bound", comment_url=f"{OPERATOR_TARGET}#issuecomment-12"),
        _verified_outcome(action_id=OTHER_OPERATOR_ACTION_ID),
    ]
    loaded = load_state(_write(tmp_path / "state.json", d))
    assert len(loaded.operator_comment_outcomes) == 3


def test_declines_of_one_round_are_bounded_across_acceptance_and_operator_outcomes(tmp_path):
    """K9's and K10's declines share one bound per round: each list within it
    on its own is not enough. A withdrawal is not a decline, and a decline of
    another round does not count against this one."""
    d = make_state(phase=Phase.REVIEW, current_pr_url=PR42, review_round=2).to_dict()
    acceptance = [_outcome()] * (MAX_DECLINES_PER_ROUND // 2)
    operator = [_operator_outcome()] * (MAX_DECLINES_PER_ROUND - len(acceptance))
    d["acceptance_outcomes"] = acceptance
    d["operator_comment_outcomes"] = (
        operator
        + [_operator_outcome(outcome="withdrawn")] * MAX_DECLINES_PER_ROUND
        + [_operator_outcome(round=2, finding_id="R2-F1")] * MAX_DECLINES_PER_ROUND
    )
    loaded = load_state(_write(tmp_path / "state.json", d))
    assert loaded.round_declines() == MAX_DECLINES_PER_ROUND  # round 2's: the state's own round
    d["operator_comment_outcomes"].append(_operator_outcome())
    with pytest.raises(StateError) as exc:
        load_state(_write(tmp_path / "state.json", d))
    assert (
        "state fields 'acceptance_outcomes' and 'operator_comment_outcomes' hold "
        f"{MAX_DECLINES_PER_ROUND + 1} declines of review round 1, over {MAX_DECLINES_PER_ROUND}"
    ) in str(exc.value)


def test_round_declines_counts_both_kinds_of_decline_of_the_round_being_fixed():
    """A K9 and a K10 decline of the round each count once; a withdrawal, a
    stale or verified outcome, and a decline of another round do not."""
    s = make_state(phase=Phase.FIX, current_pr_url=PR42, review_round=2)
    s.acceptance_outcomes = [
        _outcome(round=2, finding_id="R2-F1"),
        _outcome(round=2, finding_id="R2-F1", outcome="stale"),
        _outcome(),
    ]
    s.operator_comment_outcomes = [
        _operator_outcome(round=2, finding_id="R2-F1"),
        _operator_outcome(round=2, finding_id="R2-F2"),
        _operator_outcome(round=2, finding_id="R2-F1", outcome="withdrawn"),
        _operator_outcome(round=2, finding_id="R2-F1", outcome="stale"),
        _verified_outcome(round=2, finding_id="R2-F1"),
        _operator_outcome(),
        _operator_outcome(),
    ]
    assert s.round_declines() == 3
    s.review_round = 1
    assert s.round_declines() == 3
    s.review_round = 3
    assert s.round_declines() == 0
    s.review_round = 2
    s.acceptance_outcomes = []
    assert s.round_declines() == 2


def _operator_entry(round_: int, n: int, outcome: str = "declined") -> dict:
    """The ``n``-th distinct ``operator_comment_outcomes`` entry of ``round_``."""
    action_id = f"X-{round_:08x}{n:08x}"
    finding_id = f"R{round_}-F1"
    if outcome == "verified":
        return _verified_outcome(
            round=round_,
            finding_id=finding_id,
            action_id=action_id,
            comment_url=f"{OPERATOR_TARGET}#issuecomment-{n + 1}",
        )
    return _operator_outcome(
        round=round_, finding_id=finding_id, action_id=action_id, outcome=outcome
    )


def _operator_round(round_: int) -> list[dict]:
    """The most one round records: MAX_DECLINES_PER_ROUND decline cycles of
    MAX_OPERATOR_ACTIONS_PER_FIX actions (one declined, the others withdrawn
    with it), then a plan of MAX_OPERATOR_ACTIONS_PER_FIX verified ones."""
    entries = []
    for cycle in range(MAX_DECLINES_PER_ROUND + 1):
        for n in range(MAX_OPERATOR_ACTIONS_PER_FIX):
            if cycle == MAX_DECLINES_PER_ROUND:
                outcome = "verified"
            else:
                outcome = "declined" if n == 0 else "withdrawn"
            entries.append(
                _operator_entry(round_, cycle * MAX_OPERATOR_ACTIONS_PER_FIX + n, outcome)
            )
    return entries


def test_one_round_of_operator_outcomes_fits_the_bound():
    """#243: a round holds at most MAX_DECLINES_PER_ROUND decline cycles and
    one completing plan, each of at most MAX_OPERATOR_ACTIONS_PER_FIX
    actions; the bound never drops an entry of the round being fixed, so it
    must hold that many."""
    assert MIN_OPERATOR_COMMENT_OUTCOMES == (
        (MAX_DECLINES_PER_ROUND + 1) * MAX_OPERATOR_ACTIONS_PER_FIX
    )
    assert MIN_OPERATOR_COMMENT_OUTCOMES == 68
    assert MAX_OPERATOR_COMMENT_OUTCOMES == 80
    assert MAX_OPERATOR_COMMENT_OUTCOMES >= MIN_OPERATOR_COMMENT_OUTCOMES
    assert len(_operator_round(1)) == MIN_OPERATOR_COMMENT_OUTCOMES


def test_the_operator_outcome_bound_drops_the_oldest_entries_never_one_of_the_round_being_fixed():
    """Past the bound the oldest entries go, but never one of the current
    round, whatever its outcome and wherever it stands: a decline refuses
    its request again and a verification is what REVIEW is told."""
    outcomes = list(OPERATOR_COMMENT_OUTCOMES)
    older = [
        _operator_entry(1, n, outcomes[n % len(outcomes)])
        for n in range(MAX_OPERATOR_COMMENT_OUTCOMES)
    ]
    s = make_state(phase=Phase.FIX, current_pr_url=PR42, review_round=2)
    s.operator_comment_outcomes = list(older)
    active: list[dict] = []
    for n in range(MIN_OPERATOR_COMMENT_OUTCOMES):
        active.append(_operator_entry(2, n, outcomes[n % len(outcomes)]))
        s.operator_comment_outcomes.append(active[-1])
        s.bound_operator_comment_outcomes()
        assert len(s.operator_comment_outcomes) == MAX_OPERATOR_COMMENT_OUTCOMES
        assert s.operator_comment_outcomes == older[n + 1 :] + active
    # The round's entries stay even as the oldest of the list.
    s.operator_comment_outcomes = active + older
    s.operator_comment_outcomes.append(_operator_entry(2, len(active), "stale"))
    s.bound_operator_comment_outcomes()
    kept = len(active)
    assert s.operator_comment_outcomes[:kept] == active
    assert s.operator_comment_outcomes[kept:-1] == older[kept + 1 :]
    assert s.operator_comment_outcomes[-1]["outcome"] == "stale"
    assert len(s.operator_comment_outcomes) == MAX_OPERATOR_COMMENT_OUTCOMES
    # Within the bound nothing is dropped.
    s.operator_comment_outcomes = older[:3]
    s.bound_operator_comment_outcomes()
    assert s.operator_comment_outcomes == older[:3]


def test_a_full_round_of_operator_outcomes_survives_the_bound_and_loads(tmp_path):
    """The largest round, after an earlier one as large: the bound keeps the
    whole current round and the newest of the earlier one, and the result
    is a state that loads, its round's declines at the shared bound."""
    s = make_state(phase=Phase.REVIEW, current_pr_url=PR42, review_round=2)
    s.operator_comment_outcomes = _operator_round(1) + _operator_round(2)
    s.bound_operator_comment_outcomes()
    room = MAX_OPERATOR_COMMENT_OUTCOMES - MIN_OPERATOR_COMMENT_OUTCOMES
    assert s.operator_comment_outcomes == _operator_round(1)[-room:] + _operator_round(2)
    assert s.round_declines() == MAX_DECLINES_PER_ROUND
    p = tmp_path / "state.json"
    save_state(s, p)
    assert load_state(p).operator_comment_outcomes == s.operator_comment_outcomes


def _with_evidence(**kw) -> dict:
    """``OPERATOR_ENTRY`` with one ``operator_evidence`` item changed by ``kw``."""
    return {**OPERATOR_ENTRY, "operator_evidence": [_operator_evidence(**kw)]}


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        (
            _drop_operator_evidence(FIXED_ENTRY),
            r"'last_fix_resolutions'\[0\]\.operator_evidence is missing",
        ),
        ({**FIXED_ENTRY, "operator_evidence": None}, "must be a list of at most"),
        (
            {**OPERATOR_ENTRY, "operator_evidence": [_operator_evidence()] * 81},
            "must be a list of at most",
        ),
        ({**OPERATOR_ENTRY, "operator_evidence": ["x"]}, "must hold exactly"),
        (
            {**OPERATOR_ENTRY, "operator_evidence": [{**_operator_evidence(), "round": 1}]},
            "must hold exactly",
        ),
        (_with_evidence(action_id=ACTION_ID), "action id of the form X-<16 hex>"),
        (_with_evidence(evidence=""), r"\.evidence must be one of"),
        (_with_evidence(evidence="observed"), r"\.evidence must be one of"),
        (_with_evidence(comment_url=""), r"\.comment_url: "),
        (
            _with_evidence(comment_url="https://GitHub.com/owner/repo/issues/5#issuecomment-11"),
            "is not a canonical comment URL on its target",
        ),
        (
            _with_evidence(comment_url=f"{FOLLOW_UP_ISSUE}#issuecomment-11"),
            "is not a canonical comment URL on its target",
        ),
        (
            _with_evidence(target=EFFECT_ISSUE, comment_url=f"{EFFECT_ISSUE}#issuecomment-11"),
            "is the run's own issue",
        ),
        (
            _with_evidence(
                target="https://github.com/other/repo/issues/5",
                comment_url="https://github.com/other/repo/issues/5#issuecomment-11",
            ),
            "is not an issue of repository 'owner/repo'",
        ),
        (_with_evidence(body_sha256=""), r"body_sha256 must be a 64-character"),
        (_with_evidence(payload_sha256="0" * 63), r"payload_sha256 must be a 64-character"),
    ],
)
def test_a_recorded_resolution_carries_well_formed_operator_evidence(tmp_path, entry, match):
    """A REMOTE ``last_fix_resolutions`` entry carries the operator comments
    verified for its finding, ``[]`` when none; every item names a canonical
    comment on another issue of the run's repository, observed by its digests."""
    d = make_state(phase=Phase.REVIEW, current_pr_url=PR42).to_dict()
    d["last_fix_resolutions"] = [entry]
    with pytest.raises(StateError, match=match):
        load_state(_write(tmp_path / "state.json", d))


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        (
            "operator_comment_outcomes",
            [_operator_outcome()],
            "'operator_comment_outcomes' is set on a LOCAL run",
        ),
        (
            "last_fix_resolutions",
            [{"finding_id": "R1-F1", "resolution": "fixed", "operator_evidence": []}],
            "carries 'operator_evidence' on a LOCAL run",
        ),
    ],
)
def test_a_local_run_carries_no_operator_state(tmp_path, field, value, match):
    """LOCAL runs have no GitHub, so they have no operator actions: neither an
    outcome nor a resolution's evidence list, even empty."""
    d = _local_state().to_dict()
    assert d["operator_comment_outcomes"] == []
    d[field] = value
    with pytest.raises(StateError, match=match):
        load_state(_write(tmp_path / "state.json", d))


def test_operator_outcomes_belong_to_the_issue_they_were_recorded_for():
    state = _after_operator_fix()
    state.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert state.operator_comment_outcomes == [] and state.last_fix_resolutions == []


# -- the sandbox pin (#280, protocol 12) -----------------------------------------------

PIN = {
    "schema": 1,
    "mode": "required",
    "source": "created",
    "recorded_at": "2026-10-24T00:00:00+00:00",
}


@pytest.mark.parametrize("local", [False, True], ids=["remote", "local"])
def test_a_sandbox_pin_round_trips_under_the_current_protocol(tmp_path, local):
    """Both modes pin: the field is beside LOCAL's contract and REMOTE's recorded configuration."""
    state = _local_state(sandbox_pin=dict(PIN)) if local else make_state(sandbox_pin=dict(PIN))
    p = tmp_path / "state.json"
    save_state(state, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12" and raw["sandbox_pin"] == PIN
    assert load_state(p).sandbox_pin == PIN


@pytest.mark.parametrize("local", [False, True], ids=["remote", "local"])
def test_a_protocol_11_file_loads_unpinned_and_is_relabelled(tmp_path, local):
    """A protocol-11 file is this protocol's shape without the pin: it loads
    with ``{}`` (not checked; the next step, resume or unblock pins it), and
    the next save writes the current protocol."""
    d = (_local_state() if local else make_state(phase=Phase.REVIEW)).to_dict()
    d["protocol_version"] = "11"
    _drop_pin(d)
    p = _write(tmp_path / "state.json", d)
    loaded = load_state(p)
    assert loaded.protocol_version == "12" and loaded.sandbox_pin == {}
    save_state(loaded, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "12" and raw["sandbox_pin"] == {}
    assert load_state(p) == loaded


@pytest.mark.parametrize("protocol", ["5", "7", "8", "9", "10", "11"])
@pytest.mark.parametrize("pin", [PIN, {}], ids=["pin", "empty"])
def test_an_older_file_carrying_a_sandbox_pin_is_refused_and_left_unchanged(
    tmp_path, protocol, pin
):
    """No controller before protocol 12 wrote ``sandbox_pin``, so an older
    label carrying one, even ``{}``, is a hand edit or a foreign file."""
    if protocol == "5":
        d = _legacy(protocol, phase=Phase.REVIEW, current_pr_url=PR42)
    elif protocol == "11":
        d = make_state(phase=Phase.REVIEW, current_pr_url=PR42).to_dict()
        d["protocol_version"] = "11"
    else:
        d = _as_protocol(make_state(phase=Phase.REVIEW, current_pr_url=PR42).to_dict(), protocol)
    d["sandbox_pin"] = dict(pin)
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    message = str(exc.value)
    assert f"labelled protocol_version {protocol!r}, which never wrote 'sandbox_pin'" in message
    assert "protocol-12 sandbox pin" in message
    assert p.read_bytes() == before


def test_a_protocol_12_file_missing_its_sandbox_pin_is_refused(tmp_path, local_fields):
    """Every protocol-12 save writes the field, ``{}`` included: read as the
    empty default, an absent one would switch the check off. An unlabelled
    file is read as the current protocol."""
    d = local_fields(make_state(sandbox_pin=dict(PIN)).to_dict())
    del d["sandbox_pin"]
    for label in ("labelled protocol_version '12'", "no protocol_version label (read as '12')"):
        if "no protocol_version" in label:
            del d["protocol_version"]
        p = _write(tmp_path / "state.json", d)
        with pytest.raises(StateError) as exc:
            load_state(p)
        assert f"{label} but is missing required field(s) 'sandbox_pin'" in str(exc.value)


@pytest.mark.parametrize(
    ("pin", "match"),
    [
        ([], "'sandbox_pin' must be an object"),
        ("required", "'sandbox_pin' must be an object"),
        ({**PIN, "schema": 2}, "'sandbox_pin': schema 2 is not 1"),
        ({**PIN, "mode": "off"}, "'sandbox_pin': mode must be one of"),
        ({**PIN, "source": "edited"}, "'sandbox_pin': source must be one of"),
        ({**PIN, "backend": "bwrap"}, "'sandbox_pin': the sandbox pin must hold exactly"),
        ({"mode": "required"}, "'sandbox_pin': schema None is not 1"),
    ],
)
@pytest.mark.parametrize("local", [False, True], ids=["remote", "local"])
def test_a_malformed_sandbox_pin_is_refused_never_read_as_unpinned(tmp_path, pin, match, local):
    d = (_local_state() if local else make_state()).to_dict()
    d["sandbox_pin"] = pin
    with pytest.raises(StateError, match=match):
        load_state(_write(tmp_path / "state.json", d))


def test_the_sandbox_pin_outlives_a_new_issue():
    """The pin is the run's, not the issue's: an EPIC's next issue keeps it."""
    state = make_state(sandbox_pin=dict(PIN))
    state.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert state.sandbox_pin == PIN
