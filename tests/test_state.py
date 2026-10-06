"""State: serialize/deserialize, atomic save/load, corruption, idempotency."""

import errno
import json

import pytest

from autoforge.claims import render_follow_up_marker, render_progress_marker
from autoforge.effects import (
    LABEL_AGENT_PUBLISHES,
    LABEL_CONTROLLER_PUBLISHES,
    LABEL_NONE,
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
    payload_chars,
    progress_comment_body,
    sha256_text,
)
from autoforge.errors import StateError
from autoforge.state import AutoForgeState, StatePaths, load_state, save_state
from autoforge.transitions import Phase, WorkflowMode

from .conftest import sample_contract


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


def test_load_corrupted_json_raises_and_does_not_clobber(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"phase": "REVIEW", "run_id": ', encoding="utf-8")
    with pytest.raises(StateError, match="[Cc]orrupt"):
        load_state(p)
    # file untouched — loader never writes
    assert p.read_text(encoding="utf-8") == '{"phase": "REVIEW", "run_id": '


def test_load_invalid_utf8_raises_state_error_and_does_not_clobber(tmp_path):
    """Invalid UTF-8 is corruption, not a read error: it must surface as StateError."""
    p = tmp_path / "state.json"
    raw = b'\xff\xfe{"phase": "REVIEW"}'
    p.write_bytes(raw)
    with pytest.raises(StateError, match="[Cc]orrupt.*UTF-8"):
        load_state(p)
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


def test_protocol_1_is_migrated_only_without_a_replan_in_flight(tmp_path):
    """#66 R7-F1: the 1 -> 2 protocol change is confined to the replan journal.

    A protocol-1 file with an empty or terminal journal loads and is
    relabelled (to the current protocol; the 2 -> 3 rule of #68 is
    checked by ``test_protocol_2_is_migrated_only_outside_the_merge_phases``);
    one with a replan in flight is refused at the boundary with the
    transaction described; any other label is unsupported as before.
    """
    path = tmp_path / "state.json"
    base = make_state().to_dict()
    assert base["protocol_version"] == "7"
    for journal in ({}, {"stage": "rejected", "rejection_reason": "refused by verification"}):
        data = dict(base, protocol_version="1", replan_transaction=journal)
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = load_state(path)
        assert loaded.protocol_version == "7" and loaded.replan_transaction == journal
        save_state(loaded, path)
        assert json.loads(path.read_text())["protocol_version"] == "7"
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
def test_protocol_2_is_refused_in_the_merge_phases(tmp_path, phase):
    """#68: protocol 2 did not record which PR and base the clean review was
    posted on. A file parked before MERGE cannot be bound after the fact --
    filling the binding from ``current_pr_url`` would be exactly the trust
    the binding exists to remove -- so it is refused at the boundary, left
    unchanged, with the PR and HEAD named so the operator can look."""
    path = tmp_path / "state.json"
    data = _clean_review_state(phase=phase).to_dict()
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
    ):
        del data[missing]
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_state(path)
    assert loaded.protocol_version == "7" and loaded.phase == phase
    assert (loaded.reviewed_pr_url, loaded.reviewed_base_ref, loaded.current_base_ref) == (
        "",
        "",
        "",
    )
    assert (loaded.reviewed_merge_base_sha, loaded.current_merge_base_sha) == ("", "")
    assert loaded.reviewed_head_sha == "a" * 40  # what protocol 2 did record is kept
    save_state(loaded, path)
    assert json.loads(path.read_text())["protocol_version"] == "7"


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_protocol_3_is_refused_in_the_merge_phases(tmp_path, phase):
    """#96: protocol 3 bound the clean review to its PR and base name but
    not to the merge base its diff was computed from. A file parked before
    MERGE cannot be bound after the fact -- reading the merge base now would
    bind the review to whatever the base is now, the rewrite the field
    exists to catch -- so it is refused at the boundary, left unchanged,
    with the PR and HEAD named. The journal rule applies first, as for
    every earlier protocol: a protocol-3 journal that is empty or rejected
    is not in flight, so the binding rule decides."""
    path = tmp_path / "state.json"
    data = _clean_review_state(phase=phase).to_dict()
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
        for missing in ("reviewed_merge_base_sha", "current_merge_base_sha"):
            del data[missing]
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = load_state(path)
        assert loaded.protocol_version == "7" and loaded.phase == phase
        assert (loaded.reviewed_merge_base_sha, loaded.current_merge_base_sha) == ("", "")
        assert loaded.reviewed_pr_url == PR42 and loaded.reviewed_base_ref == "main"
        assert loaded.replan_transaction == journal
        save_state(loaded, path)
        assert json.loads(path.read_text())["protocol_version"] == "7"


