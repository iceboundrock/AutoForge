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


def introduce_before_start(case, key):
    """Register merge base -> introducing commit -> starting tip for one starting defect."""
    start = case["snapshots"][case["start"]]
    introducing = "6" * 40
    case["snapshots"][introducing] = dict(start, tree="7" * 40, present=[key])
    start["parents"] = [introducing]
    case["defects"][key]["introduced"] = introducing
    return introducing


def add_stale_round(packet, index):
    """Insert a completed stale round before a run's terminal clean review."""
    run = packet["runs"][index]
    case = packet["cases"][run["case"]]
    final = run["launches"][-1]
    moved, tree = "6" * 40, "7" * 40
    case["snapshots"][moved] = dict(
        case["snapshots"][final["head"]], parents=[final["head"]], tree=tree
    )
    stale = dict(
        final,
        outcome="stale_clean",
        next_head=moved,
        comment=final["comment"] + "/stale",
        findings={"complete": True, "items": []},
    )
    clean = dict(final, round=final["round"] + 1, head=moved, tree=tree)
    run["launches"][-1:] = [stale, clean]
    size = len(run["launches"])
    run["stop"].update(
        through=size, review=size, round=clean["round"], head=moved, tree=tree, wall_ms=None
    )
    return stale, clean


def copy_case(packet, case_id, copy_id):
    """Register a copy of a case and of its runs under fresh PR identities."""
    packet["cases"][copy_id] = deepcopy(packet["cases"][case_id])
    for run in deepcopy([run for run in packet["runs"] if run["case"] == case_id]):
        pr = f"fixture/pr/{len(packet['runs']) + 1}"
        for row in [*run["launches"], run["stop"]]:
            if row is not None and "pr" in row:
                row["pr"] = pr
        run.update(case=copy_id, pr=pr)
        packet["runs"].append(run)
    return packet["cases"][copy_id]


def pre_launch_stop(run):
    run["launches"] = []
    run["stop"].update(
        phase="BLOCKED",
        issuance="not_issued",
        proof=None,
        through=0,
        round=0,
        review=None,
        **dict.fromkeys(("pr", "base_ref", "merge_base", "head", "tree")),
    )


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


@pytest.mark.parametrize(("stale", "error"), [(False, "first stop"), (True, "accepted needs-fix")])
def test_clean_or_stale_review_cannot_authorize_fix(packet, stale, error):
    launch = packet["runs"][0]["launches"][1]
    if stale:
        launch.update(outcome="stale_needs_fix", next_head=packet["runs"][0]["stop"]["head"])
    else:
        # An accepted clean round is a first stop: nothing, a FIX least of all, follows it.
        launch.update(outcome="clean", findings={"complete": True, "items": []})
    with pytest.raises(EvidenceError, match=error):
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
        start["parents"] = [intermediate]
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


@pytest.mark.parametrize("transition", ["drift", None])
def test_first_review_of_a_moved_head_needs_an_explicit_drift(packet, transition):
    run = packet["runs"][0]
    final = run["launches"][-1]
    run["launches"][1:] = [dict(final, round=1, transition=transition)]
    run["stop"].update(through=2, round=1, review=2)
    if transition is None:
        with pytest.raises(EvidenceError, match="first review candidate"):
            score(packet)
        return
    pair = next(row for row in score(packet)["pairs"] if row["case"] == "wrong-boundary")
    got = pair["runs"]["baseline"]
    assert got["completed_rounds"] == 1 and got["validated_success"]
    # The registered start stays pinned: the moved revision's review and receipt
    # are not starting evidence.
    assert not got["starting_review_accepted"] and got["starting_validation"] is None
    assert got["counts"]["recall"] is None and pair["status"] == "validation_unknown"


def test_missing_stale_payload_keeps_accepted_finding_coverage(packet):
    stale, clean = add_stale_round(packet, 0)
    expected = score(packet)["rates"]
    assert expected["recall"]["difference"] == "0"
    stale["findings"] = None
    report = score(packet)
    assert report["rates"] == expected
    got = report["pairs"][1]["runs"]["baseline"]
    assert got["review_record_complete"] and got["completed_rounds"] == 3
    # A retained stale payload is still checked.
    stale["findings"] = deepcopy(packet["runs"][0]["launches"][1]["findings"])
    with pytest.raises(EvidenceError, match="verdict"):
        score(packet)
    # A missing accepted payload still suppresses every finding rate.
    stale["findings"], clean["findings"] = None, None
    rates = score(packet)["rates"]
    assert rates["recall"]["difference"] is None and rates["precision"]["difference"] is None


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
        case["snapshots"][launch["head"]], parents=[launch["head"]], tree=tree
    )
    # The revision moved while the clean round ran: it completed stale.
    launch.update(outcome="stale_clean", next_head=new)
    with pytest.raises(EvidenceError, match="certificate does not belong"):
        score(packet)
    trailing = retry_review(launch, head=new, tree=tree, round=2, next_head=None)
    run["launches"].append(trailing)
    run["stop"].update(through=3, wall_ms=700)
    # A later reserved ordinal makes the certificate of an earlier one historical.
    with pytest.raises(EvidenceError, match="certificate does not belong"):
        score(packet)
    # A retry is no completed round to certify.
    run["stop"].update(head=new, tree=tree, round=2, review=3)
    with pytest.raises(EvidenceError, match="stop disagrees"):
        score(packet)
    no_certificate(run, through=3, round_=1, review=2, at=trailing)
    got = result(packet, "clean-boundary", "baseline")
    assert got["review_record_complete"] and got["completed_rounds"] == 1
    assert got["terminal_bound"] and got["certificate_issued"] is False


