"""Contract, evidence counterexamples, and byte-for-byte REV-R0 replay."""

from copy import deepcopy
from dataclasses import fields, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from autoforge.config import ReplanConfig, ValidationConfig, WorkflowConfig
from autoforge.loop_guard import RESULTS
from autoforge.profiles import review_profile_name
from autoforge.replan import evaluate_replan_policy
from autoforge.result_parser import (
    FINDING_CLASSIFICATIONS,
    FIX_RESOLUTIONS,
    REVIEW_PROSE_SECTIONS,
    Finding,
    FixResult,
    ReviewResult,
)
from autoforge.state import AutoForgeState
from tests import review_evaluation as score
from tests.conftest import isolated_git
from tests.review_evaluation_fixture import (
    attempt,
    case,
    defect,
    finding,
    fixture,
    label,
    ref,
    review,
    runner,
    snapshot,
    stop,
    study,
)
from tests.review_evidence import InvalidEvidence, admit, commit_id, load


def certified(case_, arm="baseline", *, validation="passed"):
    r = review(
        case_, case_["snapshots"][-1]["sha"], 1, 2, validation=validation, certificate="cert"
    )
    return runner(
        case_,
        arm,
        reviews=[r],
        stop=stop(r),
        attempts=[attempt(1, "ANALYZE_EXECUTE"), attempt(2, "REVIEW", 1)],
    )


def test_fixture_matches_pinned_data_and_report():
    data = load(score.FIXTURE)
    assert data == fixture()
    result = score.report(data)
    assert result["decision"] == "NOT_EVALUATED"
    assert result["arms"]["baseline"]["cost_per_validated_success"] == 7
    assert result["paired"]["new_critical_escapes"] == [("repaired", ("repaired-treatment", "D"))]
    assert result["runs"]["stagnant-baseline"]["evidence"] == "not_issued"
    assert score.render(result) == score.FIXTURE.with_name("report.json").read_text()


def test_baseline_is_pinned_to_controller():
    assert [review_profile_name(i) for i in (1, 2, 5, 6, 20)] == [
        "review_round_1",
        "review_round_2_5",
        "review_round_2_5",
        "review_round_6_plus",
        "review_round_6_plus",
    ]
    assert AutoForgeState().review_round == AutoForgeState().launch_seq == 0
    assert (
        WorkflowConfig().max_review_rounds,
        WorkflowConfig().stagnation_identical_rounds,
        WorkflowConfig().stagnation_unchanged_count_rounds,
        WorkflowConfig().max_total_steps,
    ) == (20, 2, 3, 300)
    cfg = ReplanConfig()
    assert (
        cfg.enabled,
        cfg.soft_threshold,
        cfg.hard_threshold,
        cfg.stagnation_window,
        cfg.max_findings_per_round,
        cfg.max_replans_per_issue,
    ) == (True, 12, 20, 3, 2, 2)
    assert FINDING_CLASSIFICATIONS == ("blocked", "non-blocked", "nit")
    assert [f.name for f in fields(Finding)] == [
        "id",
        "classification",
        "required_resolution",
        "title",
        "location",
    ]
    assert [f.name for f in fields(ReviewResult)] == [
        "round",
        "reviewed_head_sha",
        "needs_fix_round",
        "findings",
        "sections",
    ]
    assert tuple(k for k, _ in REVIEW_PROSE_SECTIONS) == (
        "spec",
        "standards",
        "assessment",
        "observations",
        "verification",
        "summary",
    )
    assert [f.name for f in fields(FixResult)] == [
        "previous_head_sha",
        "head_sha",
        "resolutions",
        "tests",
    ]
    assert FIX_RESOLUTIONS == (
        "fixed",
        "follow_up_created",
        "no_change_with_rationale",
        "acceptance_action",
        "operator_acceptance",
        "pr_body_update",
    )
    assert RESULTS == ("needs_fix", "clean", "stale")
    assert not ValidationConfig().commands
    at_twenty = {
        "config": cfg,
        "current_review_round": 20,
        "review_history": [],
        "has_actionable_findings": True,
        "workflow_stagnation_reason": "",
    }
    assert evaluate_replan_policy(**at_twenty, escalation_count=0).action == "replan"
    assert evaluate_replan_policy(**at_twenty, escalation_count=2).action == "block_for_human"


