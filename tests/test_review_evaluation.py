"""The REV-R0 review evaluation contract (#355): its files, its scorer and its protocol.

``tests/review_evaluation.py`` scores adjudicated labels of review findings and
decides between two arms. These tests hold it to
``docs/review-evaluation-protocol.md``: the fixture corpus replays as pinned, the
fixture scores are the ones the protocol states, a label never comes from an
agent's word on its own work, and the pre-registered decision is reached in its
registered order. Every run here is scripted or synthetic; nothing calls a model.
"""

from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path

import pytest

from autoforge.config import default_config
from autoforge.loop_guard import RESULTS, review_record
from autoforge.profiles import review_profile_name
from autoforge.result_parser import (
    FINDING_CLASSIFICATIONS,
    FIX_RESOLUTIONS,
    MAX_FINDINGS_PER_REVIEW,
)
from tests import review_evaluation as rv

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = REPO_ROOT / "docs" / "review-evaluation-protocol.md"
ARM = "current_review"
LABELED = "2026-11-24T12:00:00+00:00"
AUTHOR = {"id": "review-fixture-author", "role": "fixture_author"}


def _files() -> tuple[dict, dict, dict]:
    return (
        rv.read_json(rv.PREREGISTRATION),
        rv.read_json(rv.CORPUS),
        rv.read_json(rv.FIXTURE_RUNS),
    )


def _run(runs: dict, case: str) -> dict:
    (found,) = [r for r in runs["runs"] if r["case"] == case]
    return found


def _case(corpus: dict, case: str) -> dict:
    (found,) = [c for c in corpus["cases"] if c["id"] == case]
    return found


def _label(run: dict, finding: str, round: int = 1) -> dict:
    (found,) = [x for x in run["labels"] if x["finding"] == finding and x["round"] == round]
    return found


def _refused(prereg: dict, corpus: dict, runs: dict, needle: str) -> None:
    with pytest.raises(rv.ReviewEvaluationError, match=needle):
        rv.check(prereg, corpus, runs)


def _metrics(prereg: dict, corpus: dict, runs: dict) -> dict:
    return rv.report(rv.check(prereg, corpus, runs))["arms"][ARM]["metrics"]


# -- the registered files -----------------------------------------------------------------


def test_the_registered_files_pass_the_check():
    ev = rv.load()
    assert sorted(ev.cases) == sorted(c["id"] for c in rv.read_json(rv.CORPUS)["cases"])
    assert {run.arm for run in ev.runs} == {ARM}
    assert {run.provenance for run in ev.runs} == {"fixture"}
    assert sorted(run.case.id for run in ev.runs) == sorted(ev.cases)


def test_the_corpus_replays_as_pinned(tmp_path):
    """Every commit builds to its pinned SHA, and every oracle fails where the ground
    truth holds its defect and passes where it does not."""
    ev = rv.load()
    assert rv.verify(ev, tmp_path) == []
    expectations = [e for run in ev.runs for e in rv.oracle_expectations(run)]
    assert ("B1", "fix-1", "fail") in expectations  # the escaped critical, at the clean HEAD
    assert ("C2", "fix-1", "fail") in expectations  # the harmful fix's regression
    assert ("C2", "candidate", "pass") in expectations
    assert {outcome for _, _, outcome in expectations} == {"pass", "fail"}


def test_a_commit_that_does_not_build_to_its_pin_is_reported(tmp_path):
    prereg, corpus, runs = _files()
    _case(corpus, "rename-slug-pattern")["commits"][1]["files"]["slug.py"] += "\n"
    problems = rv.verify(rv.check(prereg, corpus, runs), tmp_path)
    assert len(problems) == 1
    assert "'candidate' is pinned at" in problems[0] and "builds as" in problems[0]


def test_an_oracle_that_does_not_show_its_defect_is_reported(tmp_path):
    """A candidate that no longer holds A1 makes its oracle pass where the ground truth
    says it fails: the labels would rest on a defect nothing shows."""
    prereg, corpus, runs = _files()
    candidate = _case(corpus, "page-count-partial-page")["commits"][1]["files"]
    candidate["pages.py"] = candidate["pages.py"].replace("total // size", "-(-total // size)")
    problems = rv.verify(rv.check(prereg, corpus, runs), tmp_path)
    assert any("A1's oracle should fail at 'candidate' and gave pass" in p for p in problems)