def test_accepted_clean_can_block_without_issuing_a_certificate(packet):
    run = packet["runs"][2]
    no_certificate(run, through=2, round_=1, review=2, at=run["launches"][1])
    got = result(packet, "clean-boundary", "baseline")
    assert got["review_record_complete"] and got["terminal_bound"]
    assert got["certificate_issued"] is False and got["validated_success"] is False


def test_starting_defect_keeps_an_introduction_before_the_starting_tip(packet):
    expected = score(deepcopy(packet))
    case = packet["cases"]["wrong-boundary"]
    introducing = introduce_before_start(case, "off-by-one")
    assert introducing not in (case["start"], case["merge_base"])
    # The starting inventory, not the introducing SHA, decides exposure.
    assert score(packet) == expected
    for arm in ("baseline", "treatment"):
        assert result(packet, "wrong-boundary", arm)["counts"]["recall"] == (4, 0, 4)


@pytest.mark.parametrize("introduced", ["successor", "sibling"])
def test_starting_defect_cannot_come_from_outside_the_starting_lineage(packet, introduced):
    case = packet["cases"]["wrong-boundary"]
    if introduced == "successor":
        later = "3" + "0" * 39
        case["snapshots"][later]["present"] = ["off-by-one"]
    else:
        later = "6" * 40
        case["snapshots"][later] = dict(case["snapshots"][case["start"]], tree="7" * 40)
    case["defects"]["off-by-one"]["introduced"] = later
    with pytest.raises(EvidenceError, match="starting defect inventory"):
        score(packet)


@pytest.mark.parametrize("introduction", ["starting tip", "earlier lineage commit"])
@pytest.mark.parametrize("held", ["held", "retry"])
def test_bound_nonissuance_after_fix_can_establish_a_new_shared_escape(packet, held, introduction):
    recorded(packet, "wrong-boundary")
    case = packet["cases"]["wrong-boundary"]
    if introduction != "starting tip":
        introduce_before_start(case, "off-by-one")
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


def test_regression_from_the_first_of_two_fix_commits_belongs_to_the_fix(packet):
    recorded(packet, "wrong-boundary")
    expected = score(deepcopy(packet))
    case = packet["cases"]["wrong-boundary"]
    endpoint = case["snapshots"]["4" + "0" * 39]
    first = "6" * 40
    # reviewed start -> first FIX commit (introduces lost-data) -> certified endpoint
    case["snapshots"][first] = dict(endpoint, tree="7" * 40)
    endpoint["parents"] = [first]
    case["defects"]["lost-data"]["introduced"] = first
    report = score(packet)
    assert report == expected  # The endpoint, not the commit count, is scored.
    got = report["pairs"][0]["runs"]["treatment"]
    assert got["fix_regressions"] == ["lost-data"] and got["known_harmful_fixes"] == 1
    assert report["rates"]["harmful_fix_rate"]["treatment"]["point"] == "1"
    assert got["certificate_issued"] and got["critical_escapes"] == {"lost-data": True}
    assert report["new_critical_escapes"] == [["wrong-boundary", 0, "lost-data"]]
    assert report["decision"] == "NO_GO"
    # A defect the second commit repairs is no regression of the endpoint.
    endpoint["present"] = []
    got = result(packet, "wrong-boundary", "treatment")
    assert got["fix_regressions"] == [] and got["critical_escapes"] == {"lost-data": False}


def fork_point(case, branch_point):
    """Where a side branch forks: a registered commit, or one below the merge base."""
    if branch_point == "below the merge base":
        # An older commit of the base branch, attested to lie in the merge
        # base's history; the side commit keeps it as its real parent.
        older = "8" * 40
        case["base_ancestors"] = [older]
        return older
    return {"reviewed revision": case["start"], "merge base": case["merge_base"]}[branch_point]