@pytest.mark.parametrize("phase", list(Phase))
def test_protocol_3_is_refused_with_a_replan_in_flight(tmp_path, phase):
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
    data = _clean_review_state(phase=phase, replan_transaction=in_flight).to_dict()
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
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = load_state(path)
        assert loaded.protocol_version == "7" and loaded.phase == phase
        assert loaded.replan_transaction == journal
        assert loaded.reviewed_merge_base_sha == "d" * 40  # the binding is kept whole
        save_state(loaded, path)
        assert json.loads(path.read_text())["protocol_version"] == "7"


@pytest.mark.parametrize("phase", list(Phase))
@pytest.mark.parametrize("stage", ["supersede_intent", "compensating", "superseded"])
def test_protocol_4_is_refused_with_a_replan_past_the_write(tmp_path, phase, stage):
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
    data = _clean_review_state(phase=phase, replan_transaction=in_flight).to_dict()
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


def test_load_oversized_state_file_is_corrupt_state_without_materialising_it(tmp_path, monkeypatch):
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
    p.write_bytes(b'{"phase": "REVIEW"}')
    os.truncate(p, 16 * MAX_STATE_FILE_BYTES)
    assert p.stat().st_size == 16 * MAX_STATE_FILE_BYTES

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
    with pytest.raises(StateError, match="[Cc]orrupt.*larger than .* bytes.*refusing to overwrite"):
        load_state(p)
    assert asked and max(asked) == MAX_STATE_FILE_BYTES + 1, asked
    assert p.stat().st_size == 16 * MAX_STATE_FILE_BYTES, "loader never writes"


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
        payload={"title": f"Follow-up for {finding_id}", "body": f"{text}\n\n{marker}"},
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


def _review_state(**kw):
    context = ReviewContext(
        issue_url=EFFECT_ISSUE,
        pr_url=PR42,
        round=1,
        needs_fix_round=True,
        findings=(dict(FIX_FINDINGS[0]),),
    )
    base = dict(
        phase=Phase.REVIEW,
        current_pr_url=PR42,
        review_round=0,
        attempt=1,
        launch_label=LABEL_AGENT_PUBLISHES,
        entry_observation=_observation(Phase.REVIEW),
        completion_context=context.to_dict(),
    )
    base.update(kw)
    return make_state(**base)


REPLAN_REF = "refs/heads/autoforge/2-replan"


