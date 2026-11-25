import json
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from tests.review_eval import FIXTURE, EvidenceError, _read_packet, bootstrap, main, score


@pytest.fixture
def packet():
    return json.loads(FIXTURE.read_bytes())


def recorded(packet, case_id=None):
    packet["mode"] = "recorded"
    if case_id is not None:
        packet["cases"] = {case_id: packet["cases"][case_id]}
        packet["runs"] = [run for run in packet["runs"] if run["case"] == case_id]
    for run in packet["runs"]:
        for row in run["launches"]:
            if row is not None:
                row.update(cost_microusd=None, context_tokens=None)


def result(packet, case, arm):
    return next(row for row in score(packet)["pairs"] if row["case"] == case)["runs"][arm]


def retry_review(row, **changes):
    return dict(row, outcome="retry", comment=None, findings=None, **changes)


def no_certificate(run, *, through, round_, review, at):
    run["stop"].update(
        phase="BLOCKED",
        issuance="not_issued",
        proof=None,
        through=through,
        round=round_,
        review=review,
        head=at["head"],
        tree=at["tree"],
    )


def test_fixture_replays_and_reports_hand_computed_results(packet, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["review_eval"])
    main()
    out = capsys.readouterr().out
    main()
    assert capsys.readouterr().out == out
    report = json.loads(out)
    assert report["decision"] == "NOT_EVALUATED"
    assert report["matched_pairs"] == 2
    assert report["new_critical_escapes"] == [["wrong-boundary", 0, "lost-data"]]
    assert report["critical_escape_totals"]["treatment"] == {"known": 1, "unresolved": 0}
    assert {key: value["difference"] for key, value in report["rates"].items()} == {
        "recall": "0",
        "precision": "-1/2",
        "false_positive_rate": "1",
        "harmful_fix_rate": "1",
        "duplicate_ratio": "-1/2",
    }
    assert report["cost_usd_per_validated_success"] == {
        "baseline": "9/4",
        "treatment": "21/4",
    }
    assert result(packet, "wrong-boundary", "baseline")["wall_ms"] == 850


def test_document_pins_the_fixture_bytes():
    doc = Path(__file__).resolve().parents[1] / "docs" / "review-evaluation-protocol.md"
    assert sha256(FIXTURE.read_bytes()).hexdigest() in doc.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("same-launch-source", "FIX source ordinal"),
        ("source-before-review", "accepted needs-fix"),
        ("wrong-phase", "wrong phase"),
        ("fix-before-review", "accepted needs-fix"),
        ("reversed-round", "next review round"),
        ("post-publication-retry", "unexpected launch phase"),
        ("round-jump", "next review round"),
        ("observed-replan", "outside registered launch phases"),
    ],
)
def test_one_pr_lifecycle_refuses_observed_contradictions(packet, change, error):
    run = packet["runs"][0]
    launches = run["launches"]
    fix = launches[2]
    if change == "same-launch-source":
        fix["source"] = 3
    elif change == "source-before-review":
        fix["source"] = 1
    elif change == "wrong-phase":
        fix["phase"] = "REVIEW"
    elif change == "fix-before-review":
        launches[1:3] = [dict(fix, source=1, outcome="retry", published=None, judgment=None)]
    elif change == "reversed-round":
        launches[-1]["round"] = 1
    elif change == "post-publication-retry":
        launches.insert(3, dict(fix, outcome="retry", published=None, judgment=None))
    elif change == "round-jump":
        launches[-1]["round"] = 20
    elif change == "observed-replan":
        launches[1] = None
        launches[2]["phase"] = "REPLAN_REEXECUTE"
        launches[-1]["round"] = 1
    with pytest.raises(EvidenceError, match=error):
        score(packet)


@pytest.mark.parametrize("stale", [False, True])
def test_clean_or_stale_review_cannot_authorize_fix(packet, stale):
    launch = packet["runs"][0]["launches"][1]
    if stale:
        launch.update(outcome="stale_needs_fix", next_head=packet["runs"][0]["stop"]["head"])
    else:
        launch.update(outcome="clean", findings={"complete": True, "items": []})
    with pytest.raises(EvidenceError, match="accepted needs-fix"):
        score(packet)


@pytest.mark.parametrize("gap", [1, 3, 37])
def test_gap_does_not_fill_rounds_or_allow_a_replacement_reset(packet, gap):
    run = packet["runs"][0]
    fix = run["launches"][2]
    suffix = [None] * gap
    if gap == 3:
        suffix.append(dict(fix, outcome="retry", published=None, judgment=None))
    else:
        run["launches"][-1]["round"] = 20
        run["stop"]["round"] = 20
    run["launches"][3:3] = suffix
    run["stop"].update(review=len(run["launches"]), through=len(run["launches"]), wall_ms=None)
    got = result(packet, "wrong-boundary", "baseline")
    assert not got["launch_window_complete"]
    assert not got["review_record_complete"]
    assert got["validated_success"] is None
    assert got["known_findings"] == {"R1-F1": "confirmed", "R1-F2": "confirmed"}