@pytest.mark.parametrize(
    "branch_point", ["reviewed revision", "merge base", "below the merge base"]
)
def test_regression_from_a_merged_side_branch_belongs_to_the_fix(packet, branch_point):
    recorded(packet, "wrong-boundary")
    expected = score(deepcopy(packet))
    case = packet["cases"]["wrong-boundary"]
    reviewed = case["start"]
    endpoint = case["snapshots"]["4" + "0" * 39]
    side = "6" * 40
    fork = fork_point(case, branch_point)
    # side-branch commit (introduces lost-data) -> FIX merge of reviewed revision and side
    case["snapshots"][side] = dict(endpoint, parents=[fork], tree="7" * 40)
    endpoint["parents"] = [reviewed, side]
    case["defects"]["lost-data"]["introduced"] = side
    report = score(packet)
    assert report == expected  # The real ancestry, not a flattened one, is scored.
    got = report["pairs"][0]["runs"]["treatment"]
    assert got["fix_regressions"] == ["lost-data"] and got["known_harmful_fixes"] == 1
    assert got["certificate_issued"] and got["critical_escapes"] == {"lost-data": True}
    assert report["new_critical_escapes"] == [["wrong-boundary", 0, "lost-data"]]
    # The side commit keeps its introducing SHA and its real parent.
    assert case["defects"]["lost-data"]["introduced"] == side
    assert case["snapshots"][side]["parents"] == [fork]
    # A defect the merge resolves away is no regression of the endpoint.
    endpoint["present"] = []
    got = result(packet, "wrong-boundary", "treatment")
    assert got["fix_regressions"] == [] and got["critical_escapes"] == {"lost-data": False}


@pytest.mark.parametrize("branch_point", ["merge base", "below the merge base"])
def test_starting_defect_from_a_merged_side_branch_keeps_its_introduction(packet, branch_point):
    expected = score(deepcopy(packet))
    case = packet["cases"]["wrong-boundary"]
    start = case["snapshots"][case["start"]]
    side, mainline = "6" * 40, "9" * 40
    case["snapshots"][side] = dict(start, parents=[fork_point(case, branch_point)], tree="7" * 40)
    case["snapshots"][mainline] = dict(
        start, parents=[case["merge_base"]], tree="a" * 40, present=[]
    )
    start["parents"] = [mainline, side]
    case["defects"]["off-by-one"]["introduced"] = side
    assert score(packet) == expected


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("registered as a snapshot", "base ancestors"),
        ("the merge base", "base ancestors"),
        ("repeated", "base ancestors"),
        ("unattested fork", "snapshot parent"),
        ("orphan side commit", "history of the registered merge base"),
        ("start below the merge base", "history of the registered merge base"),
        ("review of the side commit", "exact candidate"),
        ("stale move to the side commit", "different observed candidate"),
        ("fix of the side commit", "exact candidate"),
    ],
)
def test_a_base_ancestor_is_only_a_side_branch_boundary(packet, change, error):
    case = packet["cases"]["wrong-boundary"]
    snaps = case["snapshots"]
    run = packet["runs"][0]
    review, fix = run["launches"][1:3]
    older, side = fork_point(case, "below the merge base"), "6" * 40
    # A side commit forked below the merge base, merged by the published FIX.
    repaired = snaps[fix["published"]]
    snaps[side] = dict(repaired, parents=[older], tree="7" * 40)
    repaired["parents"] = [fix["head"], side]
    score(deepcopy(packet))
    if change == "registered as a snapshot":
        snaps[older] = dict(snaps[case["merge_base"]], tree="9" * 40)
    elif change == "the merge base":
        case["base_ancestors"].append(case["merge_base"])
    elif change == "repeated":
        case["base_ancestors"].append(older)
    elif change == "unattested fork":
        case["base_ancestors"] = []
    elif change == "orphan side commit":
        repaired["parents"] = [fix["head"]]
    elif change == "start below the merge base":
        snaps[case["start"]]["parents"] = [older]
    elif change == "review of the side commit":
        review.update(head=side, tree=snaps[side]["tree"])
    elif change == "stale move to the side commit":
        review.update(outcome="stale_needs_fix", next_head=side)
    else:
        fix.update(head=side, tree=snaps[side]["tree"])
    with pytest.raises(EvidenceError, match=error):
        score(packet)


@pytest.mark.parametrize(
    "graph", ["cycle", "unregistered parent", "repeated parent", "parent of the base", "root"]
)
def test_snapshot_graph_is_rooted_at_the_merge_base_and_acyclic(packet, graph):
    case = packet["cases"]["wrong-boundary"]
    snaps = case["snapshots"]
    start = snaps[case["start"]]
    if graph == "cycle":
        start["parents"] = [case["merge_base"], "3" + "0" * 39]  # That commit's parent is start.
    elif graph == "unregistered parent":
        start["parents"] = [case["merge_base"], "9" * 40]
    elif graph == "repeated parent":
        start["parents"] = [case["merge_base"]] * 2
    elif graph == "parent of the base":
        snaps[case["merge_base"]]["parents"] = [case["start"]]
    else:
        snaps["6" * 40] = dict(start, parents=[], tree="7" * 40, present=[])
    with pytest.raises(EvidenceError, match="snapshot (parent|ancestry)"):
        score(packet)


@pytest.mark.parametrize("endpoint", ["reviewed", "sibling"])
def test_fix_endpoint_must_descend_from_its_reviewed_revision(packet, endpoint):
    case = packet["cases"]["wrong-boundary"]
    fix = packet["runs"][0]["launches"][2]
    if endpoint == "reviewed":
        fix["published"] = fix["head"]
    else:
        sibling = "6" * 40
        repaired = case["snapshots"]["3" + "0" * 39]
        case["snapshots"][sibling] = dict(repaired, parents=[case["merge_base"]], tree="7" * 40)
        fix["published"] = sibling
    with pytest.raises(EvidenceError, match="FIX successor must descend"):
        score(packet)