def test_an_oracle_that_crashes_shows_no_defect(tmp_path):
    """Only an AssertionError is a failure: an import error is no evidence of a defect."""
    prereg, corpus, runs = _files()
    oracles = _case(corpus, "retry-attempt-bound")["oracles"]
    oracles["attempt_count"] = "from retry import no_such_name\n"
    problems = rv.verify(rv.check(prereg, corpus, runs), tmp_path)
    assert any("gave error: ImportError" in p for p in problems)


def test_an_oracle_runs_isolated_from_the_hosts_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "F" * 36)
    source = (
        "import os\nassert 'GH_TOKEN' not in os.environ\nassert not os.environ.get('PYTHONPATH')\n"
    )
    assert rv.run_oracle(source, {"x.py": ""}, tmp_path) == "pass"


# -- the fixture's scores -----------------------------------------------------------------


def test_the_fixture_report_is_byte_identical_and_pinned():
    first = rv.dumps(rv.report(rv.load()))
    assert first == rv.dumps(rv.report(rv.load()))
    assert first == rv.FIXTURE_REPORT.read_text(encoding="utf-8")


def test_the_fixture_scores_are_the_ones_the_protocol_states():
    report = rv.report(rv.load())
    assert report["not_evidence"] == rv.NOT_EVIDENCE
    arm = report["arms"][ARM]
    m = arm["metrics"]
    # Detected A1 (major 4), A2 (minor 2), B2 (minor 2), C1 (major 4) of 24 exposed weight;
    # B1 (critical 8) and C2 (major 4) missed. B's unknown finding could have been B1.
    assert m["weighted_recall"]["value"] == 0.5
    assert m["weighted_recall"]["label_bounds"] == [0.5, 0.833333]
    # 5 confirmed, 2 false, 3 unknown.
    assert m["precision"]["value"] == 0.714286
    assert m["precision"]["label_bounds"] == [0.5, 0.8]
    # One clean negative clean, one with a false finding, one with unknown ones only.
    assert m["clean_negative_fpr"]["value"] == 0.5
    assert m["clean_negative_fpr"]["label_bounds"] == [0.333333, 0.666667]
    assert m["harmful_fix_rate"]["value"] == 0.333333
    assert m["duplicate_ratio"]["value"] == 0.2
    for metric in ("weighted_recall", "precision", "clean_negative_fpr"):
        assert m[metric]["status"] == "unknown", metric
    for metric in ("harmful_fix_rate", "duplicate_ratio"):
        assert m[metric]["status"] == "known", metric
    assert m["escaped_critical_defects"] == {
        "value": 1,
        "defects": [{"case": "redact-authorization-header", "defect": "B1"}],
    }
    assert m["cost_per_validated_success"] == {
        "provenance": "synthetic",
        "validated_successes": 3,
        "value": 3.666667,
    }
    assert m["context_tokens"]["value"] is None
    rows = {row["case"]: row for row in arm["runs"]}
    assert [c for c, row in sorted(rows.items()) if row["validated_success"]] == [
        "document-empty-chunks",
        "page-count-partial-page",
        "rename-slug-pattern",
    ]
    assert rows["page-count-partial-page"]["late_defects"] == ["A3"]
    assert rows["retry-attempt-bound"]["escaped"] == [{"defect": "C2", "severity": "major"}]
    assert rows["type-hint-cache-key"]["ended"] == "stopped"
    assert arm["loops"]["rounds_by_profile"] == {"review_round_1": 6, "review_round_2_5": 5}


def test_every_interval_is_reproducible_from_its_registered_seed():
    prereg = rv.read_json(rv.PREREGISTRATION)
    assert rv.bootstrap_indices(6, prereg) == rv.bootstrap_indices(6, prereg)
    other = copy.deepcopy(prereg)
    other["uncertainty"]["seed"] += 1
    assert rv.bootstrap_indices(6, other) != rv.bootstrap_indices(6, prereg)
    assert len(rv.bootstrap_indices(6, prereg)) == prereg["uncertainty"]["resamples"]