def test_missing_launch_is_not_a_conflict_with_later_changed_head(packet):
    run = packet["runs"][0]
    run["launches"][1:3] = [None, None]
    got = result(packet, "wrong-boundary", "baseline")
    assert not got["review_record_complete"]
    assert got["certificate_issued"] is None


def test_retries_before_completion_cost_money_but_do_not_consume_rounds(packet):
    run = packet["runs"][0]
    launch, review, fix, _ = run["launches"]
    run["launches"][0:0] = [dict(launch, outcome="retry")]
    run["launches"][2:2] = [retry_review(review)]
    fix["source"] = 4
    run["launches"].insert(4, dict(fix, outcome="retry", published=None, judgment=None))
    run["stop"].update(review=7, through=7, wall_ms=1500)
    got = result(packet, "wrong-boundary", "baseline")
    assert got["review_record_complete"] and got["validated_success"]
    assert got["completed_rounds"] == 2
    assert got["launches_observed"] == {"ANALYZE_EXECUTE": 2, "REVIEW": 3, "FIX": 2}
    assert got["cost_microusd"] == 5_500_000


@pytest.mark.parametrize("field", ["merge_base", "base_ref", "pr", "tree"])
def test_exact_binding_pins_start_and_later_inputs(packet, field):
    run = packet["runs"][0]
    case = packet["cases"][run["case"]]
    if field == "merge_base":
        # Give the starting revision an intermediate ancestor distinct from
        # the registered base, so ancestry alone would accept the mismatch.
        start = case["snapshots"][case["start"]]
        intermediate = "7" * 40
        case["snapshots"][intermediate] = dict(start, tree="8" * 40, present=[])
        start["parent"] = intermediate
        run["launches"][1]["merge_base"] = intermediate
    else:
        run["launches"][-1][field] = {"base_ref": "release", "pr": "other", "tree": "9" * 40}[field]
    with pytest.raises(EvidenceError, match="scope|merge base|candidate"):
        score(packet)


@pytest.mark.parametrize("outcome", ["clean", "stale_clean"])
def test_failed_validation_refuses_clean_before_it_can_become_stale(packet, outcome):
    run = packet["runs"][0]
    launch = run["launches"][1]
    launch.update(
        outcome=outcome,
        findings={"complete": True, "items": []},
        next_head=run["stop"]["head"] if outcome == "stale_clean" else None,
    )
    launch["validation"]["result"] = "fail"
    with pytest.raises(EvidenceError, match="clean reply over failed validation"):
        score(packet)


def test_refused_reply_and_completed_stale_round_are_distinct(packet):
    run = packet["runs"][0]
    launch, final = run["launches"][1], run["launches"][-1]
    refused = retry_review(launch, validation=dict(launch["validation"], result="fail"))
    stale = dict(launch, outcome="stale_needs_fix", next_head=final["head"])
    run["launches"][1:] = [refused, stale, final]
    got = result(packet, "wrong-boundary", "baseline")
    assert got["completed_rounds"] == 2
    assert got["known_findings"] == {}  # A completed stale round is not accepted for scoring.
    assert got["validated_success"]


def test_retry_after_head_moves_still_reviews_round_one(packet):
    run = packet["runs"][0]
    launch, final = run["launches"][1], run["launches"][-1]
    run["launches"][1:] = [
        retry_review(launch, validation=dict(launch["validation"], result="fail")),
        dict(final, round=1, transition="drift"),
    ]
    run["stop"].update(through=3, round=1, review=3)
    got = result(packet, "wrong-boundary", "baseline")
    assert got["completed_rounds"] == 1 and got["validated_success"]
    assert not got["starting_review_accepted"] and got["counts"]["recall"] is None


def test_missing_finding_payload_does_not_change_false_alarm_to_true_negative(packet):
    recorded(packet, "clean-boundary")
    run = packet["runs"][1]
    assert score(packet)["decision"] == "NO_GO"
    run["launches"][1]["findings"]["complete"] = False
    got = result(packet, "clean-boundary", "treatment")
    assert got["clean_negative_false_alarm"] is True  # Known positive survives truncation.
    run["launches"][1]["findings"] = None
    got = result(packet, "clean-boundary", "treatment")
    assert got["launch_window_complete"] and got["validated_success"]
    assert not got["review_record_complete"] and got["clean_negative_false_alarm"] is None
    report = score(packet)
    assert report["rates"]["false_positive_rate"]["treatment"]["point"] is None
    assert report["decision"] == "INCONCLUSIVE"