def test_published_partial_fix_is_a_blocked_first_stop(packet):
    recorded(packet, "wrong-boundary")
    treatment = packet["runs"][1]
    fix = treatment["launches"][2]
    fix["outcome"] = "partial"
    # The controller waits for an explicit unblock; the next REVIEW is not this slice.
    with pytest.raises(EvidenceError, match="launch after the first stop"):
        score(packet)
    treatment["launches"].pop()
    # A certified stop at the published head is still not the BLOCKED partial stop.
    treatment["stop"].update(through=3, round=1, review=2)
    with pytest.raises(EvidenceError, match="certificate does not belong"):
        score(packet)
    endpoint = packet["cases"]["wrong-boundary"]["snapshots"][fix["published"]]
    no_certificate(
        treatment, through=3, round_=1, review=2, at={"head": fix["head"], "tree": fix["tree"]}
    )
    with pytest.raises(EvidenceError, match="stop disagrees"):
        score(packet)
    treatment["stop"].update(head=fix["published"], tree=endpoint["tree"])
    report = score(packet)
    got = report["pairs"][0]["runs"]["treatment"]
    assert got["stop"] == "BLOCKED" and got["terminal_bound"]
    assert got["certificate_issued"] is False and got["validated_success"] is False
    assert got["completed_rounds"] == 1 and got["fix_launches"] == 1
    # The published head keeps its harm evidence; a non-issuance certifies no escape.
    assert got["fix_regressions"] == ["lost-data"] and got["known_harmful_fixes"] == 1
    assert report["rates"]["harmful_fix_rate"]["treatment"]["point"] == "1"
    assert got["critical_escapes"] == {"lost-data": False}
    assert report["new_critical_escapes"] == []
    treatment["stop"]["phase"] = "FAILED"
    with pytest.raises(EvidenceError, match="stop phase its first stop cannot enter"):
        score(packet)
    treatment["stop"].update(phase="BLOCKED", through=4)
    with pytest.raises(EvidenceError, match="launch after the first stop"):
        score(packet)
    treatment["launches"].append(None)
    with pytest.raises(EvidenceError, match="launch after the first stop"):
        score(packet)


def test_partial_fix_is_a_first_stop_across_a_gap_before_it(packet):
    recorded(packet, "wrong-boundary")
    treatment = packet["runs"][1]
    fix = treatment["launches"][2]
    fix["outcome"] = "partial"
    endpoint = packet["cases"]["wrong-boundary"]["snapshots"][fix["published"]]
    treatment["launches"][0] = None  # The producer's slot is missing.
    # The retained partial FIX still ends the slice: the REVIEW after it is refused.
    with pytest.raises(EvidenceError, match="launch after the first stop"):
        score(packet)
    treatment["launches"].pop()
    # Nor does the gap excuse a certificate after it, at the published head.
    treatment["stop"].update(through=3, round=1, review=2)
    with pytest.raises(EvidenceError, match="certificate does not belong"):
        score(packet)
    no_certificate(
        treatment, through=3, round_=1, review=2, at={"head": fix["head"], "tree": fix["tree"]}
    )
    with pytest.raises(EvidenceError, match="stop disagrees"):
        score(packet)
    treatment["stop"].update(head=fix["published"], tree=endpoint["tree"])
    report = score(packet)
    got = report["pairs"][0]["runs"]["treatment"]
    # The gap leaves the terminal outcome unproven, never contradicted.
    assert got["stop"] == "BLOCKED" and not got["terminal_bound"]
    assert got["certificate_issued"] is None and got["validated_success"] is None
    # The unrelated gap does not erase the publication's own evidenced harm ...
    assert got["fix_regressions"] == ["lost-data"] and got["known_harmful_fixes"] == 1
    # ... while the pooled rate and the escape wait for the whole ledger.
    assert got["counts"]["harmful_fix_rate"] is None
    assert report["rates"]["harmful_fix_rate"]["treatment"]["point"] is None
    assert got["critical_escapes"] == {"lost-data": None}
    assert report["unresolved_critical_escapes"] == [["wrong-boundary", 0, "lost-data"]]
    treatment["stop"]["phase"] = "FAILED"
    with pytest.raises(EvidenceError, match="stop phase its first stop cannot enter"):
        score(packet)
    # A later watermark or a reserved ordinal contradicts the stop, gap or not.
    treatment["stop"].update(phase="BLOCKED", through=4)
    with pytest.raises(EvidenceError, match="launch after the first stop"):
        score(packet)
    treatment["launches"].append(None)
    with pytest.raises(EvidenceError, match="launch after the first stop"):
        score(packet)