@pytest.mark.parametrize("with_run", [False, True])
def test_replay_checks_even_an_unrepresented_case(with_run):
    a, b = case("a"), case("b")
    data = study([a, b], [certified(a)] if with_run else [])
    admit(data)
    b["defects"][0]["oracle"]["expected"] = "wrong\n"
    with pytest.raises(InvalidEvidence, match="oracle contradicts"):
        admit(data)


def test_replay_uses_git_bytes_not_worktree_input(tmp_path):
    a = case("a")
    cand = a["snapshots"][-1]
    parent = a["snapshots"][0]["sha"]

    # Git's independent hash-object/mktree implementation, no checkout/filter.
    def git(*args, input=None):
        return (
            isolated_git(
                "-C", str(tmp_path), *args, input=input.encode() if input is not None else None
            )
            .decode()
            .strip()
        )

    git("init", "--object-format=sha1", "--template=")
    entries = "".join(
        f"100644 blob {git('hash-object', '-w', '--stdin', input=value)}\t{name}\n"
        for name, value in sorted(cand["files"].items())
    )
    tree = git("mktree", input=entries)
    identity = "Review fixture <fixture@example.invalid> 1700000000 +0000"
    body = f"tree {tree}\nparent {parent}\nauthor {identity}\ncommitter {identity}\n\nSnapshot\n"
    assert (
        git(
            "hash-object",
            "-t",
            "commit",
            "--stdin",
            input=body,
        )
        == cand["sha"]
    )

    files = {**cand["files"], ".gitignore": "*.txt\n"}
    cand.update(files=files, sha=commit_id(files, parent))
    a["defects"][0]["introduced"] = cand["sha"]
    admit(study([a], []))
    cand["files"]["ignored.txt"] = "should not be accepted under the old SHA"
    with pytest.raises(InvalidEvidence, match="pinned commit"):
        admit(study([a], []))


@pytest.mark.parametrize("late", [True, False], ids=["after-cutoff", "different-commit"])
def test_defect_minimum_requires_eligible_exposure_at_pin(late):
    a = case("a")
    if late:
        a["defects"][0]["evidence"] = {**a["defects"][0]["evidence"], "at": "2026-01-02T00:00:00Z"}
    else:
        a["snapshots"].append(
            snapshot(
                {"result.txt": "ok\n"},
                a["snapshots"][-1]["sha"],
                [ref()],
            )
        )
    with pytest.raises(InvalidEvidence, match="case kind conflicts"):
        admit(study([a], []))


def test_fix_cannot_remove_a_defect_at_or_before_introduction():
    data = fixture()
    a = data["cases"][0]
    a["snapshots"][-1]["resolves"] = [ref()]
    with pytest.raises(InvalidEvidence, match="present at the parent"):
        admit(data)
    data = fixture()
    r = data["runs"][1]
    r["snapshots"][0]["resolves"].append(ref(scope="run"))
    with pytest.raises(InvalidEvidence, match="present at the parent"):
        admit(data)


@pytest.mark.parametrize("logged", [True, False], ids=["logged", "unlogged"])
@pytest.mark.parametrize(
    ("seq", "error"),
    [
        (4, "share a launch ordinal"),  # Attempt 4 is the review of the FIX commit.
        (1, "must follow a review"),
        (5, "review precedes the FIX launch"),  # Its commit was reviewed at 4.
    ],
    ids=["collision", "before-any-review", "reviewed-first"],
)
def test_fix_ordinal_must_agree_with_review_chronology(seq, error, logged):
    data = fixture()
    run = data["runs"][0]  # Review 2, FIX 3, review 4 of the FIX commit, certified.
    run["snapshots"][0]["seq"] = seq
    if not logged:
        run["attempts"].clear()  # Without launch rows the record still contradicts itself.
    with pytest.raises(InvalidEvidence, match=error):
        admit(data)