def _replan_state(**kw):
    """A REPLAN_REEXECUTE entry: a K1 push owned by the transaction, and the K7 counts."""
    push = EffectRecord.plan(
        0,
        EffectKind.PUSH,
        _owner(Phase.REPLAN_REEXECUTE, txn=EFFECT_TXN),
        identity={"repository": "owner/repo", "ref": REPLAN_REF, "candidate_sha": CANDIDATE_SHA},
        target={"repository": "owner/repo", "ref": REPLAN_REF},
        precondition={"expected_old": None, "base_sha": BASE_SHA},
        payload={"sha": CANDIDATE_SHA},
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
        launch_label=LABEL_AGENT_PUBLISHES,
        effect_records=[push.to_dict()],
        entry_observation=_observation(
            Phase.REPLAN_REEXECUTE, refs={REPLAN_REF: None}, base_sha=BASE_SHA
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


# -- round trips under protocol 7 ------------------------------------------------------


def test_the_controller_speaks_protocol_7():
    """ADR 0004 D13.1: #160 is protocol 7; 6 is reserved for #126."""
    from autoforge import __protocol_version__

    assert __protocol_version__ == "7"


@pytest.mark.parametrize(
    ("stage", "phase"),
    [
        (Stage.INTENDED, Phase.UPDATE_EPIC),
        (Stage.ATTEMPTED, Phase.UPDATE_EPIC),
        (Stage.OBSERVED, Phase.UPDATE_EPIC),
        (Stage.CONFLICT, Phase.BLOCKED),
    ],
)
def test_effect_state_round_trips_in_every_record_stage_under_protocol_7(tmp_path, stage, phase):
    """D2.1-D2.3, D4.4, D4.6, D13.3: a record in each stage, the entry observation,
    the completion context and the launch label survive save and load unchanged,
    in a file labelled protocol 7. A conflict is persisted with the run BLOCKED
    in the phase it stopped, which keeps the pieces bound to that phase."""
    p = tmp_path / "state.json"
    s = _update_epic_state(stage, phase=phase)
    save_state(s, p)

    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "7"
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
    """D4.6: the REVIEW context (round, needs_fix_round, findings) and the
    REPLAN_REEXECUTE context and push record (bound to the state's replan
    transaction id) round-trip; a context saved without records is legal."""
    for name, s in (("review", _review_state()), ("replan", _replan_state())):
        p = tmp_path / f"{name}.json"
        save_state(s, p)
        loaded = load_state(p)
        assert loaded == s
        effects = loaded.phase_effects()
        assert effects.context is not None
        assert effects.context.to_dict() == s.completion_context
    review = load_state(tmp_path / "review.json").phase_effects()
    assert review.records == () and review.context.round == 1
    replan = load_state(tmp_path / "replan.json").phase_effects()
    assert replan.records[0].owner.transaction_id == EFFECT_TXN
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
        (_set((*R0, "position"), 51), "position must be in [0, 50], got 51"),
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
    ],
)
def test_a_corrupt_entry_observation_fails_loudly(tmp_path, mutate, needle):
    """D4.4: the entry observation is a closed, bounded schema (refs, base,
    marker-keyed objects); anything else is corruption and refused on load."""
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


def test_records_persisted_without_their_completion_context_are_refused(tmp_path):
    """D4.6: records are written in the same save as the context they complete;
    records without one could never complete the phase, and are corruption."""
    s = _update_epic_state(completion_context={})
    p = tmp_path / "state.json"
    save_state(s, p)
    with pytest.raises(StateError, match="are persisted without the completion context"):
        load_state(p)


def test_effect_records_over_the_per_plan_count_are_refused(tmp_path):
    """D2.4: a plan holds at most one push plus one effect per finding; a longer
    list is refused before any record in it is parsed."""
    assert MAX_EFFECTS_PER_PLAN == 51
    s = _update_epic_state()
    s.effect_records = [{}] * (MAX_EFFECTS_PER_PLAN + 1)
    p = tmp_path / "state.json"
    save_state(s, p)
    with pytest.raises(StateError, match="holds 52 records, over 51"):
        load_state(p)


def _big_fix_state(count):
    """A FIX entry deferring ``count`` findings, each to a ~62K-character follow-up."""
    filler = "word " * 12_400
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
    """The dict an older controller wrote: no protocol-7 field at all."""
    d = make_state(**kw).to_dict()
    d["protocol_version"] = protocol
    for key in _EFFECT_KEYS:
        del d[key]
    return d