@pytest.mark.parametrize("missing", ["producer", "source review"])
def test_an_unrelated_gap_keeps_a_published_fix_regression(packet, missing):
    recorded(packet, "wrong-boundary")
    expected = score(deepcopy(packet))["pairs"][0]["runs"]["treatment"]
    assert expected["fix_regressions"] == ["lost-data"] and expected["known_harmful_fixes"] == 1
    treatment = packet["runs"][1]
    treatment["launches"][0 if missing == "producer" else 1] = None
    report = score(packet)
    got = report["pairs"][0]["runs"]["treatment"]
    assert not got["lifecycle_complete"] and got["certificate_issued"] is None
    assert got["critical_escapes"] == {"lost-data": None}
    assert report["rates"]["harmful_fix_rate"]["treatment"]["point"] is None
    assert report["new_critical_escapes"] == []
    assert report["unresolved_critical_escapes"] == [["wrong-boundary", 0, "lost-data"]]
    # The publication, its retained source review, its exact range and the
    # endpoint's inventory are its own evidence; only without the review that
    # binds its revision does the harm stay unknown with the rest.
    harm = ["lost-data"] if missing == "producer" else []
    assert got["fix_regressions"] == harm and got["known_harmful_fixes"] == len(harm)


@pytest.mark.parametrize("gap", [False, True])
@pytest.mark.parametrize("first_stop", ["clean", "held", "partial"])
def test_a_first_stop_enters_only_its_own_stop_phases(packet, first_stop, gap):
    if first_stop == "clean":
        run = packet["runs"][2]  # The producer, then a clean round 1 of the start.
        no_certificate(run, through=2, round_=1, review=2, at=run["launches"][1])
        case, arm, permitted = "clean-boundary", "baseline", ("READY_FOR_MERGE", "BLOCKED")
    else:
        run = packet["runs"][1]
        run["launches"].pop()
        fix = run["launches"][-1]
        at = fix
        if first_stop == "held":
            fix.update(outcome="held", published=None, judgment=None)
        else:
            fix["outcome"] = "partial"
            endpoint = packet["cases"]["wrong-boundary"]["snapshots"][fix["published"]]
            at = {"head": fix["published"], "tree": endpoint["tree"]}
        no_certificate(run, through=3, round_=1, review=2, at=at)
        case, arm, permitted = "wrong-boundary", "treatment", ("BLOCKED",)
    if gap:
        run["launches"][0] = None  # An unrelated earlier slot is missing.
    got = result(packet, case, arm)
    assert got["stop"] == "BLOCKED" and got["terminal_bound"] is not gap
    assert got["certificate_issued"] is (None if gap else False)
    # A retained first stop fixes the phases the stop can enter, gap or not.
    run["stop"]["phase"] = "FAILED"
    with pytest.raises(EvidenceError, match="stop phase its first stop cannot enter"):
        score(packet)
    run["stop"].update(phase="READY_FOR_MERGE", issuance=None)
    if "READY_FOR_MERGE" in permitted:
        assert result(packet, case, arm)["certificate_issued"] is None
    else:
        with pytest.raises(EvidenceError, match="certificate does not belong"):
            score(packet)


@pytest.mark.parametrize(
    "record",
    [
        "consistent",
        "consistent next round",
        "later completed round",
        "named review missing",
        "next round's launch",
        "later fix of an earlier round",
        "watermark revision",
    ],
)
def test_a_retained_launch_contradicts_the_stop_across_a_gap(packet, record):
    run = packet["runs"][0]
    first, fix, second = run["launches"][1:]
    second.update(outcome="needs_fix", findings={"complete": False, "items": []})
    run["launches"][2] = None  # The FIX slot is missing.
    # Each stop below keeps the revision the last retained launch leaves, so
    # only the rule a case names is contradicted.
    if record == "consistent":
        no_certificate(run, through=4, round_=2, review=4, at=second)
    elif record == "later completed round":
        # Round 2 completed after the round 1 review the stop names as its last.
        no_certificate(run, through=4, round_=1, review=2, at=second)
    elif record == "named review missing":
        run["launches"][1:3] = [None, fix]
        no_certificate(run, through=4, round_=1, review=2, at=second)
    elif record in ("consistent next round", "next round's launch"):
        # A REVIEW launch after the named round is of the next one.
        size = 2 if record == "consistent next round" else 3
        run["launches"][3] = retry_review(second, round=size)
        no_certificate(run, through=4, round_=1, review=2, at=second)
    elif record == "later fix of an earlier round":
        run["launches"].append(dict(fix, outcome="retry", published=None, judgment=None))
        no_certificate(run, through=6, round_=2, review=4, at=second)
    else:
        # The retained last launch, a published FIX, leaves its endpoint current.
        run["launches"][1:] = [None, first, dict(fix, source=3)]
        no_certificate(run, through=4, round_=1, review=3, at=first)
    if record.startswith("consistent"):
        got = result(packet, "wrong-boundary", "baseline")
        assert got["stop"] == "BLOCKED" and not got["terminal_bound"]
        assert got["certificate_issued"] is None
        return
    with pytest.raises(EvidenceError, match="stop disagrees"):
        score(packet)