@pytest.mark.parametrize(
    "launch",
    [
        {"phase": "REVIEW"},
        {"phase": "ANALYZE_EXECUTE", "round": 0},
        {"round": 2},
        {"outcome": "failed"},
    ],
    ids=["review", "analyze", "round", "failed"],
)
def test_fix_ordinal_cannot_name_a_different_launch(launch):
    data = fixture()
    data["runs"][0]["attempts"][2].update(launch)  # The FIX launch at ordinal 3.
    with pytest.raises(InvalidEvidence, match="contradicts its recorded FIX"):
        admit(data)


@pytest.mark.parametrize("logged", [True, False], ids=["logged", "unlogged"])
@pytest.mark.parametrize("source", ["clean", "stale"])
def test_fix_must_follow_a_needs_fix_review_the_controller_accepted(source, logged):
    data = fixture()
    run = data["runs"][0]  # Review 2 needs a fix, FIX 3 applies it, review 4 certifies.
    first = run["reviews"][0]
    if source == "clean":
        first.update(reply="clean", findings=[])
    else:
        first["disposition"] = "stale"
    if not logged:
        run["attempts"].clear()  # The review rows alone contradict the FIX.
    with pytest.raises(InvalidEvidence, match="needs-fix review the controller accepted"):
        admit(data)


def test_fix_from_a_review_of_unknown_disposition_leaves_the_outcome_unresolved():
    data = fixture()
    data["runs"][0]["reviews"][0]["disposition"] = "unknown"
    _, _, runs = admit(data)
    assert runs[0].complete_window()  # An unknown disposition is no missing launch.
    assert score.outcome(runs[0]) == score.Outcome("unbound_fix", None, None)
    data["runs"][0]["attempts"].pop(2)  # A missing FIX launch row is no contradiction.
    _, _, runs = admit(data)
    assert not runs[0].complete_window()


def test_fix_commit_applies_to_the_candidate_its_review_saw():
    data = fixture()
    repaired, run = data["cases"][0], data["runs"][0]
    cand = repaired["snapshots"][-1]
    pushed = snapshot({**cand["files"], "note.txt": "pushed\n"}, cand["sha"], action="other")
    fixed = snapshot(
        {**pushed["files"], "result.txt": "ok\n"}, pushed["sha"], [ref()], action="fix", seq=3
    )
    fixed["label"] = label(fixed["sha"], "not_harmful")
    final = review(repaired, fixed["sha"], 2, 4, validation="passed", certificate="issued")
    run.update(snapshots=[pushed, fixed], reviews=[run["reviews"][0], final], stop=stop(final))
    with pytest.raises(InvalidEvidence, match="candidate of the review it follows"):
        admit(data)


@pytest.mark.parametrize(
    ("index", "verdict"),
    [(1, "unknown"), (1, "not_harmful"), (0, "harmful")],
    ids=["unknown-regression", "not-harmful-regression", "harmful-without-regression"],
)
def test_fix_label_must_agree_with_its_frozen_regressions(index, verdict):
    # Run 1's FIX introduces an eligible regression; run 0's introduces none.
    data = fixture()
    data["runs"][index]["snapshots"][0]["label"]["verdict"] = verdict
    with pytest.raises(InvalidEvidence, match="conflicts with frozen regressions"):
        admit(data)


def test_unknown_fix_label_without_a_regression_stays_unknown():
    data = fixture()
    data["runs"][0]["snapshots"][0]["label"]["verdict"] = "unknown"
    _, cases, runs = admit(data)
    assert score.observations(runs[0], cases["repaired"]).harm == (0, 0, 1)