def test_missing_review_disposition_is_not_a_report_of_no_findings(packet):
    run = packet["runs"][3]
    run["launches"][1].update(outcome=None, comment=None, findings=None)
    got = result(packet, "clean-boundary", "treatment")
    assert got["launch_window_complete"] and not got["lifecycle_complete"]
    assert not got["review_record_complete"] and got["clean_negative_false_alarm"] is None
    assert got["validated_success"] is None


def test_certificate_cannot_reuse_a_historical_round_or_promote_a_retry(packet):
    run = packet["runs"][2]
    case = packet["cases"][run["case"]]
    launch = run["launches"][1]
    new = "6" * 40
    tree = "7" * 40
    case["snapshots"][new] = dict(
        case["snapshots"][launch["head"]], parent=launch["head"], tree=tree
    )
    trailing = retry_review(launch, head=new, tree=tree, round=2, transition="drift")
    run["launches"].append(trailing)
    run["stop"].update(through=3, wall_ms=700)
    with pytest.raises(EvidenceError, match="stop disagrees"):
        score(packet)
    run["stop"].update(head=new, tree=tree, round=2, review=3)
    with pytest.raises(EvidenceError, match="stop disagrees"):
        score(packet)
    no_certificate(run, through=3, round_=1, review=2, at=trailing)
    got = result(packet, "clean-boundary", "baseline")
    assert got["review_record_complete"] and got["clean_negative_false_alarm"] is False
    assert got["terminal_bound"] and got["certificate_issued"] is False


def test_accepted_clean_can_block_without_issuing_a_certificate(packet):
    run = packet["runs"][2]
    no_certificate(run, through=2, round_=1, review=2, at=run["launches"][1])
    got = result(packet, "clean-boundary", "baseline")
    assert got["review_record_complete"] and got["terminal_bound"]
    assert got["certificate_issued"] is False and got["validated_success"] is False


@pytest.mark.parametrize("held", ["held", "retry"])
def test_bound_nonissuance_after_fix_can_establish_a_new_shared_escape(packet, held):
    recorded(packet, "wrong-boundary")
    case = packet["cases"]["wrong-boundary"]
    case["defects"]["off-by-one"]["severity"] = "critical"
    baseline, treatment = packet["runs"]
    baseline["launches"].pop()
    fix = baseline["launches"][-1]
    fix.update(outcome=held, published=None, judgment=None)
    no_certificate(baseline, through=3, round_=1, review=2, at=fix)
    treatment["launches"] = treatment["launches"][:2]
    clean = treatment["launches"][1]
    clean.update(outcome="clean", findings={"complete": True, "items": []})
    treatment["stop"].update(through=2, round=1, review=2, head=clean["head"], tree=clean["tree"])
    treatment["stop"]["proof"]["comment"] = clean["comment"]
    got = result(packet, "wrong-boundary", "baseline")
    assert got["terminal_bound"] and got["critical_escapes"]["off-by-one"] is False
    report = score(packet)
    assert report["new_critical_escapes"] == [["wrong-boundary", 0, "off-by-one"]]
    assert report["decision"] == "NO_GO"
    baseline["stop"] = None
    assert score(packet)["new_critical_escapes"] == []


def test_exported_regression_overrides_an_unknown_fix_judgment_and_exclusion(packet):
    recorded(packet, "wrong-boundary")
    treatment = packet["runs"][1]
    treatment["launches"][2]["judgment"] = {
        "status": "unknown",
        "source": packet["cases"]["wrong-boundary"]["defects"]["lost-data"]["source"],
    }
    assert score(packet)["rates"]["harmful_fix_rate"]["treatment"]["point"] == "1"
    treatment["exclusion"] = "infrastructure"
    report = score(packet)
    assert report["matched_pairs"] == 0
    assert report["decision"] == "NO_GO"
    row = report["pairs"][0]["runs"]["treatment"]
    assert row["critical_escapes"]["lost-data"] is True and row["known_harmful_fixes"] == 1
    packet["runs"].pop(0)
    assert score(packet)["decision"] == "NO_GO"