@pytest.mark.parametrize("gap", [False, True])
@pytest.mark.parametrize("presented", ["fail", "pass", None])
def test_ready_for_merge_refuses_a_named_review_over_failed_validation(packet, presented, gap):
    run = packet["runs"][2]  # The producer, then a clean round 1 of the start.
    review = run["launches"][1]
    # The review's disposition and the stop's issuance are both unknown.
    review.update(outcome=None, comment=None, findings=None)
    review["validation"]["result"] = presented
    run["stop"].update(issuance=None, proof=None)
    if gap:
        run["launches"][0] = None
    if presented == "fail":
        with pytest.raises(EvidenceError, match="passing or empty FULL validation"):
            score(packet)
        return
    got = result(packet, "clean-boundary", "baseline")
    assert got["stop"] == "READY_FOR_MERGE" and not got["terminal_bound"]
    assert got["certificate_issued"] is None and got["validated_success"] is None


@pytest.mark.parametrize("gap", [False, True])
def test_ready_for_merge_follows_only_a_clean_round(packet, gap):
    run = packet["runs"][0]
    run["launches"].pop()  # The run ends at its FIX.
    fix = run["launches"][-1]
    fix.update(outcome="held", published=None, judgment=None)
    if gap:
        run["launches"][0] = None
    run["stop"].update(
        issuance=None, proof=None, through=3, round=1, review=2, head=fix["head"], tree=fix["tree"]
    )
    # Issuance is unknown, but only the write completing a clean round enters READY_FOR_MERGE.
    with pytest.raises(EvidenceError, match="certificate does not belong"):
        score(packet)
    run["stop"]["phase"] = "BLOCKED"
    got = result(packet, "wrong-boundary", "baseline")
    assert got["certificate_issued"] is None and not got["terminal_bound"]
    assert got["review_record_complete"] is not gap


@pytest.mark.parametrize("gap", [False, True])
def test_no_launch_follows_an_accepted_clean_review(packet, gap):
    run = packet["runs"][2]  # The producer, then a clean round 1 of the start.
    case = packet["cases"][run["case"]]
    clean = run["launches"][1]
    proof = deepcopy(run["stop"]["proof"])
    moved, tree = "6" * 40, "7" * 40
    case["snapshots"][moved] = dict(
        case["snapshots"][clean["head"]], parents=[clean["head"]], tree=tree
    )
    if gap:
        run["launches"][0] = None
    drift = dict(clean, round=2, head=moved, tree=tree, transition="drift", comment="fixture/c/8")
    run["launches"].append(drift)
    run["stop"].update(through=3, round=2, review=3, head=moved, tree=tree, wall_ms=None)
    run["stop"]["proof"]["comment"] = drift["comment"]
    # The accepted clean round entered READY_FOR_MERGE or BLOCKED: no drift follows it.
    with pytest.raises(EvidenceError, match="launch after the first stop"):
        score(packet)
    run["launches"].pop()
    no_certificate(run, through=3, round_=1, review=2, at=clean)
    with pytest.raises(EvidenceError, match="launch after the first stop"):
        score(packet)
    # Drift during the review is recorded as a stale round, which the next REVIEW follows.
    clean.update(outcome="stale_clean", next_head=moved)
    final = dict(drift, transition=None)
    run["launches"].append(final)
    run["stop"].update(
        phase="READY_FOR_MERGE",
        issuance="issued",
        proof=dict(proof, comment=final["comment"]),
        round=2,
        review=3,
        head=moved,
        tree=tree,
    )
    got = result(packet, "clean-boundary", "baseline")
    assert got["completed_rounds"] == (None if gap else 2)
    assert got["certificate_issued"] is (None if gap else True)


def test_certificate_issued_into_blocked_after_configuration_drift(packet):
    """ADR 0006 D12.6: the clean round's certificate is issued and the same write blocks."""
    recorded(packet, "wrong-boundary")
    expected = score(deepcopy(packet))
    assert expected["pairs"][0]["runs"]["baseline"]["certificate_applicable"] is True
    baseline, treatment = packet["runs"]
    baseline["stop"]["phase"] = "BLOCKED"
    report = score(packet)
    got = report["pairs"][0]["runs"]["baseline"]
    # The issued certificate stays issued; it is never relabelled as non-issuance.
    assert got["terminal_bound"] and got["certificate_issued"] is True
    assert got["certificate_applicable"] is False
    # The configuration in force moved, which the fixed-policy slice does not hold.
    assert got["validated_success"] is None
    assert report["rates"] == expected["rates"]
    # It still certifies its candidate, so an escape stands, and a known failure stays one.
    treatment["stop"]["phase"] = "BLOCKED"
    report = score(packet)
    got = report["pairs"][0]["runs"]["treatment"]
    assert got["certificate_issued"] and got["critical_escapes"] == {"lost-data": True}
    assert got["validated_success"] is False
    assert report["new_critical_escapes"] == [["wrong-boundary", 0, "lost-data"]]
    # Neither shape is one the controller writes.
    baseline["stop"]["phase"] = "FAILED"
    with pytest.raises(EvidenceError, match="stop certificate shape"):
        score(packet)
    baseline["stop"].update(phase="READY_FOR_MERGE", issuance="not_issued", proof=None)
    with pytest.raises(EvidenceError, match="stop certificate shape"):
        score(packet)