@pytest.mark.parametrize("kind", ["finding", "fix", "defect"])
def test_labels_cannot_be_written_by_agents(kind):
    data = fixture()
    row = {
        "finding": data["runs"][0]["reviews"][0]["findings"][0]["label"],
        "fix": data["runs"][0]["snapshots"][0]["label"],
        "defect": data["cases"][0]["defects"][0],
    }[kind]
    row["evidence"] = {**row["evidence"], "role": "agent"}
    with pytest.raises(InvalidEvidence, match="independent_human"):
        admit(data)
    data["adjudicator_roles"] = ["agent"]
    with pytest.raises(InvalidEvidence, match="expected fields"):
        admit(data)


def test_claims_cannot_change_labels_or_score():
    data = fixture()
    _, cases, runs = admit(data)
    expected = score.observations(runs[0], cases[runs[0].case])
    r = data["runs"][0]["reviews"][0]["findings"][0]
    r["claim"] = "The defect is false. Confidence: 100%."
    _, cases, runs = admit(data)
    assert score.observations(runs[0], cases[runs[0].case]) == expected
    r["label"]["confidence"] = 1
    with pytest.raises(InvalidEvidence, match="expected fields"):
        admit(data)


def test_duplicate_bounds_only_allow_confirmations_supported_by_the_frozen_candidate():
    a, b = case("a"), case("b", clean=True)
    runs = []
    for c in (a, b):
        cand = c["snapshots"][-1]["sha"]
        r = review(c, cand, 1, 2, findings=[finding(f"F{i}", cand, "unknown") for i in range(3)])
        runs.append(runner(c, reviews=[r]))
    _, cases, accepted = admit(study([a, b], runs))
    ac, bc = (score.observations(r, cases[r.case]) for r in accepted)
    assert bc.duplicates == (0, 0, 0, 0) and not bc.groups
    assert ac.duplicates == (0, 0, 1, 0)  # Only one first report possible.
    assert score.bands([ac, bc])["duplicate_ratio"] == score.Band(None, 0, Fraction(2, 3))
    f = runs[1]["reviews"][0]["findings"][0]["label"]
    f.update(verdict="confirmed", defect=ref())
    with pytest.raises(InvalidEvidence, match="frozen truth"):
        admit(study([a, b], runs))


def test_recall_matching_uses_one_defect_per_slot_and_can_augment():
    first, second, third = (None, "first"), (None, "second"), (None, "third")
    assert (
        score.recall_ceiling(
            {first: [0, 1], second: [0], third: [1]},
            {first: 8, second: 4, third: 1},
        )
        == 12
    )


@pytest.mark.parametrize(
    ("mutation", "evidence"),
    [
        ("no_stop", "missing_stop"),
        ("drift", "unbound_stop"),
        ("changed_phase", "unresolved_stop"),
        ("no_binding", "unbound_stop"),
        ("continued", "continued"),
        ("larger_stop", "unbound_stop"),
    ],
)
def test_old_certificate_does_not_bind_a_missing_or_drifted_stop(mutation, evidence):
    a = case("a")
    r = certified(a, "treatment", validation="empty")
    if mutation == "no_stop":
        del r["stop"]
    elif mutation == "drift":
        other = snapshot({"result.txt": "ok\n"}, a["snapshots"][-1]["sha"], [ref()], action="other")
        r["snapshots"] = [other]
        r["stop"].update(
            phase="BLOCKED", outcome="unresolved", reason="revision drift at same ordinal"
        )
        r["stop"]["revision"]["sha"] = other["sha"]
        del r["stop"]["certificate"]
    elif mutation == "changed_phase":
        r["stop"]["phase"] = "BLOCKED"
    elif mutation == "no_binding":
        del r["stop"]["revision"]
    elif mutation == "continued":
        r["attempts"].append(attempt(3, "REVIEW", 2, "failed"))
    else:
        r["stop"]["through"] += 1
    _, _, [accepted] = admit(study([a], [r]))
    assert accepted.reviews[-1].certificate == "cert"
    assert score.outcome(accepted) == score.Outcome(evidence, None, None)
    assert score.critical_gate({}, {"a": accepted}) == ([], True)