def test_a_late_defect_counts_only_once_an_amendment_moves_the_cutoff():
    prereg, corpus, runs = _files()
    before = _metrics(prereg, corpus, runs)
    corpus["discovery_cutoff"] = "2026-11-26T00:00:00+00:00"
    after = _metrics(prereg, corpus, runs)
    # A3 (minor 2) joins the exposed weight, undetected, and escapes at A's clean HEAD.
    assert before["weighted_recall"]["value"] == 0.5
    assert after["weighted_recall"]["value"] == round(12 / 26, 6)
    # A minor escape blocks no success.
    assert after["cost_per_validated_success"]["validated_successes"] == 3


# -- what is never a label ----------------------------------------------------------------


def test_what_the_agents_say_of_their_own_work_moves_no_score():
    """The reviewer's classification, prose and stated confidence, and the fixer's
    resolution kinds, are claims: rewriting all of them, with an assertion of certainty,
    changes no metric."""
    prereg, corpus, runs = _files()
    before = _metrics(prereg, corpus, runs)
    swap = {"blocked": "nit", "non-blocked": "blocked", "nit": "non-blocked"}
    for run in runs["runs"]:
        for review in run["reviews"]:
            result = review["result"]
            result["verification"] = "I am certain every finding is correct and complete."
            result["assessment"] = "Confidence: 100%."
            result["confidence"] = 1.0  # the controller's parser drops it, and so does the scorer
            for finding in result["findings"]:
                finding["classification"] = swap[finding["classification"]]
        for fix in run["fixes"]:
            fix["resolutions"] = dict.fromkeys(fix["resolutions"], "fixed")
    assert _metrics(prereg, corpus, runs) == before


@pytest.mark.parametrize(
    "field", ["confidence", "self_reported", "agent_verdict", "classification"]
)
def test_a_label_carrying_an_agents_confidence_or_verdict_is_refused(field):
    prereg, corpus, runs = _files()
    _label(_run(runs, "page-count-partial-page"), "R1-F1")[field] = 0.99
    _refused(prereg, corpus, runs, "never a label")


@pytest.mark.parametrize("role", ["reviewer", "fixer", "agent", "model", "self"])
def test_an_agent_is_no_adjudicator(role):
    prereg, corpus, runs = _files()
    _label(_run(runs, "redact-authorization-header"), "R1-F2")["adjudicator"]["role"] = role
    _refused(prereg, corpus, runs, "self-reported correctness is refused")


def test_an_agent_adjudicating_a_defect_or_a_fix_is_refused():
    prereg, corpus, runs = _files()
    _case(corpus, "retry-attempt-bound")["defects"][0]["adjudicator"]["role"] = "reviewer"
    _refused(prereg, corpus, runs, "self-reported correctness is refused")
    prereg, corpus, runs = _files()
    _run(runs, "retry-attempt-bound")["fixes"][0]["label"]["adjudicator"]["role"] = "fixer"
    _refused(prereg, corpus, runs, "self-reported correctness is refused")