def test_pre_launch_skip_needs_a_documented_pre_launch_stop(packet):
    recorded(packet, "wrong-boundary")
    treatment = packet["runs"][1]
    treatment["exclusion"] = "skipped"
    # The four-launch run executed; calling it skipped would drop it from rates.
    with pytest.raises(EvidenceError, match="skipped exclusion"):
        score(packet)
    pre_launch_stop(treatment)
    report = score(packet)
    assert report["pair_status"] == {"excluded": 1} and report["matched_pairs"] == 0
    assert report["pairs"][0]["runs"]["treatment"]["launches_observed"] == {}
    treatment["stop"]["through"] = 1
    with pytest.raises(EvidenceError, match="skipped exclusion"):
        score(packet)
    treatment["launches"] = [None]
    with pytest.raises(EvidenceError, match="skipped exclusion"):
        score(packet)
    treatment["launches"], treatment["stop"] = [], None
    with pytest.raises(EvidenceError, match="skipped exclusion"):
        score(packet)


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


@pytest.mark.parametrize(
    ("retry", "accepted", "status"),
    [
        (None, "same", "matched"),
        ("same", None, "matched"),
        (None, None, "validation_unknown"),
        ("other", "same", "changed validation set or policy"),
    ],
)
def test_starting_validation_is_any_receipt_of_the_starting_candidate(
    packet, retry, accepted, status
):
    run = packet["runs"][2]
    review = run["launches"][1]
    receipts = {
        None: None,
        "same": review["validation"],
        "other": dict(review["validation"], policy="c" * 64),
    }
    run["launches"][1:2] = [
        retry_review(review, validation=receipts[retry]),
        dict(review, validation=receipts[accepted]),
    ]
    run["stop"].update(through=3, review=3, wall_ms=None)
    if status not in ("matched", "validation_unknown"):
        with pytest.raises(EvidenceError, match=status):
            score(packet)
        return
    pair = next(row for row in score(packet)["pairs"] if row["case"] == "clean-boundary")
    assert pair["status"] == status
    got = pair["runs"]["baseline"]
    # The certificate's receipt never stands in for an absent review receipt.
    assert got["starting_validation"] == (
        pair["runs"]["treatment"]["starting_validation"] if status == "matched" else None
    )
    assert got["certificate_issued"] is (True if accepted else None)


def test_unknown_presented_result_keeps_comparability_but_not_the_certificate(packet):
    recorded(packet, "clean-boundary")
    baseline = packet["runs"][0]
    review = baseline["launches"][1]
    review["validation"] = dict(review["validation"], result=None)
    pair = score(packet)["pairs"][0]
    assert pair["status"] == "matched"
    got = pair["runs"]["baseline"]
    assert got["starting_validation"] == pair["runs"]["treatment"]["starting_validation"]
    # The certificate's passing result is not borrowed for the unknown receipt.
    assert got["certificate_issued"] is None and not got["terminal_bound"]
    assert got["validated_success"] is None
    review["validation"]["policy"] = "c" * 64
    with pytest.raises(EvidenceError, match="certificate validation binding"):
        score(packet)


@pytest.mark.parametrize("unknown", ["presented result", "issuance"])
def test_unknown_certificate_evidence_keeps_complete_finding_coverage(packet, unknown):
    expected = score(deepcopy(packet))["rates"]
    assert expected["false_positive_rate"]["difference"] == "1"
    baseline = packet["runs"][2]
    review = baseline["launches"][1]
    if unknown == "presented result":
        review["validation"]["result"] = None
    else:
        baseline["stop"].update(issuance=None, proof=None)
    got = result(packet, "clean-boundary", "baseline")
    # Issuance stays unproven and success unknown ...
    assert got["certificate_issued"] is None and got["validated_success"] is None
    assert not got["terminal_bound"]
    # ... while the reconciled launch window and accepted clean payload still count.
    assert got["review_record_complete"] and got["clean_negative_false_alarm"] is False
    assert score(packet)["rates"] == expected
    # A genuinely missing accepted payload, or the launch itself, is another matter.
    review["findings"] = None
    assert score(packet)["rates"]["false_positive_rate"]["difference"] is None
    baseline["launches"][1] = None
    got = result(packet, "clean-boundary", "baseline")
    assert not got["review_record_complete"] and got["clean_negative_false_alarm"] is None
    assert score(packet)["rates"]["false_positive_rate"]["difference"] is None