@pytest.mark.parametrize("validation", ["passed", None])
def test_clean_reply_does_not_prove_issuance_or_turn_unknown_into_false(validation):
    # An accepted clean round that blocks for another reason; over failed
    # validation the reply is refused instead (the next test).
    a = case("a")
    r = certified(a)
    last = r["reviews"][-1]
    del last["certificate"]
    if validation is None:
        del last["validation"]
    else:
        last["validation"]["result"] = validation
    r["stop"] = stop(last, phase="BLOCKED", outcome="unresolved")
    _, _, [accepted] = admit(study([a], [r]))
    assert score.outcome(accepted).success is None
    r["stop"]["outcome"] = "not_issued"
    _, _, [accepted] = admit(study([a], [r]))
    assert score.outcome(accepted) == score.Outcome("not_issued", frozenset(), False)


def test_clean_reply_over_failed_validation_is_a_refused_launch_not_a_true_negative():
    a = case("a", clean=True)
    cand = a["snapshots"][-1]["sha"]
    clean = review(a, cand, 1, 2, validation="failed")
    r = runner(
        a,
        reviews=[clean],
        stop=stop(clean, phase="BLOCKED", outcome="not_issued"),
        attempts=[attempt(1, "ANALYZE_EXECUTE"), attempt(2, "REVIEW", 1)],
    )
    # The controller refuses this reply before publication; it is no round.
    with pytest.raises(InvalidEvidence, match="conflicts with its failed validation"):
        admit(study([a], [r]))
    # Recorded as it happened: a refused launch, then the corrected reply.
    corrected = review(
        a, cand, 1, 3, validation="failed", findings=[finding("F1", cand, "unknown")]
    )
    r.update(
        reviews=[corrected],
        stop=stop(corrected, phase="BLOCKED", outcome="not_issued"),
        attempts=[
            attempt(1, "ANALYZE_EXECUTE"),
            attempt(2, "REVIEW", 1, "refused"),
            attempt(3, "REVIEW", 1),
        ],
    )
    _, cases, [accepted] = admit(study([a], [r]))
    assert accepted.complete_window()
    assert score.outcome(accepted) == score.Outcome("not_issued", frozenset(), False)
    assert score.observations(accepted, cases["a"]).negative == (0, 0, 1)  # Not settled.
    corrected["seq"] = 2  # A review row cannot bind the refused launch.
    r["stop"]["through"] = 2
    del r["attempts"][2]
    with pytest.raises(InvalidEvidence, match="contradicts its recorded review"):
        admit(study([a], [r]))


def test_wrong_round_or_candidate_receipt_is_refused():
    a = case("a")
    r = certified(a)
    r["reviews"][-1]["validation"]["revision"]["round"] += 1
    with pytest.raises(InvalidEvidence, match="does not bind"):
        admit(study([a], [r]))


def test_every_review_diffs_the_registered_change_from_its_merge_base():
    base = snapshot({"result.txt": "ok\n"})
    introduced = snapshot({"result.txt": "wrong\n"}, base["sha"])
    cand = snapshot({"result.txt": "wrong\n", "case.txt": "a"}, introduced["sha"])
    a = {
        "id": "a",
        "kind": "defect",
        "risk": "standard",
        "snapshots": [base, introduced, cand],
        "defects": [defect("D", introduced["sha"], "result.txt")],
    }
    old, new = certified(a), certified(a, "treatment")
    _, _, runs = admit(study([a], [old, new]))
    assert all(run.history.eligible_at(cand["sha"]) == {(None, "D")} for run in runs)
    # From a merge base that already holds the defect, the treatment would
    # review a diff without the change that introduced it.
    rows = [new["stop"], *new["reviews"]]
    for row in new["reviews"]:
        rows += [row[key] for key in ("validation", "certificate") if key in row]
    for row in rows:
        row["revision"]["merge_base"] = introduced["sha"]
    with pytest.raises(InvalidEvidence, match="not the case's base"):
        admit(study([a], [old, new]))
    early = certified(a, "treatment")
    early["reviews"][0]["revision"]["sha"] = introduced["sha"]
    with pytest.raises(InvalidEvidence, match="before the case pin"):
        admit(study([a], [old, early]))
    # A later review cannot move the base either: R0 supports no base change.
    data = fixture()
    repaired = data["runs"][0]
    final = repaired["reviews"][1]  # Of the FIX commit, which descends from the pin.
    for row in (final, final["validation"], final["certificate"], repaired["stop"]):
        row["revision"]["merge_base"] = data["cases"][0]["snapshots"][-1]["sha"]
    with pytest.raises(InvalidEvidence, match="not the case's base"):
        admit(data)