@pytest.mark.parametrize("protocol", ["1", "2", "3", "4", "5"])
def test_a_legacy_file_loads_with_no_effect_state_and_is_relabelled_7_on_save(tmp_path, protocol):
    """D13.2: protocol 5 (the label before effects) and the older legacy labels
    load with no records, no observation and no context, and the next save writes
    protocol 7 with the four fields at their empty values."""
    p = _write(
        tmp_path / "state.json",
        _legacy(protocol, phase=Phase.REVIEW, current_pr_url=PR42, review_round=1),
    )
    loaded = load_state(p)
    assert loaded.protocol_version == "7"
    assert loaded.phase_effects().empty
    assert _effect_fields(loaded) == {
        "effect_records": [],
        "entry_observation": {},
        "completion_context": {},
        "launch_label": LABEL_NONE,
    }
    save_state(loaded, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["protocol_version"] == "7"
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
    no label. The label survives the save that relabels the file."""
    from autoforge.effects import is_legacy_reentry

    p = _write(
        tmp_path / "state.json",
        _legacy("5", phase=phase, attempt=attempt, current_pr_url=PR42),
    )
    loaded = load_state(p)
    assert loaded.launch_label == label
    assert loaded.phase_effects().empty
    assert is_legacy_reentry(loaded.phase, loaded.attempt, loaded.launch_label) == (
        phase == Phase.UPDATE_EPIC and attempt >= 1
    )
    save_state(loaded, p)
    assert json.loads(p.read_text(encoding="utf-8"))["launch_label"] == label
    assert load_state(p).launch_label == label


def test_a_legacy_local_state_gets_no_launch_label(tmp_path):
    """D13.2: the label is a REMOTE concept; a protocol-5 LOCAL file mid-launch
    loads with no label, so its protocol-7 save stays loadable as LOCAL."""
    d = _local_state(attempt=1).to_dict()
    d["protocol_version"] = "5"
    for key in _EFFECT_KEYS:
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
    tmp_path, protocol, field, value
):
    """D13.2: a legacy label never wrote effect state, so a legacy file carrying
    any non-empty effect field is a hand edit or a foreign file: refused, never
    merged, and the file is not rewritten."""
    d = _legacy(protocol, phase=Phase.UPDATE_EPIC, attempt=1, current_pr_url=PR42)
    d[field] = value
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"labelled protocol_version {protocol!r}, which never wrote {field!r}" in str(exc.value)
    assert p.read_bytes() == before


@pytest.mark.parametrize("missing", _EFFECT_KEYS)
@pytest.mark.parametrize("stage", [Stage.INTENDED, Stage.ATTEMPTED])
def test_a_protocol_7_file_missing_an_effect_field_is_refused_and_left_unchanged(
    tmp_path, missing, stage
):
    """D13.2 loads only a legacy label with no effect state. A protocol-7 save
    writes all four fields, so one that is absent is corruption: read as its
    empty default, a missing ``effect_records`` would turn a pending progress
    comment into no plan at all, and the phase would complete from its context
    without publishing it."""
    d = _update_epic_state(stage).to_dict()
    assert d["protocol_version"] == "7"
    del d[missing]
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    needle = f"labelled protocol_version '7' but is missing required field(s) {missing!r}"
    assert needle in str(exc.value)
    assert p.read_bytes() == before


def test_an_unlabelled_file_missing_an_effect_field_is_refused(tmp_path):
    """Every controller has written ``protocol_version``; a file without one is
    read as the current protocol, so deleting the label as well as
    ``effect_records`` does not turn a pending progress comment into no plan."""
    d = _update_epic_state(Stage.INTENDED).to_dict()
    del d["protocol_version"], d["effect_records"]
    p = _write(tmp_path / "state.json", d)
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
    assert loaded.protocol_version == "7"
    assert loaded.phase_effects().empty
    assert loaded.launch_label == LABEL_AGENT_PUBLISHES


@pytest.mark.parametrize("protocol", ["6", "8", 7])
def test_protocol_6_and_other_unknown_labels_are_refused_and_left_unchanged(tmp_path, protocol):
    """D13.1: protocol 6 is reserved for #126 and was never written by any
    released controller, so it is not a legacy label this controller migrates;
    it is refused exactly like a future label (and a non-string label), and the
    file is not rewritten. Pinned so that adopting 6 is a deliberate change."""
    d = make_state().to_dict()
    d["protocol_version"] = protocol
    p = _write(tmp_path / "state.json", d)
    before = p.read_bytes()
    with pytest.raises(StateError) as exc:
        load_state(p)
    assert f"unsupported protocol_version {protocol!r} (controller speaks '7')" in str(exc.value)
    assert p.read_bytes() == before


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