def test_excluded_failed_first_review_needs_no_completed_reply(packet):
    recorded(packet, "wrong-boundary")
    run = packet["runs"][1]
    run["launches"] = run["launches"][:2]
    run["launches"][1] = retry_review(run["launches"][1])
    run["stop"] = None
    run["exclusion"] = "service_unavailable"
    report = score(packet)
    assert report["pair_status"] == {"excluded": 1}
    assert report["decision"] == "INCONCLUSIVE"


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ("set", "validation_mismatch"),
        ("policy", "validation_mismatch"),
        ("missing", "validation_unknown"),
    ],
)
def test_pairs_distinguish_validation_conflict_from_absence(packet, change, status):
    recorded(packet, "clean-boundary")
    baseline = packet["runs"][0]
    row = baseline["launches"][1]
    if change == "set":
        row["validation"].update(set=[], result="empty")
    elif change == "policy":
        row["validation"]["policy"] = "c" * 64
    else:
        row["validation"] = None
    if row["validation"] is not None:
        baseline["stop"]["proof"]["validation"] = row["validation"]
    report = score(packet)
    assert report["pair_status"] == {status: 1} and report["matched_pairs"] == 0
    assert report["rates"]["false_positive_rate"]["treatment"]["point"] is None
    got = report["pairs"][0]["runs"]["baseline"]
    if change == "set":
        assert got["certificate_issued"] and got["validated_success"] is False


@pytest.mark.parametrize("change", ["comment", "validation", "tree", "round", "fast"])
def test_terminal_certificate_binds_every_input(packet, change):
    stop = packet["runs"][0]["stop"]
    if change == "comment":
        stop["proof"]["comment"] = "wrong-comment"
    elif change == "validation":
        stop["proof"]["validation"]["policy"] = "d" * 64
    elif change == "tree":
        stop["tree"] = "d" * 40
    elif change == "fast":
        stop["proof"]["validation"]["scope"] = "fast"
    else:
        stop["round"] = 1
    with pytest.raises(EvidenceError):
        score(packet)


def test_label_cutoff_and_agent_independence(packet):
    row = packet["runs"][3]["launches"][1]
    label = row["findings"]["items"][0]["label"]
    label["source"]["discovered"] = "2026-11-21"
    stats = score(packet)["rates"]["false_positive_rate"]["treatment"]
    assert stats == {"point": None, "low": "0", "high": "1"}
    label["source"]["adjudicator"] = packet["runs"][3]["actors"][1]
    with pytest.raises(EvidenceError, match="agent cannot adjudicate"):
        score(packet)


def test_repeated_assignment_refuses_cherry_picking(packet):
    packet["runs"].append(deepcopy(packet["runs"][0]))
    with pytest.raises(EvidenceError, match="unplanned or repeated"):
        score(packet)


def test_real_usage_not_claimed_and_unknown_not_zero(packet):
    recorded(packet)
    got = result(packet, "wrong-boundary", "baseline")
    assert (
        got["validated_success"] and got["context_tokens"] is None and got["cost_microusd"] is None
    )
    assert score(packet)["cost_usd_per_validated_success"] == {"baseline": None, "treatment": None}
    packet["runs"][0]["launches"][0]["cost_microusd"] = 0
    with pytest.raises(EvidenceError, match="only synthetic"):
        score(packet)


def test_missing_watermark_or_usage_does_not_invent_zero(packet):
    run = packet["runs"][0]
    run["launches"][0].update(cost_microusd=None, elapsed_ms=None)
    got = result(packet, "wrong-boundary", "baseline")
    assert got["launch_window_complete"] and got["validated_success"]
    assert got["cost_microusd"] is None and got["launch_ms"] is None
    run["stop"]["through"] += 1
    got = result(packet, "wrong-boundary", "baseline")
    assert not got["launch_window_complete"] and got["wall_ms"] is None
    assert not got["terminal_bound"]


def test_cli_refuses_duplicate_json_keys_without_echoing_content(tmp_path, monkeypatch, capsys):
    path = tmp_path / "packet.json"
    path.write_text('{"untrusted text": 1, "untrusted text": 2}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["review_eval", str(path)])
    with pytest.raises(SystemExit, match="2"):
        main()
    out = capsys.readouterr()
    assert not out.out
    assert out.err == "Invalid review evidence packet.\n"


def test_cli_opens_neither_a_link_nor_a_fifo_as_evidence(tmp_path):
    import os

    path = tmp_path / "packet.json"
    path.symlink_to(FIXTURE)
    with pytest.raises(OSError):
        _read_packet(path)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(EvidenceError, match="regular packet"):
        _read_packet(fifo)


def test_bootstrap_respects_the_metric_population_and_reports_unusable_draws(packet):
    report = score(packet)
    fpr = report["rates"]["false_positive_rate"]
    assert fpr["bootstrap_point_interval"] == ["1", "1"]
    assert fpr["bootstrap_label_envelope"] == ["1", "1"]
    assert fpr["usable"]
    groups = (((0, 0, 0), (1, 0, 1)), ((0, 0, 1), (1, 0, 1)))
    points, bounds, undefined_points, undefined_bounds = bootstrap(groups, recall=False)
    assert points == bounds == (1, 1)
    assert 0 < undefined_points == undefined_bounds < 10_000