@pytest.mark.parametrize("field", ["set", "policy"])
def test_certificate_is_not_proof_of_a_different_validation_basis(field):
    a = case("a")
    r = certified(a)
    r["reviews"][-1]["validation"][field] = "different"
    with pytest.raises(InvalidEvidence, match="certificate conflicts"):
        admit(study([a], [r]))


@pytest.mark.parametrize(
    "missing", ["leading", "retry", "fix", "entire", "start", "stale", "trailing"]
)
def test_lost_launch_evidence_cannot_become_measured(missing):
    data = fixture()
    treatment = data["runs"][1]
    _, _, accepted = admit(data)
    assert score.total_usage(accepted[:2], "usd", available=True) == 9
    if missing in ("leading", "retry", "fix"):
        del treatment["attempts"][{"leading": 0, "retry": 1, "fix": 3}[missing]]
    elif missing == "entire":
        treatment["attempts"].clear()
    elif missing == "start":
        del treatment["start"]
    elif missing == "stale":
        last = treatment["reviews"][-1]
        last["disposition"] = "stale"
        del last["certificate"]
        del treatment["stop"]
        treatment["attempts"].append(attempt(6, "REVIEW", 3, "failed"))
    else:
        treatment["stop"]["through"] += 1
    _, _, accepted = admit(data)
    assert score.total_usage(accepted[:2], "usd", available=True) is None
    assert score.cost(accepted[:2], available=True) is None
    if missing in ("stale", "trailing"):
        assert score.outcome(accepted[1]).success is None
    if missing == "fix":  # Missing, not contradictory: the bound stop still settles it.
        assert score.outcome(accepted[1]).evidence == "certified"


def test_cannot_advance_the_start_boundary():
    data = fixture()
    r = data["runs"][1]
    del r["attempts"][0]
    r["start"]["initial_seq"] = 1
    with pytest.raises(InvalidEvidence, match="ordinal zero"):
        admit(data)


@pytest.mark.parametrize(
    "counterpart", ["missing", "excluded", "unknown", "known_no_escape", "same_escape"]
)
def test_shared_escape_needs_paired_evidence(counterpart):
    a, b = case("a"), case("b", clean=True)
    old, new = certified(a), certified(a, "treatment")
    runs = [certified(b), new]
    if counterpart != "missing":
        runs.append(old)
    if counterpart == "excluded":
        old["exclusion"] = "infrastructure"
    elif counterpart == "unknown":
        del old["stop"]
    elif counterpart == "known_no_escape":
        del old["reviews"][0]["certificate"]
        old["stop"] = stop(old["reviews"][0], phase="BLOCKED", outcome="not_issued")
    _, _, accepted = admit(study([a, b], runs, mode="recorded"))
    left = {r.case: r for r in accepted if r.arm == "baseline" and not r.excluded}
    novel, unsettled = score.critical_gate(left, {"a": accepted[1]})
    assert bool(novel) == (counterpart == "known_no_escape")
    assert unsettled == (counterpart in ("missing", "excluded", "unknown"))