def test_the_scorer_holds_no_engine_github_client_or_provider():
    """It reads files and launches only git and the oracles: nothing it imports can run
    an agent, write to GitHub or advance a run."""
    tree = ast.parse(Path(rv.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    autoforge = {name for name in imported if name.startswith("autoforge")}
    assert autoforge == {
        "autoforge.errors",
        "autoforge.executor",
        "autoforge.git_transport",
        "autoforge.profiles",
        "autoforge.result_parser",
    }


# -- what the check refuses ---------------------------------------------------------------


def test_every_finding_has_exactly_one_label():
    prereg, corpus, runs = _files()
    run = _run(runs, "redact-authorization-header")
    run["labels"] = [x for x in run["labels"] if x["finding"] != "R1-F3"]
    _refused(prereg, corpus, runs, "R1-F3 has no label")
    prereg, corpus, runs = _files()
    run = _run(runs, "redact-authorization-header")
    run["labels"].append(copy.deepcopy(run["labels"][0]))
    _refused(prereg, corpus, runs, "twice")
    prereg, corpus, runs = _files()
    run = _run(runs, "rename-slug-pattern")
    run["labels"].append(
        {
            "finding": "R1-F1",
            "round": 1,
            "candidate_sha": run["reviews"][0]["result"]["reviewed_head_sha"],
            "verdict": "false",
            "reason": "No such finding.",
            "adjudicator": AUTHOR,
            "labeled_at": LABELED,
        }
    )
    _refused(prereg, corpus, runs, "no review raised")


def test_a_label_is_bound_to_the_candidate_it_judged():
    prereg, corpus, runs = _files()
    _label(_run(runs, "page-count-partial-page"), "R1-F1")["candidate_sha"] = "a" * 40
    _refused(prereg, corpus, runs, "names another candidate")


def test_labels_are_frozen_at_the_discovery_cutoff():
    prereg, corpus, runs = _files()
    _label(_run(runs, "page-count-partial-page"), "R1-F1")["labeled_at"] = (
        "2026-11-24T18:00:01+00:00"
    )
    _refused(prereg, corpus, runs, "after the discovery cutoff")
    prereg, corpus, runs = _files()
    _label(_run(runs, "page-count-partial-page"), "R1-F1")["labeled_at"] = (
        "2026-11-24T00:30:00+00:00"
    )
    _refused(prereg, corpus, runs, "before it was discovered")


def test_a_confirmed_label_names_a_defect_its_candidate_holds():
    prereg, corpus, runs = _files()
    _label(_run(runs, "retry-attempt-bound"), "R1-F1")["defect"] = "C2"
    _refused(prereg, corpus, runs, "whose commit does not hold it")
    prereg, corpus, runs = _files()
    del _label(_run(runs, "retry-attempt-bound"), "R1-F1")["defect"]
    _refused(prereg, corpus, runs, "defect must be a non-empty string")
    prereg, corpus, runs = _files()
    lab = _label(_run(runs, "redact-authorization-header"), "R1-F2")
    lab["defect"] = "B1"
    _refused(prereg, corpus, runs, "is false and names a defect")


def test_a_clean_negative_holds_no_defect():
    prereg, corpus, runs = _files()
    case = _case(corpus, "rename-slug-pattern")
    case["defects"] = copy.deepcopy(_case(corpus, "retry-attempt-bound")["defects"])
    case["defects"][0]["evidence"] = {"kind": "adjudication", "rationale": "Planted."}
    case["defects"][0]["fixed_at"] = []
    _refused(prereg, corpus, runs, "clean negative with defects")


def test_a_defect_is_one_the_change_introduced():
    prereg, corpus, runs = _files()
    _case(corpus, "page-count-partial-page")["defects"][0]["introduced_at"] = "base"
    _refused(prereg, corpus, runs, "no commit it may be introduced at")


def test_a_harmful_fix_is_exactly_one_that_introduced_a_defect():
    prereg, corpus, runs = _files()
    _run(runs, "retry-attempt-bound")["fixes"][0]["label"]["verdict"] = "not_harmful"
    _refused(prereg, corpus, runs, "so it is harmful, not not_harmful")
    prereg, corpus, runs = _files()
    _run(runs, "retry-attempt-bound")["fixes"][0]["label"]["verdict"] = "unknown"
    _refused(prereg, corpus, runs, "so it is harmful, not unknown")
    prereg, corpus, runs = _files()
    _run(runs, "page-count-partial-page")["fixes"][0]["label"]["verdict"] = "harmful"
    _refused(prereg, corpus, runs, "introduces no registered defect")


def test_a_review_result_the_controller_would_refuse_is_refused():
    prereg, corpus, runs = _files()
    result = _run(runs, "rename-slug-pattern")["reviews"][0]["result"]
    result["needs_fix_round"] = True
    _refused(prereg, corpus, runs, "no REVIEW result the controller accepts")
    prereg, corpus, runs = _files()
    result = _run(runs, "page-count-partial-page")["reviews"][0]["result"]
    result["reviewed_head_sha"] = "b" * 40
    _refused(prereg, corpus, runs, "reviewed another HEAD")
    prereg, corpus, runs = _files()
    run = _run(runs, "page-count-partial-page")
    run["reviews"][1]["commit"] = "candidate"
    run["reviews"][1]["result"]["reviewed_head_sha"] = run["reviews"][0]["result"][
        "reviewed_head_sha"
    ]
    _refused(prereg, corpus, runs, "the run's line says 'fix-1'")


def test_a_run_that_ended_on_findings_says_why_and_a_clean_one_does_not():
    prereg, corpus, runs = _files()
    del _run(runs, "type-hint-cache-key")["stopped"]
    _refused(prereg, corpus, runs, "stopped must be a non-empty string")
    prereg, corpus, runs = _files()
    _run(runs, "rename-slug-pattern")["stopped"] = "BLOCKED"
    _refused(prereg, corpus, runs, "says it was stopped")


def test_a_fixture_run_may_not_say_it_measured_anything():
    prereg, corpus, runs = _files()
    _run(runs, "rename-slug-pattern")["launches"][0]["provenance"] = "measured"
    _refused(prereg, corpus, runs, "may not say it measured")


def test_a_planned_run_is_not_repeated():
    prereg, corpus, runs = _files()
    again = copy.deepcopy(_run(runs, "rename-slug-pattern"))
    again["id"] += "-again"
    runs["runs"].append(again)
    _refused(prereg, corpus, runs, "a planned run is not")


def test_an_unregistered_field_is_refused_wherever_it_appears():
    prereg, corpus, runs = _files()
    _case(corpus, "rename-slug-pattern")["reviewer_notes"] = "x"
    _refused(prereg, corpus, runs, "unregistered field")
    prereg, corpus, runs = _files()
    _run(runs, "retry-attempt-bound")["fixes"][0]["label"]["confidence"] = 1
    _refused(prereg, corpus, runs, "unregistered field")


# -- the pre-registered decision ----------------------------------------------------------


def _real(runs: list[dict], arm: str) -> list[dict]:
    out = copy.deepcopy(runs)
    for run in out:
        run["arm"] = arm
        run["id"] = f"{arm}/{run['case']}"
        run["provenance"] = "real"
        for launch in run["launches"]:
            launch["provenance"] = "measured"
    return out


def _resolve_unknowns(runs: list[dict]) -> list[dict]:
    """The same runs after an adjudicator settled every unknown label as false."""
    for run in runs:
        for lab in run["labels"]:
            if lab["verdict"] == "unknown":
                lab["verdict"] = "false"
    return runs


def _catching(runs: list[dict]) -> list[dict]:
    """A treatment whose review also catches B1 at round 1 and C2 at round 2."""
    out = copy.deepcopy(runs)
    for run in out:
        if run["case"].startswith("redact-authorization-header"):
            review = run["reviews"][0]
            review["result"]["findings"].append(
                {
                    "id": "R1-F4",
                    "classification": "blocked",
                    "title": "An Authorization header as HTTP writes it keeps its token",
                    "location": "redact.py:_HEADER",
                    "required_resolution": "Match the header in any case and mask the "
                    "credential after a scheme word.",
                }
            )
            run["labels"].append(
                {
                    "finding": "R1-F4",
                    "round": 1,
                    "candidate_sha": review["result"]["reviewed_head_sha"],
                    "verdict": "confirmed",
                    "defect": "B1",
                    "adjudicator": AUTHOR,
                    "labeled_at": LABELED,
                }
            )
            run["fixes"][0]["resolutions"]["R1-F4"] = "fixed"
            run["commits"][0]["fixes"] = ["B1", "B2"]
        if run["case"].startswith("retry-attempt-bound"):
            second = run["reviews"][1]
            second["result"]["needs_fix_round"] = True
            second["result"]["findings"] = [
                {
                    "id": "R2-F1",
                    "classification": "blocked",
                    "title": "The last transient error is swallowed",
                    "location": "retry.py:retry",
                    "required_resolution": "Raise the last error when every attempt failed.",
                }
            ]
            run["labels"].append(
                {
                    "finding": "R2-F1",
                    "round": 2,
                    "candidate_sha": second["result"]["reviewed_head_sha"],
                    "verdict": "confirmed",
                    "defect": "C2",
                    "adjudicator": AUTHOR,
                    "labeled_at": LABELED,
                }
            )
            run["commits"].append(
                {
                    "name": "fix-2",
                    "parent": "fix-1",
                    "sha": "2" * 40,
                    "fixes": ["C2"],
                    "files": {"retry.py": "# raises the last error again\n"},
                }
            )
            run["fixes"].append(
                {
                    "round": 2,
                    "commit": "fix-2",
                    "resolutions": {"R2-F1": "fixed"},
                    "label": {
                        "verdict": "not_harmful",
                        "reason": "It restores the raise and nothing else.",
                        "adjudicator": AUTHOR,
                        "labeled_at": LABELED,
                    },
                }
            )
            third = copy.deepcopy(run["reviews"][0])
            third.update(round=3, commit="fix-2")
            third["result"].update(
                round=3, reviewed_head_sha="2" * 40, needs_fix_round=False, findings=[]
            )
            run["reviews"].append(third)
            run["launches"] += [
                {**run["launches"][2], "round": 2},
                {**run["launches"][3], "round": 3},
            ]
    return out


def _replicated(corpus: dict, runs: list[dict], defect_copies: int, clean_copies: int):
    """The corpus and runs, each case copied under new ids: a sample of the registered size."""
    cases, copies = [], []
    for case in corpus["cases"]:
        n = defect_copies if case["kind"] == "defect" else clean_copies
        for k in range(n):
            cases.append({**copy.deepcopy(case), "id": f"{case['id']}~{k}"})
            for run in runs:
                if run["case"] == case["id"]:
                    copies.append(
                        {
                            **copy.deepcopy(run),
                            "case": f"{case['id']}~{k}",
                            "id": f"{run['id']}~{k}",
                        }
                    )
    return {**corpus, "cases": cases}, copies


def _decide(baseline: list[dict], treatment: list[dict], corpus: dict | None = None, prereg=None):
    p, c, r = _files()
    runs = {"schema": r["schema"], "runs": baseline + treatment}
    return rv.decide(rv.check(prereg or p, corpus or c, runs), "baseline", "treatment")


def _sample(defect_copies: int = 7, clean_copies: int = 4):
    """A sample the registered minimum allows: 21 defect cases and 12 clean negatives."""
    _, corpus, runs = _files()
    return _replicated(corpus, _resolve_unknowns(runs["runs"]), defect_copies, clean_copies)


def test_fixture_runs_reach_no_decision_however_good_they_look():
    ev = rv.load()
    decision = rv.decide(ev, ARM, ARM)
    assert decision["decision"] == rv.NOT_EVALUATED
    assert rv.NOT_EVIDENCE in decision["reasons"][0]
    assert rv.decide(ev, ARM, "absent")["decision"] == rv.NOT_EVALUATED


def test_go_needs_the_primary_effect_every_bound_and_the_registered_sample():
    corpus, runs = _sample()
    decision = _decide(_real(runs, "baseline"), _real(_catching(runs), "treatment"), corpus)
    assert decision == {
        "decision": rv.GO,
        "reasons": ["weighted_recall rose by +0.1 or more and every bound held"],
    }


def test_a_new_critical_escape_is_no_go_however_large_the_benefit():
    """The treatment is the better reviewer everywhere but B, where the baseline caught
    B1 and the treatment let it escape."""
    corpus, runs = _sample()
    better = _catching(runs)
    worse = [
        copy.deepcopy(b) if b["case"].startswith("redact") else copy.deepcopy(c)
        for b, c in zip(runs, better, strict=True)
    ]
    decision = _decide(_real(better, "baseline"), _real(worse, "treatment"), corpus)
    assert decision["decision"] == rv.NO_GO
    assert all("critical defect B1" in reason for reason in decision["reasons"])


def test_a_margin_worse_on_its_whole_interval_is_no_go():
    """Every clean negative gains a false finding: the false-positive rate rises past its
    margin whatever the treatment gains elsewhere."""
    corpus, runs = _sample()
    treatment = _catching(runs)
    for run in treatment:
        if run["case"].startswith("rename-slug-pattern"):
            review = run["reviews"][0]
            review["result"]["needs_fix_round"] = True
            review["result"]["findings"] = [
                {
                    "id": "R1-F1",
                    "classification": "nit",
                    "title": "Rename the pattern back",
                    "location": "slug.py",
                    "required_resolution": "Keep the old name.",
                }
            ]
            run["labels"] = [
                {
                    "finding": "R1-F1",
                    "round": 1,
                    "candidate_sha": review["result"]["reviewed_head_sha"],
                    "verdict": "false",
                    "reason": "A private name is the change's to choose.",
                    "adjudicator": AUTHOR,
                    "labeled_at": LABELED,
                }
            ]
            run["fixes"] = [
                {"round": 1, "commit": None, "resolutions": {"R1-F1": "no_change_with_rationale"}}
            ]
            second = copy.deepcopy(review)
            second.update(round=2)
            second["result"].update(round=2, needs_fix_round=False, findings=[])
            run["reviews"].append(second)
    decision = _decide(_real(runs, "baseline"), _real(treatment, "treatment"), corpus)
    assert decision["decision"] == rv.NO_GO
    assert decision["reasons"] == [
        "clean_negative_fpr's whole interval is worse than its margin 0.05"
    ]


def test_no_effect_is_no_go_on_a_complete_sample():
    corpus, runs = _sample()
    decision = _decide(_real(runs, "baseline"), _real(runs, "treatment"), corpus)
    assert decision == {
        "decision": rv.NO_GO,
        "reasons": ["weighted_recall's interval lies wholly below +0.1"],
    }


def test_unknown_labels_rule_out_go():
    _, corpus, raw = _files()
    corpus, runs = _replicated(corpus, raw["runs"], 7, 4)  # the unknown labels left as they are
    decision = _decide(_real(runs, "baseline"), _real(_catching(runs), "treatment"), corpus)
    assert decision["decision"] == rv.INCONCLUSIVE
    assert "weighted_recall is unknown on an arm" in decision["reasons"]
    assert "precision is unknown on an arm" in decision["reasons"]


def test_a_sample_below_the_registered_minimum_reaches_no_primary_conclusion():
    """Six cases decide nothing, not even that the treatment changed nothing."""
    corpus, runs = _sample(1, 1)
    decision = _decide(_real(runs, "baseline"), _real(runs, "treatment"), corpus)
    assert decision["decision"] == rv.INCONCLUSIVE
    assert any("below the registered 20 and 10" in reason for reason in decision["reasons"])


def test_a_case_with_no_pair_or_too_many_exclusions_is_inconclusive_but_harm_still_decides():
    corpus, runs = _sample()
    treatment = _real(_catching(runs), "treatment")
    lost = treatment[1:]
    decision = _decide(_real(runs, "baseline"), lost, corpus)
    assert decision["decision"] == rv.INCONCLUSIVE
    assert "have no pair of runs" in decision["reasons"][0]
    excluded = copy.deepcopy(treatment)
    for run in excluded[:4]:
        run["excluded"] = "the provider was unavailable"
    decision = _decide(_real(runs, "baseline"), excluded, corpus)
    assert decision["decision"] == rv.INCONCLUSIVE
    assert "4 excluded case(s) pass the registered share" in decision["reasons"]
    # Harm decides on whatever pairs there are.
    worse = _real(runs, "treatment")[1:]
    decision = _decide(_real(_catching(runs), "baseline"), worse, corpus)
    assert decision["decision"] == rv.NO_GO


def test_a_cost_no_launch_measured_rules_out_go():
    corpus, runs = _sample()
    treatment = _real(_catching(runs), "treatment")
    treatment[0]["launches"][0]["provenance"] = "estimated"
    decision = _decide(_real(runs, "baseline"), treatment, corpus)
    assert decision == {
        "decision": rv.INCONCLUSIVE,
        "reasons": ["cost_per_validated_success is not measured on both arms"],
    }


# -- the baseline the protocol captured ---------------------------------------------------


def test_the_baseline_facts_are_the_codes():
    """What section 3 records of today's review is what the code does: a change to any
    of it moves this test, and the protocol's baseline with it (its amendments)."""
    base = rv.read_json(rv.PREREGISTRATION)["baseline"]
    assert {r: review_profile_name(int(r)) for r in base["review_profiles"]} == base[
        "review_profiles"
    ]
    assert tuple(base["finding_classifications"]) == FINDING_CLASSIFICATIONS
    assert tuple(base["fix_resolutions"]) == FIX_RESOLUTIONS
    assert tuple(base["review_results"]) == RESULTS
    assert base["max_findings_per_review"] == MAX_FINDINGS_PER_REVIEW
    finding = {
        "id": "R1-F1",
        "classification": "nit",
        "required_resolution": "x",
        "title": "t",
        "location": "l",
    }
    kept = review_record(1, "a" * 40, "needs_fix", [finding])["findings"][0]
    assert sorted(kept) == base["persisted_finding_fields"]
    cfg = default_config()
    assert base["workflow"] == {key: getattr(cfg.workflow, key) for key in base["workflow"]}
    assert base["replan"] == {key: getattr(cfg.review.replan, key) for key in base["replan"]}
    from autoforge.certificate import CLEAN

    assert base["certificate_clean"] == CLEAN


def test_every_fixture_review_is_one_the_current_routing_and_parser_accept():
    ev = rv.load()
    for run in ev.runs:
        assert [r.round for r in run.reviews] == list(range(1, len(run.reviews) + 1))
        for review in run.reviews:
            for finding in review.findings:
                assert finding.classification in FINDING_CLASSIFICATIONS


# -- the protocol -------------------------------------------------------------------------


def _protocol() -> str:
    return PROTOCOL.read_text(encoding="utf-8")


def test_the_protocol_registers_the_digests_of_its_files():
    text = _protocol()
    for path in (rv.PREREGISTRATION, rv.CORPUS):
        assert f"`{path.name}`: `{rv.file_digest(path)}`" in text, path.name


def test_the_protocol_names_every_case_metric_and_registered_value():
    text = _protocol()
    prereg = rv.read_json(rv.PREREGISTRATION)
    for case in rv.read_json(rv.CORPUS)["cases"]:
        assert f"`{case['id']}`" in text, case["id"]
    for metric in (*rv.RATE_METRICS, "escaped_critical_defects", "cost_per_validated_success"):
        assert f"`{metric}`" in text, metric
    for verdict in (*prereg["label_verdicts"], *prereg["fix_verdicts"]):
        assert f"`{verdict}`" in text, verdict
    for severity, weight in prereg["severity_weights"].items():
        assert f"`{severity}` | {weight}" in text, severity
    for cls in prereg["defect_classes"]:
        assert f"`{cls}`" in text, cls
    u = prereg["uncertainty"]
    assert f"{u['resamples']:,} resamples" in text and f"seed {u['seed']}" in text
    d = prereg["decision"]
    assert f"`{d['primary']['metric']}` | at least +{d['primary']['min_difference']}" in text
    for metric, margin in d["margins"].items():
        assert re.search(rf"`{metric}` \| [^|]*{margin['margin']}", text), metric
    minimum = prereg["minimum_sample"]
    assert f"{minimum['defect_cases']} defect cases and {minimum['clean_negative_cases']}" in text


def test_the_protocol_states_the_fixture_scores():
    text = _protocol()
    m = rv.report(rv.load())["arms"][ARM]["metrics"]
    for metric in rv.RATE_METRICS:
        assert f"`{metric}` | {m[metric]['value']}" in text, metric


def test_every_test_the_protocol_names_exists():
    names = set(re.findall(r"test_review_evaluation\.py::(test_\w+)", _protocol()))
    assert names
    defined = {
        node.name
        for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
    }
    assert names <= defined, names - defined


def test_the_protocol_is_linked_and_its_links_resolve():
    text = _protocol()
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text):
        if "://" not in target:
            assert (PROTOCOL.parent / target).exists(), target
    index = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "(review-evaluation-protocol.md)" in index


def test_no_workflow_or_package_module_runs_this_harness():
    workflows = REPO_ROOT / ".github" / "workflows"
    for path in workflows.glob("*.y*ml"):
        assert "review_evaluation" not in path.read_text(encoding="utf-8"), path.name
    for path in (REPO_ROOT / "src" / "autoforge").rglob("*.py"):
        assert "review_evaluation" not in path.read_text(encoding="utf-8"), path


def test_the_command_line_checks_scores_and_refuses(tmp_path, capsys):
    assert rv.main(["check"]) == 0
    assert rv.main(["score", "--out", str(tmp_path / "report.json")]) == 0
    assert (tmp_path / "report.json").read_text(encoding="utf-8") == rv.FIXTURE_REPORT.read_text(
        encoding="utf-8"
    )
    capsys.readouterr()
    assert rv.main(["decide", "--baseline", ARM, "--treatment", ARM]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == rv.NOT_EVALUATED
    bad = tmp_path / "runs.json"
    raw = rv.read_json(rv.FIXTURE_RUNS)
    raw["runs"][0]["labels"][0]["confidence"] = 0.9
    bad.write_text(json.dumps(raw), encoding="utf-8")
    assert rv.main(["--runs", str(bad), "check"]) == 2
    assert "never a label" in capsys.readouterr().err