@pytest.mark.parametrize(
    ("contradiction", "error"),
    [
        (None, None),
        ("comment", "certificate comment binding"),
        ("policy", "certificate validation binding"),
        ("round", "stop disagrees"),
        ("revision", "stop disagrees"),
        ("needs-fix review", "certificate does not belong"),
    ],
)
def test_a_gap_does_not_mask_a_contradicted_certificate(packet, contradiction, error):
    run = packet["runs"][0]
    case = packet["cases"][run["case"]]
    run["launches"][2] = None  # The FIX slot is missing.
    stop, named = run["stop"], run["launches"][3]
    if contradiction == "comment":
        stop["proof"]["comment"] = "wrong-comment"
    elif contradiction == "policy":
        stop["proof"]["validation"]["policy"] = "d" * 64
    elif contradiction == "round":
        stop["round"] = 3
    elif contradiction == "revision":
        stop.update(head=case["start"], tree=case["snapshots"][case["start"]]["tree"])
    elif contradiction == "needs-fix review":
        named.update(outcome="needs_fix", findings={"complete": False, "items": []})
    if error is None:
        # A consistent gapped record leaves issuance unproven, not refused.
        got = result(packet, "wrong-boundary", "baseline")
        assert got["certificate_issued"] is None and not got["terminal_bound"]
        return
    with pytest.raises(EvidenceError, match=error):
        score(packet)


def test_receipt_of_a_later_candidate_is_not_starting_validation(packet):
    run = packet["runs"][0]
    review, final = run["launches"][1], run["launches"][-1]
    run["launches"][1:] = [
        retry_review(review, validation=None),
        dict(final, round=1, transition="drift"),
    ]
    run["stop"].update(through=3, round=1, review=3)
    pair = next(row for row in score(packet)["pairs"] if row["case"] == "wrong-boundary")
    assert pair["runs"]["baseline"]["validated_success"]
    assert pair["runs"]["baseline"]["starting_validation"] is None
    assert pair["status"] == "validation_unknown"


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


@pytest.mark.parametrize(
    ("case_id", "floor", "metric"),
    [("clean-boundary", "negative", "false_positive_rate"), ("wrong-boundary", "defect", "recall")],
)
def test_sample_floor_uses_the_cutoff_of_its_rates(packet, case_id, floor, metric):
    assert score(packet)["sample"] == {"defect": 1, "negative": 1}
    case = packet["cases"][case_id]
    case["snapshots"][case["start"]]["source"]["discovered"] = "2026-11-21"
    report = score(packet)
    assert report["sample"][floor] == 0
    assert report["rates"][metric]["baseline"]["point"] is None
    assert report["rates"][metric]["treatment"]["point"] is None


def test_a_cutoff_ineligible_case_drops_out_of_pooled_recall(packet):
    eligible_only = score(deepcopy(packet))
    assert eligible_only["rates"]["recall"]["usable"]
    late = copy_case(packet, "wrong-boundary", "late-boundary")
    late["snapshots"][late["start"]]["source"]["discovered"] = "2026-11-21"
    report = score(packet)
    assert report["matched_pairs"] == 3 and report["sample"] == eligible_only["sample"]
    pair = next(row for row in report["pairs"] if row["case"] == "late-boundary")
    # Zero eligible observations on both arms, not incomplete coverage ...
    for arm in ("baseline", "treatment"):
        assert pair["runs"][arm]["review_record_complete"]
        assert pair["runs"][arm]["counts"]["recall"] == (0, 0, 0)
    # ... so the pool and its draws are those of the eligible case alone.
    assert report["rates"]["recall"] == eligible_only["rates"]["recall"]
    # The recall population is fixed before coverage is: deleting only the
    # ineligible case's finding payloads changes neither recall nor its draws,
    # nor the FPR of the clean negative, whose population it is not in either.
    for run in packet["runs"]:
        if run["case"] == "late-boundary":
            run["launches"][1]["findings"] = None
    report = score(packet)
    for metric in ("recall", "false_positive_rate"):
        assert report["rates"][metric] == eligible_only["rates"][metric]
    # Precision observes whatever a review retained, so that gap is its own.
    assert report["rates"]["precision"]["difference"] is None
    # Missing coverage in an eligible case still suppresses the contrast.
    packet["runs"][0]["launches"][1]["findings"] = None
    recall = score(packet)["rates"]["recall"]
    assert recall["difference"] is None and not recall["usable"]


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
    # A reserved ordinal after the certified clean round contradicts its certificate.
    run["stop"]["through"] += 1
    with pytest.raises(EvidenceError, match="certificate does not belong"):
        score(packet)
    # A tail gap after a non-terminal launch is unknown, not zero.
    run["stop"]["through"] -= 1
    run["launches"].pop()
    got = result(packet, "wrong-boundary", "baseline")
    assert not got["launch_window_complete"] and got["launch_ms"] is None
    assert not got["terminal_bound"] and got["certificate_issued"] is None
    # The stop's own creation-to-stop clock is not a launch record.
    assert got["wall_ms"] == 850


def test_missing_launch_is_not_missing_wall_time(packet):
    run = packet["runs"][0]
    run["launches"][2] = None
    got = result(packet, "wrong-boundary", "baseline")
    assert not got["launch_window_complete"] and not got["terminal_bound"]
    assert got["launch_ms"] is None and got["validated_success"] is None
    assert got["wall_ms"] == 850
    run["stop"]["wall_ms"] = None
    assert result(packet, "wrong-boundary", "baseline")["wall_ms"] is None


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