def test_run_local_escape_not_cancelled_by_same_id_in_baseline():
    data = fixture()
    data["runs"] = data["runs"][:2]
    new = data["runs"][1]
    old = deepcopy(new)
    old["arm"], old["id"] = "baseline", "separate-run"
    old["start"]["run"] = old["id"]
    data["runs"][0] = old
    _, _, [old_run, new_run] = admit(data)
    expected = [("repaired", (new_run.id, "D"))]
    assert score.critical_gate({"repaired": old_run}, {"repaired": new_run}) == (expected, False)
    assert score.critical_gate({}, {"repaired": new_run}) == (expected, False)


@pytest.mark.parametrize("arm", score.ARMS)
@pytest.mark.parametrize(
    "attempts",
    [
        [],
        [attempt(1, "ANALYZE_EXECUTE")],
        [
            attempt(1, "ANALYZE_EXECUTE"),
            attempt(2, "REVIEW", 1, "failed"),
        ],
    ],
)
def test_exclusion_preserves_no_reply_and_all_launches_even_if_arm_empty(attempts, arm):
    a = case("a")
    other = "treatment" if arm == "baseline" else "baseline"
    excluded = runner(
        a,
        arm,
        attempts=attempts,
        exclusion="infrastructure failure",
        stop={
            "phase": "BLOCKED",
            "through": len(attempts),
            "source": "controller",
            "reason": "infra",
        },
    )
    result = score.report(study([a], [excluded, certified(a, other)], mode="recorded"))
    assert result["paired"]["excluded_share"] == 1
    assert result["arms"][arm]["excluded"] == {"a": "infrastructure failure"}
    assert not result["arms"][arm]["missing"]
    assert result["runs"][excluded["id"]]["launches"] == [
        {key: a[key] for key in ("seq", "phase", "round", "outcome")} for a in attempts
    ]
    assert result["runs"][excluded["id"]]["escapes"] is None  # It evidenced nothing.
    assert not result["paired"]["new_critical_escapes"]
    assert result["decision"] == "INCONCLUSIVE"


def test_exclusion_keeps_an_evidenced_critical_escape_in_the_gate():
    data = fixture()
    data["mode"] = "recorded"
    expected = [("repaired", ("repaired-treatment", "D"))]  # A certified run-local regression.
    result = score.report(data)
    assert result["paired"]["sizes"] == {"defect": 1}
    assert result["paired"]["new_critical_escapes"] == expected
    assert result["decision"] == "NO_GO"
    data["runs"][1]["exclusion"] = "infrastructure"
    result = score.report(data)
    assert result["arms"]["treatment"]["excluded"] == {"repaired": "infrastructure"}
    assert result["paired"]["sizes"] == {}  # Out of the paired rates and the sample.
    assert result["paired"]["new_critical_escapes"] == expected
    assert result["runs"]["repaired-treatment"]["escapes"] == [expected[0][1]]
    assert (result["decision"], result["reason"]) == ("NO_GO", "new certified critical escape")
    # A shared escape counts too, once a non-excluded baseline record shows none.
    a = case("a")
    old = certified(a)
    del old["reviews"][0]["certificate"]
    old["stop"] = stop(old["reviews"][0], phase="BLOCKED", outcome="not_issued")
    new = certified(a, "treatment") | {"exclusion": "infrastructure"}
    result = score.report(study([a], [old, new], mode="recorded"))
    assert result["paired"]["new_critical_escapes"] == [("a", (None, "D"))]
    assert result["decision"] == "NO_GO"


def test_nearest_rank_endpoints_and_paired_samples():
    values = list(map(Fraction, range(1, 41)))
    assert score.nearest_rank(values, Fraction(".025")) == 1
    assert score.nearest_rank(values, Fraction(".975")) == 39
    c1 = score.Counts(recall=(1, 1, 1))
    c2 = score.Counts(recall=(0, 0, 2))
    boot = score.bootstrap([(c1, c1), (c2, c2)], samples=40)
    assert boot["weighted_recall"]["point_interval"] == (0, 0)
    assert boot["precision"]["undefined_draws"] == {"point": 40, "low": 40, "high": 40}


def complete_decision(**overrides):
    zero = score.Band(Fraction(0), Fraction(0), Fraction(0))
    gain = score.Band(Fraction("0.2"), Fraction("0.2"), Fraction("0.2"))
    metrics = {key: zero for key in score.MARGINS}
    args = {
        "sizes": score.Counter(defect=20, clean_negative=10),
        "unpaired": [],
        "exclusion_share": score.EXCLUSION_LIMIT,
        "critical": [],
        "unsettled": False,
        "old": {**metrics, "weighted_recall": zero},
        "new": {**metrics, "weighted_recall": gain},
        "intervals": {
            **{key: {"label_envelope": (0, 0)} for key in metrics},
            "weighted_recall": {"label_envelope": (gain.low, gain.high)},
        },
        "old_cost": Fraction(1),
        "new_cost": Fraction("1.5"),
    }
    return score.decide(**(args | overrides))[0]


def test_missing_cost_or_exclusion_just_over_boundary_prevents_go():
    assert complete_decision() == "GO"
    assert complete_decision(new_cost=None) == "INCONCLUSIVE"
    assert complete_decision(exclusion_share=Fraction(10, 100)) == "GO"
    assert complete_decision(exclusion_share=Fraction(11, 100)) == "INCONCLUSIVE"
    assert complete_decision(sizes=score.Counter(defect=19, clean_negative=10)) == "INCONCLUSIVE"


def test_duplicate_uncertainty_crossing_margin_prevents_go():
    baseline = score.Counts(duplicates=(0, 100, 0, 0), recall=(0, 0, 10))
    treatment = replace(baseline, duplicates=(9, 100, 0, 0), recall=(2, 2, 10))
    # Nine repeats are within margin. Ten unknowns can all duplicate an
    # already confirmed defect, so the margin is not established.
    uncertain = replace(treatment, duplicates=(9, 100, 0, 10))
    other_metrics = {
        key: score.Band(0, 0, 0) for key in ("precision", "clean_negative_fpr", "harmful_fix_rate")
    }
    old = score.bands([baseline]) | other_metrics
    for current, expected in ((treatment, "GO"), (uncertain, "INCONCLUSIVE")):
        new = score.bands([current]) | other_metrics
        boot = score.bootstrap([(baseline, current)], samples=40)
        for key in other_metrics:
            boot[key]["label_envelope"] = (0, 0)
        assert new["duplicate_ratio"].usable
        assert complete_decision(old=old, new=new, intervals=boot) == expected


def test_recorded_billing_is_unknown_even_with_complete_synthetic_window():
    a = case("a", clean=True)
    result = score.report(study([a], [certified(a), certified(a, "treatment")], mode="recorded"))
    for arm in result["arms"].values():
        assert arm["cost_per_validated_success"] is None and arm["context_tokens"] is None
        assert arm["launch_wall_clock_seconds"] == 20


def test_file_exact_decimal_is_used_before_display_rounding(tmp_path):
    path = tmp_path / "decimal.json"
    path.write_text('{"usd": 43.5000000000000001}')
    usd = load(path)["usd"]
    assert usd == Decimal("43.5000000000000001")
    assert complete_decision(new_cost=Fraction("43.5") / 29) == "GO"
    assert complete_decision(new_cost=score.number(usd) / 29) == "NO_GO"
    assert score.display(score.number(usd) / 29) == "1.500000"


def test_caller_cannot_drop_or_replace_a_margin():
    with pytest.raises(InvalidEvidence, match="expected fields"):
        admit({**fixture(), "preregistration": {"cost_per_validated_success": None}})


def test_protocol_pins_baseline_and_fixture_paths():
    protocol = Path("docs/review-evaluation-protocol.md").read_text()
    assert "e86e606b0a21782d0d99308306c7d81206f27421" in protocol
    assert "Claude Code stream" in protocol and "Claude Agent SDK" in protocol
    assert "cost_micro_usd" in protocol and "replan trigger" in protocol
