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
import dataclasses
import json
import re
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
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
LABELED_AT = datetime.fromisoformat(LABELED)
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
    assert rv.verify(ev, tmp_path, oracles=True) == []
    expectations = [e for run in ev.runs for e in rv.oracle_expectations(run)]
    assert ("B1", "fix-1", "fail") in expectations  # the escaped critical, at the clean HEAD
    assert ("C2", "fix-1", "fail") in expectations  # the harmful fix's regression
    assert ("C2", "candidate", "pass") in expectations
    assert {outcome for _, _, outcome in expectations} == {"pass", "fail"}


def test_a_commit_that_does_not_build_to_its_pin_is_reported(tmp_path):
    prereg, corpus, runs = _files()
    _case(corpus, "rename-slug-pattern")["commits"][1]["files"]["slug.py"] += "\n"
    problems = rv.verify(rv.check(prereg, corpus, runs), tmp_path, oracles=True)
    assert len(problems) == 1
    assert "'candidate' is pinned at" in problems[0] and "builds as" in problems[0]


def test_an_oracle_that_does_not_show_its_defect_is_reported(tmp_path):
    """A candidate that no longer holds A1 makes its oracle pass where the ground truth
    says it fails: the labels would rest on a defect nothing shows."""
    prereg, corpus, runs = _files()
    candidate = _case(corpus, "page-count-partial-page")["commits"][1]["files"]
    candidate["pages.py"] = candidate["pages.py"].replace("total // size", "-(-total // size)")
    problems = rv.verify(rv.check(prereg, corpus, runs), tmp_path, oracles=True)
    assert any("A1's oracle should fail at 'candidate' and gave pass" in p for p in problems)


def test_an_oracle_that_crashes_shows_no_defect(tmp_path):
    """Only an AssertionError is a failure: an import error is no evidence of a defect."""
    prereg, corpus, runs = _files()
    oracles = _case(corpus, "retry-attempt-bound")["oracles"]
    oracles["attempt_count"] = "from retry import no_such_name\n"
    problems = rv.verify(rv.check(prereg, corpus, runs), tmp_path, oracles=True)
    assert any("gave error: ImportError" in p for p in problems)


def test_an_oracle_runs_isolated_from_the_hosts_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "F" * 36)
    source = (
        "import os\nassert 'GH_TOKEN' not in os.environ\nassert not os.environ.get('PYTHONPATH')\n"
    )
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "x.py").write_text("", encoding="utf-8")
    assert rv.run_oracle(source, tmp_path / "tree", tmp_path) == "pass"


def test_verify_replays_every_case_whether_or_not_a_run_holds_it(tmp_path):
    """The corpus is replayed case by case: a case no run holds, and a list of no runs at
    all, still have every pin built and every oracle run at the case's commits."""
    prereg, corpus, runs = _files()
    none = {"schema": runs["schema"], "runs": []}
    (tmp_path / "intact").mkdir()
    assert rv.verify(rv.check(prereg, corpus, none), tmp_path / "intact", oracles=True) == []

    _case(corpus, "rename-slug-pattern")["commits"][1]["files"]["slug.py"] += "\n"
    candidate = _case(corpus, "page-count-partial-page")["commits"][1]["files"]
    candidate["pages.py"] = candidate["pages.py"].replace("total // size", "-(-total // size)")
    (tmp_path / "no-runs").mkdir()
    problems = rv.verify(rv.check(prereg, corpus, none), tmp_path / "no-runs", oracles=True)
    assert any(
        p.startswith("case 'rename-slug-pattern': commit 'candidate' is pinned at")
        for p in problems
    )
    assert any(
        p == "case 'page-count-partial-page': A1's oracle should fail at 'candidate' and gave pass"
        for p in problems
    )

    prereg, corpus, runs = _files()
    runs["runs"] = [r for r in runs["runs"] if r["case"] != "retry-attempt-bound"]
    _case(corpus, "retry-attempt-bound")["oracles"]["attempt_count"] = (
        "from retry import no_such_name\n"
    )
    (tmp_path / "unrepresented").mkdir()
    problems = rv.verify(rv.check(prereg, corpus, runs), tmp_path / "unrepresented", oracles=True)
    assert problems
    assert all(
        p.startswith("case 'retry-attempt-bound': C1's oracle") and "gave error: ImportError" in p
        for p in problems
    )


def test_verify_runs_the_oracles_of_trusted_inputs_alone(tmp_path, capsys):
    """An oracle is code that runs as the caller, with the caller's files and network:
    ``python -I`` keeps the host's variables out and confines nothing. ``verify`` runs the
    oracles of the registered files, and another file's only when the caller trusts it;
    ``--pins-only`` replays any file's pins and runs nothing it holds."""
    assert rv.registered_inputs(rv.load())
    _, corpus, runs = _files()
    marker = tmp_path / "an-oracle-ran"
    corpus["cases"] = [_case(corpus, "retry-attempt-bound")]
    oracles = corpus["cases"][0]["oracles"]
    oracles["attempt_count"] = f"open({str(marker)!r}, 'a').close()\n" + oracles["attempt_count"]
    runs["runs"] = [_run(runs, "retry-attempt-bound")]
    custom = ["--corpus", str(tmp_path / "corpus.json"), "--runs", str(tmp_path / "runs.json")]
    (tmp_path / "corpus.json").write_text(json.dumps(corpus), encoding="utf-8")
    (tmp_path / "runs.json").write_text(json.dumps(runs), encoding="utf-8")
    assert not rv.registered_inputs(rv.load(rv.PREREGISTRATION, *map(Path, custom[1::2])))

    assert rv.main([*custom, "verify"]) == 2
    assert "--trust-oracles" in capsys.readouterr().err
    assert not marker.exists()
    assert rv.main([*custom, "verify", "--pins-only"]) == 0
    assert "the pins alone, no oracle" in capsys.readouterr().out
    assert not marker.exists()
    assert rv.main([*custom, "verify", "--trust-oracles"]) == 0
    assert "pins and oracles" in capsys.readouterr().out
    assert marker.exists()


def test_two_inputs_that_build_one_commit_give_its_oracles_one_tree(tmp_path):
    """An oracle reads the tree of the commit it runs at, as the controller's byte-exact
    materializer writes and proves it, never the files that built the commit. Here a
    file the case's base ignores is added to its candidate: git leaves it out, so both
    inputs build the very same commits, and an oracle that looks for the file finds it in
    neither. The input whose files are not its commits' trees is reported."""
    prereg, corpus, runs = _files()
    case = _case(corpus, "retry-attempt-bound")
    corpus["cases"] = [case]
    none = {"schema": runs["schema"], "runs": []}
    case["commits"][0]["files"][".gitignore"] = "notes.txt\n"
    ev = rv.check(prereg, corpus, none)
    (tmp_path / "pin").mkdir()
    pinned = rv.build(ev.cases[case["id"]].commits, ev.commit_date, case["id"], tmp_path / "pin")
    for commit in case["commits"]:
        commit["sha"] = pinned.shas[commit["name"]]
    noted = copy.deepcopy(corpus)
    _case(noted, case["id"])["commits"][1]["files"]["notes.txt"] = "Not in any commit.\n"

    looks = "import os\nassert not os.path.exists('notes.txt'), 'the oracle saw notes.txt'\n"
    trees = []
    for name, files in (("as-pinned", corpus), ("noted", noted)):
        root = tmp_path / name
        root.mkdir()
        checked = rv.check(prereg, files, none)
        built = rv.build(checked.cases[case["id"]].commits, checked.commit_date, case["id"], root)
        assert built.shas == pinned.shas
        assert rv.oracle_at(looks, built, "candidate", root) == "pass"
        listing = rv._tree_blobs(built.repo, checked.commit_date, built.shas["candidate"])
        trees.append(listing)
        (root / "verify").mkdir()
        problems = rv.verify(checked, root / "verify", oracles=True)
        if name == "as-pinned":
            assert problems == []
        else:
            assert problems == [
                f"case {case['id']!r}: commit {commit!r}'s tree is not the files that built "
                "it, at ['notes.txt']: git left out or rewrote what the files hold"
                for commit in ("candidate", "reference")
            ]
    assert trees[0] == trees[1] and "notes.txt" not in trees[0]
    # A file's bytes are held too: an attribute that makes git rewrite them on commit
    # leaves a tree that is not the files.
    rewritten = copy.deepcopy(corpus)
    base = _case(rewritten, case["id"])["commits"][0]["files"]
    base[".gitattributes"] = "*.txt ident\n"
    base["id.txt"] = "$Id: not what git commits $\n"
    checked = rv.check(prereg, rewritten, none)
    (tmp_path / "ident").mkdir()
    built = rv.build(
        checked.cases[case["id"]].commits, checked.commit_date, "ident", tmp_path / "ident"
    )
    assert built.unfaithful == {name: ("id.txt",) for name in ("base", "candidate", "reference")}


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
    # A1 confirmed twice in one round: 1 duplicate of 5 confirmed. B's unknown finding sits
    # beside B2 and could be a second report of it (2 of 6), or the first of B1 (1 of 6);
    # type-hint's are on a clean negative, where no label can confirm anything.
    assert m["duplicate_ratio"]["value"] == 0.2
    assert m["duplicate_ratio"]["label_bounds"] == [0.166667, 0.333333]
    for metric in ("weighted_recall", "precision", "clean_negative_fpr", "duplicate_ratio"):
        assert m[metric]["status"] == "unknown", metric
    assert m["harmful_fix_rate"]["status"] == "known"
    assert m["escaped_critical_defects"] == {
        "value": 1,
        "defects": [{"case": "redact-authorization-header", "defect": "B1", "belongs_to": "case"}],
        "possible": [],
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
    assert rows["retry-attempt-bound"]["escaped"] == [
        {"defect": "C2", "severity": "major", "belongs_to": "run"}
    ]
    assert all(row["missing_launches"] == [] for row in rows.values())
    assert rows["type-hint-cache-key"]["ended"] == "stopped"
    assert arm["loops"]["ended"] == {"certified": 5, "stopped": 1}
    assert arm["loops"]["rounds_by_profile"] == {"review_round_1": 6, "review_round_2_5": 5}


def test_every_interval_is_reproducible_from_its_registered_seed():
    prereg = rv.read_json(rv.PREREGISTRATION)
    assert rv.bootstrap_indices(6, prereg) == rv.bootstrap_indices(6, prereg)
    other = copy.deepcopy(prereg)
    other["uncertainty"]["seed"] += 1
    assert rv.bootstrap_indices(6, other) != rv.bootstrap_indices(6, prereg)
    assert len(rv.bootstrap_indices(6, prereg)) == prereg["uncertainty"]["resamples"]


def test_an_interval_ends_at_nearest_rank_order_statistics():
    """Each end is the value of one-based rank ``ceil(p * n)``: ranks 1 and 39 of 40 at
    95%, not 40, which one more rank would give."""
    ranked = [float(v) for v in range(1, 41)]
    assert rv.percentile_interval(ranked, 0.95) == (1.0, 39.0)
    assert rv.percentile_interval(ranked[::-1], 0.95) == (1.0, 39.0)
    assert rv.percentile_interval([*ranked, 41.0], 0.95) == (2.0, 40.0)
    assert rv.percentile_interval([float(v) for v in range(1, 21)], 0.9) == (1.0, 19.0)
    assert rv.percentile_interval([float(v) for v in range(1, 10001)], 0.95) == (250.0, 9750.0)
    assert rv.percentile_interval([3.0], 0.95) == (3.0, 3.0)
    assert rv.percentile_interval([], 0.95) is None


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


def _more_unknowns(run: dict, round: int, count: int) -> None:
    """``count`` more findings in ``round`` of ``run``, each a copy of its first and
    labeled ``unknown``."""
    review = run["reviews"][round - 1]
    findings = review["result"]["findings"]
    template = copy.deepcopy(findings[0])
    for _ in range(count):
        fid = f"R{round}-F{len(findings) + 1}"
        findings.append({**copy.deepcopy(template), "id": fid})
        run["labels"].append(
            {
                "finding": fid,
                "round": round,
                "candidate_sha": review["result"]["reviewed_head_sha"],
                "verdict": "unknown",
                "adjudicator": AUTHOR,
                "labeled_at": LABELED,
                "reason": "Not settled.",
            }
        )
        for fix in run["fixes"]:
            if fix["round"] == round:
                fix["resolutions"][fid] = "no_change_with_rationale"


def test_unknown_findings_that_could_be_duplicates_bound_the_duplicate_ratio():
    """A duplicate is a later confirmed finding of a round naming a defect an earlier one
    of that round names. An unknown finding could be one, or a first report of another
    defect, but only as the check would accept its label: confirmed of a registered defect
    at its round's commit. The fixture's band is 1 of 6 (B's unknown finding the first
    report of B1) to 2 of 6 (a second report of B2)."""
    prereg, corpus, runs = _files()
    assert _metrics(prereg, corpus, runs)["duplicate_ratio"]["label_bounds"] == [
        0.166667,
        0.333333,
    ]
    # A clean-negative round holds no registered defect: two unknown findings there can
    # be neither duplicates nor first reports, and leave the band where it was.
    _more_unknowns(_run(runs, "type-hint-cache-key"), 1, 1)
    m = _metrics(prereg, corpus, runs)["duplicate_ratio"]
    assert m["value"] == 0.2  # 1 of 5: no unknown label counts in the point estimate
    assert m["label_bounds"] == [0.166667, 0.333333]
    # Three unknown findings in a round whose only unnamed registered defect is B1: one of
    # them at most is a first report (1 of 6), all three could restate B2 (4 of 8).
    _more_unknowns(_run(runs, "redact-authorization-header"), 1, 2)
    m = _metrics(prereg, corpus, runs)["duplicate_ratio"]
    assert m["label_bounds"] == [0.166667, 0.5]
    assert m["status"] == "unknown"
    # Settling every unknown label removes the band.
    m = _metrics(prereg, corpus, {**runs, "runs": _resolve_unknowns(runs["runs"])})
    assert m["duplicate_ratio"]["label_bounds"] == [0.2, 0.2]
    assert m["duplicate_ratio"]["status"] == "known"


def test_unknown_findings_bound_recall_one_registered_defect_each():
    """An unknown finding could confirm one defect, a registered one present at its
    round's commit: recall's upper bound is the heaviest set of undetected defects the
    unknown findings could name, one each."""
    prereg, corpus, runs = _files()
    lab = _label(_run(runs, "redact-authorization-header"), "R1-F1")
    lab.update(verdict="false", reason="Settled false for this test.")
    del lab["defect"]
    m = _metrics(prereg, corpus, runs)["weighted_recall"]
    # A1, A2 and C1 detected (10 of 24). B's one unknown finding could name B1 (8) or B2
    # (2), not both: 18 of 24, not 20.
    assert m["value"] == round(10 / 24, 6)
    assert m["label_bounds"] == [round(10 / 24, 6), 0.75]
    _more_unknowns(_run(runs, "redact-authorization-header"), 1, 1)
    assert _metrics(prereg, corpus, runs)["weighted_recall"]["label_bounds"] == [
        round(10 / 24, 6),
        round(20 / 24, 6),
    ]
    # Across rounds, a finding that could name either of two defects leaves the one only
    # it could name to the other round's finding.
    d1 = rv.Defect(
        "D1", "critical", 8, "correctness", "c", (), LABELED_AT, "seeded", True, None, ()
    )
    d2 = rv.Defect("D2", "major", 4, "correctness", "c", (), LABELED_AT, "seeded", True, None, ())
    rounds = [(1, frozenset({"D1", "D2"})), (1, frozenset({"D1"}))]
    assert rv._nameable_weight(rounds, [d1, d2]) == 12
    assert rv._nameable_weight([(1, frozenset({"D1", "D2"}))], [d1, d2]) == 8
    assert rv._nameable_weight([(2, frozenset())], [d1, d2]) == 0


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


# Where an adjudicator appears: a finding's label, a defect and a fix's label.
_ADJUDICATORS = {
    "a finding's label": lambda corpus, runs: _label(
        _run(runs, "redact-authorization-header"), "R1-F2"
    )["adjudicator"],
    "a defect": lambda corpus, runs: _case(corpus, "retry-attempt-bound")["defects"][0][
        "adjudicator"
    ],
    "a fix's label": lambda corpus, runs: _run(runs, "retry-attempt-bound")["fixes"][0]["label"][
        "adjudicator"
    ],
}


@pytest.mark.parametrize("where", sorted(_ADJUDICATORS))
def test_no_preregistration_makes_an_agent_an_adjudicator(where, monkeypatch):
    """Only a human adjudicates (section 4.3), and the scorer holds that rule itself: a
    preregistration that moves `agent` from the refused roles to the allowed ones, drops
    it from the refused ones or invents another allowed role is refused, and an agent's
    label is refused all the same where the preregistration's own check is bypassed.
    A preregistration may narrow who adjudicates; whether a declared human is one is the
    record's word."""
    prereg, corpus, runs = _files()
    _ADJUDICATORS[where](corpus, runs)["role"] = "agent"
    relaxed = copy.deepcopy(prereg)
    relaxed["refused_adjudicator_roles"].remove("agent")
    relaxed["adjudicator_roles"].append("agent")
    _refused(relaxed, corpus, runs, "only a human adjudicates")
    dropped = copy.deepcopy(prereg)
    dropped["refused_adjudicator_roles"].remove("agent")
    _refused(dropped, corpus, runs, r"leaves out \['agent'\]")
    invented = copy.deepcopy(prereg)
    invented["adjudicator_roles"].append("copilot")
    _refused(invented, corpus, runs, "only a human adjudicates")
    monkeypatch.setattr(rv, "check_preregistration", lambda p: p)
    _refused(relaxed, corpus, runs, "self-reported correctness is refused")
    _ADJUDICATORS[where](corpus, runs)["role"] = "copilot"
    _refused(invented, corpus, runs, "is no human adjudicator's")
    monkeypatch.undo()
    narrowed = copy.deepcopy(prereg)
    narrowed["adjudicator_roles"] = ["fixture_author"]
    assert rv.check(narrowed, *_files()[1:]).runs


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
        "autoforge.loop_guard",
        "autoforge.materialize",
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


def test_a_fix_follows_the_defect_it_fixes():
    """A commit that fixes a defect strictly descends from the commit that introduced it
    and finds it at its parent. Put the retry regression's fix at its parent candidate
    and C2 would be nowhere, its introduction included: every oracle outcome a pass, an
    always-passing oracle verified, the fix still harmful and the regression gone from
    the escapes. Each such record is refused, a case's and a run's alike."""
    # (whose defect: the case's or the run's, the defect's fixed_at), and a run commit
    # that fixes what it introduced.
    for owner, fixed_at in (
        ("run", ["candidate"]),
        ("run", ["fix-1"]),
        ("case", ["base"]),
        ("case", ["candidate"]),
    ):
        prereg, corpus, runs = _files()
        record = (
            _run(runs, "retry-attempt-bound")
            if owner == "run"
            else _case(corpus, "retry-attempt-bound")
        )
        record["defects"][0]["fixed_at"] = fixed_at
        _refused(prereg, corpus, runs, "does not descend from")
    prereg, corpus, runs = _files()
    _run(runs, "retry-attempt-bound")["commits"][0]["fixes"] = ["C1", "C2"]
    _refused(prereg, corpus, runs, "'fix-1' is said to fix C2 and does not descend from 'fix-1'")
    # A second fix of a line its first already fixed finds nothing at its parent.
    prereg, corpus, runs = _files()
    _run(runs, "page-count-partial-page")["commits"].append(
        {"name": "fix-2", "parent": "fix-1", "sha": "2" * 40, "fixes": ["A1"], "files": {"x": ""}}
    )
    _refused(prereg, corpus, runs, "which its parent 'fix-1' does not hold")


def test_an_oracle_expected_to_both_fail_and_pass_is_refused():
    """The failure at a defect's introduction is what shows the defect at all, so an
    expectation that contradicts it is refused, never settled as a pass."""
    holds = "which holds it, so it would both fail and pass there"
    prereg, corpus, runs = _files()
    _run(runs, "retry-attempt-bound")["defects"][0]["evidence"]["passes_at"].append("fix-1")
    _refused(prereg, corpus, runs, holds)
    prereg, corpus, runs = _files()
    _case(corpus, "page-count-partial-page")["defects"][0]["evidence"]["passes_at"] = ["candidate"]
    _refused(prereg, corpus, runs, holds)
    # The replay holds the same line whatever reached it: a fix at the introduction.
    case = rv.load().cases["retry-attempt-bound"]
    (c1,) = case.defects.values()
    at_introduction = dataclasses.replace(c1, fixed_at=("candidate",))
    with pytest.raises(rv.ReviewEvaluationError, match="both fail and pass at 'candidate'"):
        rv._expectations("case", [at_introduction], case.commits, ("candidate",))


# A defect of rename-slug-pattern's candidate, discovered only after the cutoff.
_LATE = {
    "id": "E1",
    "defect_class": "correctness",
    "severity": "critical",
    "location": "slug.py",
    "introduced_at": "candidate",
    "fixed_at": [],
    "discovered_at": "2026-11-25T09:00:00+00:00",
    "discovered_by": "post_merge",
    "summary": "Found after the cutoff.",
    "evidence": {"kind": "adjudication", "rationale": "Reported after the cutoff."},
    "adjudicator": AUTHOR,
}


def test_a_defect_case_holds_a_registered_defect_at_its_candidate():
    """A defect case counts toward the minimum sample because a review of its candidate has
    a registered defect to find. One whose only defect was discovered after the cutoff, or
    is confined to another commit, has none: it is refused, never counted."""
    prereg, corpus, runs = _files()
    _case(corpus, "rename-slug-pattern").update(kind="defect", defects=[copy.deepcopy(_LATE)])
    _refused(prereg, corpus, runs, "no registered defect present at its candidate")
    prereg, corpus, runs = _files()
    (c1,) = _case(corpus, "retry-attempt-bound")["defects"]
    c1.update(
        introduced_at="reference",
        fixed_at=[],
        evidence={"kind": "adjudication", "rationale": "Only the reference holds it."},
    )
    _refused(prereg, corpus, runs, "no registered defect present at its candidate")
    # 18 defect cases and 12 clean negatives are short of the minimum. Two clean negatives
    # relabeled defect cases on a late defect add no defect to find, and are refused.
    corpus, runs = _sample(6, 4)
    decision = _decide(_real(runs, "baseline"), _real(_catching(runs), "treatment"), corpus)
    assert decision["decision"] == rv.INCONCLUSIVE
    assert any("pairs 18 defect case(s) and 12 clean" in r for r in decision["reasons"])
    for cid in ("rename-slug-pattern~0", "rename-slug-pattern~1"):
        _case(corpus, cid).update(kind="defect", defects=[copy.deepcopy(_LATE)])
    with pytest.raises(rv.ReviewEvaluationError, match="no registered defect present at its"):
        _decide(_real(runs, "baseline"), _real(_catching(runs), "treatment"), corpus)


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
    sha = run["reviews"][0]["result"]["reviewed_head_sha"]
    run["reviews"][1]["result"]["reviewed_head_sha"] = sha
    run["reviews"][1]["controller"]["validated_sha"] = sha
    run["reviews"][1]["controller"]["certified_as"]["reviewed_head_sha"] = sha
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


def test_a_launch_is_bound_to_a_round_of_its_run():
    prereg, corpus, runs = _files()
    del _run(runs, "page-count-partial-page")["launches"][1]["round"]
    _refused(prereg, corpus, runs, "is a REVIEW launch and names no round")
    prereg, corpus, runs = _files()
    _run(runs, "rename-slug-pattern")["launches"][1]["round"] = 2
    _refused(prereg, corpus, runs, "a round the run does not have")
    prereg, corpus, runs = _files()
    run = _run(runs, "rename-slug-pattern")
    run["launches"].append({**run["launches"][1], "phase": "FIX"})
    _refused(prereg, corpus, runs, "which needed no fix")
    prereg, corpus, runs = _files()
    _run(runs, "rename-slug-pattern")["launches"][0]["round"] = 1
    _refused(prereg, corpus, runs, "precedes every round")


def test_a_launch_ordinal_is_spent_once_and_within_the_runs_window():
    prereg, corpus, runs = _files()
    _run(runs, "rename-slug-pattern")["launches"][1]["ordinal"] = 1
    _refused(prereg, corpus, runs, "the controller spends each ordinal on one launch")
    prereg, corpus, runs = _files()
    _run(runs, "rename-slug-pattern")["launches"][1]["ordinal"] = 3
    _refused(prereg, corpus, runs, "past the last ordinal 2 the controller spent")
    prereg, corpus, runs = _files()
    del _run(runs, "rename-slug-pattern")["launch_window"]
    _refused(prereg, corpus, runs, "is missing")
    prereg, corpus, runs = _files()
    del _run(runs, "rename-slug-pattern")["launches"][0]["ordinal"]
    _refused(prereg, corpus, runs, "is missing")
    # The state's launch_seq is the run's last ordinal only at a stop the evaluation
    # ended it at; DONE follows a merge, which no evaluation makes; and a start is the
    # run's creation or unrecorded, never an ordinal a record names.
    for change, needle in (
        ({"end": "unrecorded"}, "records no end and names a last ordinal"),
        ({"end": "DONE"}, "end must be one of"),
        ({"start": 2}, "start must be one of"),
        ({"first": 1}, "unregistered field"),
    ):
        prereg, corpus, runs = _files()
        _run(runs, "rename-slug-pattern")["launch_window"].update(change)
        _refused(prereg, corpus, runs, needle)


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


def _del(path: str):
    def edit(prereg: dict) -> None:
        *parents, last = path.split(".")
        node = prereg
        for key in parents:
            node = node[key]
        del node[last]

    return edit


def _set(path: str, value: object):
    def edit(prereg: dict) -> None:
        *parents, last = path.split(".")
        node = prereg
        for key in parents:
            node = node[key]
        node[last] = value

    return edit


# A preregistration short of a decision criterion, or holding one the scorer cannot
# use, and what the check says of it.
_UNUSABLE_CRITERIA = {
    "no cost margin": (_del("decision.margins.cost_per_validated_success"), "is missing"),
    "no precision margin": (_del("decision.margins.precision"), "is missing"),
    "a margin on the primary": (
        _set("decision.margins.weighted_recall", {"direction": "higher_is_better", "margin": 0}),
        "unregistered field",
    ),
    "a margin as text": (_set("decision.margins.precision.margin", "0.05"), "must be a number"),
    "a negative margin": (_set("decision.margins.precision.margin", -0.05), "must be a number"),
    "a margin past 1": (_set("decision.margins.duplicate_ratio.margin", 1.5), "must be a number"),
    "a boolean margin": (
        _set("decision.margins.cost_per_validated_success.margin", True),
        "must be a number",
    ),
    "a turned direction": (
        _set("decision.margins.harmful_fix_rate.direction", "higher_is_better"),
        "must be lower_is_better",
    ),
    "no direction": (_del("decision.margins.clean_negative_fpr.direction"), "is missing"),
    "no primary effect": (_del("decision.primary.min_difference"), "is missing"),
    "a primary effect of 0": (_set("decision.primary.min_difference", 0), "must be a number"),
    "a lower-is-better primary": (
        _set("decision.primary.metric", "duplicate_ratio"),
        "is lower-is-better",
    ),
    "no minimum of clean negatives": (
        _del("minimum_sample.clean_negative_cases"),
        "is missing",
    ),
    "a minimum of 0 defect cases": (_set("minimum_sample.defect_cases", 0), "integer >= 1"),
    "no excluded share": (_del("decision.max_excluded_case_share"), "is missing"),
    "an excluded share past 1": (
        _set("decision.max_excluded_case_share", 2),
        "must be a number",
    ),
    "no label-uncertainty limit": (_del("max_label_uncertainty"), "is missing"),
    "a label-uncertainty limit of null": (
        _set("max_label_uncertainty", None),
        "must be a number",
    ),
    "no severity blocking a success": (
        _set("success_blocking_severities", []),
        "at least one entry",
    ),
    "no critical severity": (
        _set("severity_weights", {"major": 4, "minor": 2}),
        "weighing `critical`",
    ),
    "another fix verdict": (
        _set("fix_verdicts", ["harmful", "harmless", "unknown"]),
        "the scorer reads these names",
    ),
    "another usage provenance": (
        _set("usage_provenance", ["measured", "unknown"]),
        "the scorer reads these names",
    ),
}


@pytest.mark.parametrize("criterion", sorted(_UNUSABLE_CRITERIA))
def test_a_preregistration_short_of_a_usable_criterion_is_refused(criterion):
    """Every criterion the decision reads is required, typed and in range: a file without
    one is refused, never read as the criterion waived. Only an amendment changes a
    criterion, by registering another value."""
    edit, needle = _UNUSABLE_CRITERIA[criterion]
    prereg, corpus, runs = _files()
    edit(prereg)
    _refused(prereg, corpus, runs, needle)


def test_an_omitted_cost_margin_never_turns_an_open_cost_into_go():
    """With a treatment run's launch record short of a launch, the registered margins
    leave the cost open, INCONCLUSIVE. Read without the cost margin, the same arms are a
    GO, which is why the check refuses that preregistration."""
    corpus, runs = _sample()
    treatment = _real(_catching(runs), "treatment")
    (short,) = [r for r in treatment if r["case"] == "page-count-partial-page~0"]
    del short["launches"][2]
    p, _, r = _files()
    ev = rv.check(p, corpus, {"schema": r["schema"], "runs": _real(runs, "baseline") + treatment})
    assert rv.decide(ev, "baseline", "treatment") == {
        "decision": rv.INCONCLUSIVE,
        "reasons": ["cost_per_validated_success is not measured on both arms"],
    }
    waived = copy.deepcopy(p)
    del waived["decision"]["margins"]["cost_per_validated_success"]
    unchecked = dataclasses.replace(ev, preregistration=waived)
    assert rv.decide(unchecked, "baseline", "treatment")["decision"] == rv.GO
    with pytest.raises(rv.ReviewEvaluationError, match="is missing"):
        _decide(_real(runs, "baseline"), treatment, corpus, waived)


# -- the pre-registered decision ----------------------------------------------------------


def _real(runs: list[dict], arm: str) -> list[dict]:
    """The runs as one real arm's, every launch measured and numbered in the order listed,
    and the controller's record of the run's launches ending at the last of them."""
    out = copy.deepcopy(runs)
    for run in out:
        run["arm"] = arm
        run["id"] = f"{arm}/{run['case']}"
        run["provenance"] = "real"
        for ordinal, launch in enumerate(run["launches"], 1):
            launch["provenance"] = "measured"
            launch["ordinal"] = ordinal
        run["launch_window"]["last"] = len(run["launches"])
    return out


def _issued(review: dict) -> None:
    """``review``'s round as one the controller issued its certificate for."""
    review["controller"].update(
        disposition="clean",
        certificate="issued",
        certified_as={
            "review_round": review["round"],
            "reviewed_head_sha": review["result"]["reviewed_head_sha"],
        },
    )


def _none_issued(review: dict, disposition: str = "needs_fix") -> None:
    """``review``'s round as one the controller recorded ``disposition`` and certified
    nothing for."""
    review["controller"].update(
        disposition=disposition, certificate="not_issued", certified_as=None
    )


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
            _none_issued(second)
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
            third["controller"] = {"validation": "passed", "validated_sha": "2" * 40}
            _issued(third)
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


def _critical_regressions(runs: list[dict]) -> list[dict]:
    """The runs with the fixer's regression at retry-attempt-bound (C2) made critical."""
    out = copy.deepcopy(runs)
    for run in out:
        for defect in run["defects"]:
            defect["severity"] = "critical"
    return out


def test_a_regression_a_run_introduced_is_its_own_whatever_its_id():
    """Both arms' fixers introduce a critical regression at retry-attempt-bound, both
    called C2, and both let it escape. The id is shared and the defect is not: each is its
    own run's, so the baseline's escape excuses none of the treatment's, and the
    treatment, better everywhere else, is NO_GO."""
    corpus, as_registered = _sample()
    runs = _critical_regressions(as_registered)
    better = _catching(runs)
    treatment = [
        copy.deepcopy(b) if b["case"].startswith("retry-attempt-bound") else c
        for b, c in zip(runs, better, strict=True)
    ]
    for run in treatment:
        if run["case"].startswith("retry-attempt-bound"):
            (regression,) = run["defects"]
            regression["summary"] = "Another fixer's change, which drops the backoff delay."
            regression["location"] = "retry.py:backoff"
    decision = _decide(_real(runs, "baseline"), _real(treatment, "treatment"), corpus)
    assert decision == {
        "decision": rv.NO_GO,
        "reasons": [
            f"critical defect C2 that treatment run 'treatment/retry-attempt-bound~{k}' "
            "introduced escaped its review"
            for k in range(7)
        ],
    }
    # The gate is all that stands in the way: a treatment that catches its own C2 is a GO.
    decision = _decide(_real(runs, "baseline"), _real(better, "treatment"), corpus)
    assert decision["decision"] == rv.GO
    # A case's defect is the same defect in both arms' runs: B1 escaping both is no new
    # escape, and only the primary decides.
    decision = _decide(_real(as_registered, "baseline"), _real(as_registered, "treatment"), corpus)
    assert decision == {
        "decision": rv.NO_GO,
        "reasons": ["weighted_recall's interval lies wholly below +0.1"],
    }
    # A regression is never: identical arms whose fixers each let a critical one escape
    # are NO_GO on the gate, since the baseline never held the treatment run's.
    decision = _decide(_real(runs, "baseline"), _real(runs, "treatment"), corpus)
    assert decision["decision"] == rv.NO_GO
    assert all("that treatment run" in reason for reason in decision["reasons"])


def test_a_shared_defect_with_no_usable_baseline_run_is_no_proven_new_escape():
    """A case's defect is a new escape only against a baseline run of the case whose
    record settles that it did not escape there. Both arms let B1 escape, and the
    treatment, which also catches C2, is a GO. Where the baseline has no run of one redact
    case, has only an excluded one, or has one whose record does not show where it ended,
    the treatment's B1 escape there proves nothing new: it leaves the comparison open and
    never decides NO_GO. A regression a treatment run introduced is that run's alone, which
    no baseline run could share: its escape is NO_GO whether or not the case is paired."""
    corpus, runs = _sample()
    caught = _catching(runs)
    treatment = _real(
        [
            copy.deepcopy(r) if r["case"].startswith("redact") else c
            for r, c in zip(runs, caught, strict=True)
        ],
        "treatment",
    )
    baseline = _real(runs, "baseline")
    assert _decide(baseline, treatment, corpus)["decision"] == rv.GO
    lone = "redact-authorization-header~0"

    def without(change) -> list[dict]:
        out = []
        for run in copy.deepcopy(baseline):
            if run["case"] == lone:
                run = change(run)
            if run is not None:
                out.append(run)
        return out

    def opened(why: str) -> str:
        return (
            f"critical defect B1 of case '{lone}' escaped the treatment's review, and whether "
            f"it escaped the baseline's is unknown: {why}"
        )

    def exclude(run: dict) -> dict:
        run["excluded"] = "the provider was unavailable"
        return run

    def unsettle(run: dict) -> dict:
        run["launch_window"] = None
        return run

    for change, reasons in (
        (
            lambda run: None,
            [
                f"case(s) ['{lone}'] have no pair of runs",
                opened("the baseline has no run of the case"),
            ],
        ),
        (exclude, [opened("the baseline's run of the case is excluded")]),
        (
            unsettle,
            [
                opened("the controller's record does not settle the baseline run's outcome"),
                "cost_per_validated_success is not measured on both arms",
            ],
        ),
    ):
        assert _decide(without(change), treatment, corpus) == {
            "decision": rv.INCONCLUSIVE,
            "reasons": reasons,
        }

    # A treatment run's own critical regression, unpaired or paired with an excluded run.
    regressions = _critical_regressions(runs)
    lone = "retry-attempt-bound~0"
    better = _real(
        [
            copy.deepcopy(r) if r["case"] == lone else c
            for r, c in zip(regressions, _catching(regressions), strict=True)
        ],
        "treatment",
    )
    baseline = _real(regressions, "baseline")
    for change in (lambda run: None, exclude):
        assert _decide(without(change), better, corpus) == {
            "decision": rv.NO_GO,
            "reasons": [
                f"critical defect C2 that treatment run 'treatment/{lone}' introduced "
                "escaped its review"
            ],
        }


def test_a_margin_worse_on_its_whole_interval_is_no_go():
    """Every clean negative gains a false finding: the false-positive rate rises past its
    margin whatever the treatment gains elsewhere."""
    corpus, runs = _sample()
    treatment = _catching(runs)
    for run in treatment:
        if run["case"].startswith("rename-slug-pattern"):
            review = run["reviews"][0]
            review["result"]["needs_fix_round"] = True
            _none_issued(review)
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
            _issued(second)
            run["reviews"].append(second)
            review_launch = run["launches"][1]
            run["launches"] += [
                {**review_launch, "phase": "FIX", "round": 1},
                {**review_launch, "round": 2},
            ]
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


def _excluded_before_review(run: dict) -> dict:
    """``run`` as one an infrastructure failure stopped before its first review result:
    its ANALYZE_EXECUTE launch and no round, fix, label or commit of its own."""
    out = copy.deepcopy(run)
    out.update(
        commits=[],
        defects=[],
        oracles={},
        reviews=[],
        fixes=[],
        labels=[],
        launches=out["launches"][:1],
        launch_window={**out["launch_window"], "end": "BLOCKED", "last": 1},
        excluded="the provider was unavailable before the first review result",
    )
    out.pop("stopped", None)
    return out


def test_a_run_excluded_before_its_first_review_is_an_exclusion():
    """An infrastructure failure can stop a planned run before any review result. Its
    record holds no round, no review invented for it, and it is neither dropped nor a case
    with no run: it is an exclusion with its reason, shown in the report, also for an arm
    whose every run is excluded, and counted in the excluded share. A run with no round
    and no exclusion is refused, and so is one naming a round it does not have or saying
    it reached READY_FOR_MERGE."""
    prereg, corpus, runs = _files()
    case = "page-count-partial-page"
    runs["runs"] = [_excluded_before_review(r) if r["case"] == case else r for r in runs["runs"]]
    lone = _excluded_before_review(_run(rv.read_json(rv.FIXTURE_RUNS), case))
    runs["runs"].append({**lone, "arm": "infra", "id": "infra/page-count-partial-page"})
    arms = rv.report(rv.check(prereg, corpus, runs))["arms"]
    reason = "the provider was unavailable before the first review result"
    assert arms[ARM]["excluded"] == [{"run": f"{ARM}/{case}", "case": case, "reason": reason}]
    assert (arms[ARM]["cases"], arms[ARM]["missing_cases"]) == (5, [])
    infra = arms["infra"]
    assert infra["excluded"] == [
        {"run": "infra/page-count-partial-page", "case": case, "reason": reason}
    ]
    assert infra["cases"] == 0 and case not in infra["missing_cases"]
    assert len(infra["missing_cases"]) == 5 and infra["runs"] == []
    assert infra["metrics"]["weighted_recall"]["value"] is None
    assert infra["loops"]["mean_review_rounds"] is None

    corpus, sample = _sample()
    baseline = _real(sample, "baseline")
    treatment = _real(_catching(sample), "treatment")
    stopped = [r["case"] for r in treatment[:4]]
    treatment[:4] = [_excluded_before_review(r) for r in treatment[:4]]
    decision = _decide(baseline, treatment, corpus)
    assert decision["decision"] == rv.INCONCLUSIVE
    assert decision["reasons"] == [
        "4 excluded case(s) pass the registered share",
        "the sample pairs 17 defect case(s) and 12 clean negative(s), below the registered "
        "20 and 10",
    ]
    only = [r for r in treatment if r["case"] in stopped]
    decision = _decide(baseline, only, corpus)
    assert decision["decision"] == rv.INCONCLUSIVE
    assert "4 excluded case(s) pass the registered share" in decision["reasons"]
    assert not any(reason.startswith("arm ") for reason in decision["reasons"])
    assert not any(any(c in reason for c in stopped) for reason in decision["reasons"])

    for change, needle in (
        (lambda run: run.pop("excluded"), "only a run excluded for an infrastructure failure"),
        (
            lambda run: run["launches"].append(
                {**run["launches"][0], "phase": "REVIEW", "round": 1, "ordinal": 2}
            ),
            "a round the run does not have",
        ),
        (lambda run: run["launch_window"].update(end="READY_FOR_MERGE"), "or on no round"),
    ):
        prereg, corpus, runs = _files()
        (run,) = [r for r in runs["runs"] if r["case"] == case]
        runs["runs"] = [r for r in runs["runs"] if r["case"] != case]
        run = _excluded_before_review(run)
        change(run)
        runs["runs"].append(run)
        _refused(prereg, corpus, runs, needle)


def test_a_cost_no_launch_measured_rules_out_go():
    corpus, runs = _sample()
    treatment = _real(_catching(runs), "treatment")
    treatment[0]["launches"][0]["provenance"] = "estimated"
    decision = _decide(_real(runs, "baseline"), treatment, corpus)
    assert decision == {
        "decision": rv.INCONCLUSIVE,
        "reasons": ["cost_per_validated_success is not measured on both arms"],
    }


def test_the_cost_criterion_is_decided_on_unrounded_values():
    """The cost per validated success is compared with its margin exactly: the baseline at
    $1.00 a success, a treatment at exactly +50% is within it and a millionth of a dollar
    over it is not, though the report shows both as 1.5."""
    corpus, runs = _sample()
    baseline, treatment = _real(runs, "baseline"), _real(_catching(runs), "treatment")
    for run in baseline + treatment:
        for launch in run["launches"]:
            launch["cost_usd"] = 0.0
    p, _, r = _files()

    def decided(base_cost: float, treatment_cost: float) -> tuple[dict, dict]:
        baseline[0]["launches"][0]["cost_usd"] = base_cost
        treatment[0]["launches"][0]["cost_usd"] = treatment_cost
        ev = rv.check(p, corpus, {"schema": r["schema"], "runs": baseline + treatment})
        arms = rv.report(ev)["arms"]
        return rv.decide(ev, "baseline", "treatment"), {
            arm: arms[arm]["metrics"]["cost_per_validated_success"] for arm in arms
        }

    _, cost = decided(1.0, 1.0)
    successes = {arm: cost[arm]["validated_successes"] for arm in cost}
    assert successes["treatment"] > 2  # so a millionth over the limit rounds to it
    limit = Fraction(3, 2) * successes["treatment"]
    decision, cost = decided(float(successes["baseline"]), float(limit))
    assert decision == {
        "decision": rv.GO,
        "reasons": ["weighted_recall rose by +0.1 or more and every bound held"],
    }
    assert (cost["baseline"]["value"], cost["treatment"]["value"]) == (1.0, 1.5)
    decision, cost = decided(float(successes["baseline"]), float(limit + Fraction(1, 10**6)))
    assert decision == {
        "decision": rv.NO_GO,
        "reasons": ["cost_per_validated_success rose past +50% of the baseline's"],
    }
    assert cost["treatment"]["value"] == 1.5  # shown rounded, decided exactly


def _written(tmp_path: Path, prereg: dict, corpus: dict, runs: dict, literals: dict) -> tuple:
    """The three files written under ``tmp_path``, each number of ``literals`` (a value
    ``runs`` holds once) replaced by the decimal text it maps to."""
    text = json.dumps(runs)
    for value, literal in literals.items():
        assert text.count(json.dumps(value)) == 1, value
        text = text.replace(json.dumps(value), literal)
    paths = []
    for name, content in (
        ("preregistration.json", json.dumps(prereg)),
        ("corpus.json", json.dumps(corpus)),
        ("runs.json", text),
    ):
        (tmp_path / name).write_text(content, encoding="utf-8")
        paths.append(tmp_path / name)
    return tuple(paths)


def test_a_file_is_decided_on_the_decimals_it_wrote(tmp_path, capsys):
    """A run file's cost is the decimal it wrote, never the binary float nearest it. The
    baseline costs $1.00 a success and the treatment is within its +50% margin at exactly
    1.5 times its successes. A treatment total written a ten-quadrillionth of a dollar
    over that is over it, though a float parse reads it as the limit itself, and `decide`
    from the files says so."""
    corpus, runs = _sample()
    baseline, treatment = _real(runs, "baseline"), _real(_catching(runs), "treatment")
    for run in baseline + treatment:
        for launch in run["launches"]:
            launch["cost_usd"] = 0
    p, _, r = _files()
    everything = {"schema": r["schema"], "runs": baseline + treatment}
    cost = rv.report(rv.check(p, corpus, everything))["arms"]
    successes = {arm: cost[arm]["metrics"]["cost_per_validated_success"] for arm in cost}
    successes = {arm: successes[arm]["validated_successes"] for arm in successes}
    baseline[0]["launches"][0]["cost_usd"] = successes["baseline"]
    treatment[0]["launches"][0]["cost_usd"] = 7777.25  # replaced by each literal below
    limit = Decimal(3) * successes["treatment"] / 2
    over = limit + Decimal("1e-16")
    assert float(str(over)) == float(limit)  # what a float parse would decide on
    for literal, decision in (
        (
            str(limit),
            {
                "decision": rv.GO,
                "reasons": ["weighted_recall rose by +0.1 or more and every bound held"],
            },
        ),
        (
            str(over),
            {
                "decision": rv.NO_GO,
                "reasons": ["cost_per_validated_success rose past +50% of the baseline's"],
            },
        ),
    ):
        paths = _written(tmp_path, p, corpus, everything, {7777.25: literal})
        assert rv.decide(rv.load(*paths), "baseline", "treatment") == decision, literal
        argv = ["--preregistration", str(paths[0]), "--corpus", str(paths[1])]
        argv += ["--runs", str(paths[2]), "decide", "--baseline", "baseline"]
        assert rv.main([*argv, "--treatment", "treatment"]) == 0
        assert json.loads(capsys.readouterr().out) == decision, literal


def test_an_excluded_share_at_its_limit_is_decided_exactly(tmp_path):
    """58 excluded cases of 100 are not more than a registered share of 0.58, whether the
    share is read from a file or given as a float, and 59 are. In binary floats
    0.58 * 100 is below 58, which read 58 exclusions as past the share."""
    assert 0.58 * 100 < 58  # what an inexact comparison would read
    _, corpus, raw = _files()
    corpus, runs = _replicated(corpus, _resolve_unknowns(raw["runs"]), 17, 17)
    corpus["cases"] = corpus["cases"][:100]
    kept = {case["id"] for case in corpus["cases"]}
    runs = [run for run in runs if run["case"] in kept]
    prereg = rv.read_json(rv.PREREGISTRATION)
    prereg["decision"]["max_excluded_case_share"] = 0.58
    prereg["uncertainty"]["resamples"] = 20  # the share is all this reads
    baseline = _real(runs, "baseline")

    def past_the_share(excluded: int, from_files: bool) -> bool:
        treatment = _real(runs, "treatment")
        for run in treatment[:excluded]:
            run["excluded"] = "the provider was unavailable"
        everything = {"schema": rv.RUNS_SCHEMA, "runs": baseline + treatment}
        if from_files:
            ev = rv.load(*_written(tmp_path, prereg, corpus, everything, {}))
            assert isinstance(ev.preregistration["decision"]["max_excluded_case_share"], Decimal)
        else:
            ev = rv.check(prereg, corpus, everything)
        reasons = rv.decide(ev, "baseline", "treatment")["reasons"]
        return f"{excluded} excluded case(s) pass the registered share" in reasons

    for from_files in (False, True):
        assert not past_the_share(58, from_files), from_files
        assert past_the_share(59, from_files), from_files


def test_a_label_band_at_its_limit_is_decided_exactly():
    """A band exactly as wide as the registered 0.1 is not wider than it. In binary
    floats 4/10 - 3/10 is wider, and would leave the rate unknown."""
    assert 0.4 - 0.3 > 0.1  # what an inexact comparison would read
    prereg = rv.read_json(rv.PREREGISTRATION)
    (score,) = [rv.score_run(run, prereg) for run in rv.load().runs[:1]]
    at_limit = dataclasses.replace(score, confirmed=3, false=6, unknown=1)
    precision = rv.arm_metrics([at_limit], prereg, None)["precision"]
    assert precision["label_bounds"] == [0.3, 0.4]
    assert precision["status"] == "known"
    wider = dataclasses.replace(score, confirmed=3, false=5, unknown=1)
    assert rv.arm_metrics([wider], prereg, None)["precision"]["status"] == "unknown"


# What one treatment run's launch record keeps, what it then misses, and whether it
# still shows the run stopped right after its final round. Its launches are ordinals 1
# to 4 of the controller's: ANALYZE_EXECUTE, REVIEW 1, FIX 1, REVIEW 2.
_GAPS = {
    "no launch at all": (
        lambda launch: False,
        [
            "the launches of ordinals 1 to 4",
            "ANALYZE_EXECUTE",
            "REVIEW of round 1",
            "REVIEW of round 2",
            "FIX of round 1",
        ],
        False,
    ),
    "no ANALYZE_EXECUTE launch": (
        lambda launch: launch["phase"] != "ANALYZE_EXECUTE",
        ["the launch of ordinal 1", "ANALYZE_EXECUTE"],
        True,
    ),
    "no REVIEW launch of round 2": (
        lambda launch: (launch["phase"], launch.get("round")) != ("REVIEW", 2),
        ["the launch of ordinal 4", "REVIEW of round 2"],
        False,
    ),
    "no FIX launch": (
        lambda launch: launch["phase"] != "FIX",
        ["the launch of ordinal 3", "FIX of round 1"],
        True,
    ),
    "no record of the controller's launch window": (
        None,
        ["the controller's record of its launch window"],
        False,
    ),
}


@pytest.mark.parametrize("gap", sorted(_GAPS))
def test_a_run_whose_launch_record_is_incomplete_has_no_measured_cost(gap):
    """A launch nobody recorded is not a launch that cost nothing: one treatment run short
    of its whole record, or of one launch of it, leaves every usage sum unknown, its known
    part shown apart, and the cost criterion open. A record that no longer shows the run
    stopped right after its final round leaves its outcome open as well. With the whole
    record the same arms are a GO
    (test_go_needs_the_primary_effect_every_bound_and_the_registered_sample)."""
    kept, missing, settled = _GAPS[gap]
    corpus, runs = _sample()
    treatment = _real(_catching(runs), "treatment")
    (short,) = [r for r in treatment if r["case"] == "page-count-partial-page~0"]
    if kept is None:
        short["launch_window"] = None
    else:
        short["launches"] = [launch for launch in short["launches"] if kept(launch)]
    p, _, r = _files()
    ev = rv.check(p, corpus, {"schema": r["schema"], "runs": _real(runs, "baseline") + treatment})
    unsettled = (
        []
        if settled
        else [
            "the controller's record does not settle the outcome of treatment run "
            "'treatment/page-count-partial-page~0': no recorded stop shows it ended at its "
            "record's final round"
        ]
    )
    assert rv.decide(ev, "baseline", "treatment") == {
        "decision": rv.INCONCLUSIVE,
        "reasons": [*unsettled, "cost_per_validated_success is not measured on both arms"],
    }
    arm = rv.report(ev)["arms"]["treatment"]
    why = "the launch record of run(s) ['treatment/page-count-partial-page~0'] is incomplete"
    for usage in ("total_cost_usd", "launch_wall_clock_seconds"):
        assert arm["metrics"][usage]["value"] is None, usage
        assert arm["metrics"][usage]["provenance"] == "unknown", usage
        assert arm["metrics"][usage]["why_unknown"] == why, usage
    assert arm["metrics"]["total_cost_usd"]["known_part"] > 0
    assert arm["metrics"]["cost_per_validated_success"]["value"] is None
    (row,) = [row for row in arm["runs"] if row["run"] == short["id"]]
    assert row["missing_launches"] == missing
    assert (row["ended"], row["validated_success"]) == (
        ("certified", True) if settled else ("unknown", None)
    )


def test_an_omitted_retry_leaves_the_cost_unknown():
    """Which launches a run made is the controller's record, not its launch list: the
    controller spends one launch ordinal before every launch, a failed attempt included.
    A $200 REVIEW attempt that failed before round 1's breaks the cost margin. A record
    that leaves only that attempt out still covers every phase and round, and is short of
    ordinal 2: its cost is unknown, never the cheaper total."""
    corpus, runs = _sample()
    baseline = _real(runs, "baseline")
    treatment = _real(_catching(runs), "treatment")
    (retried,) = [r for r in treatment if r["case"] == "page-count-partial-page~0"]
    retried["launches"].insert(1, {**retried["launches"][1], "cost_usd": 200.0})
    for ordinal, launch in enumerate(retried["launches"], 1):
        launch["ordinal"] = ordinal
    retried["launch_window"]["last"] = 5
    assert _decide(baseline, treatment, corpus) == {
        "decision": rv.NO_GO,
        "reasons": ["cost_per_validated_success rose past +50% of the baseline's"],
    }
    del retried["launches"][1]
    assert {(launch["phase"], launch.get("round")) for launch in retried["launches"]} == {
        ("ANALYZE_EXECUTE", None),
        ("REVIEW", 1),
        ("FIX", 1),
        ("REVIEW", 2),
    }
    p, _, r = _files()
    ev = rv.check(p, corpus, {"schema": r["schema"], "runs": baseline + treatment})
    assert rv.decide(ev, "baseline", "treatment") == {
        "decision": rv.INCONCLUSIVE,
        "reasons": ["cost_per_validated_success is not measured on both arms"],
    }
    arm = rv.report(ev)["arms"]["treatment"]
    (row,) = [row for row in arm["runs"] if row["run"] == retried["id"]]
    assert row["missing_launches"] == ["the launch of ordinal 2"]
    assert arm["metrics"]["total_cost_usd"]["value"] is None


def test_an_omitted_leading_attempt_leaves_the_cost_unknown():
    """The state's launch_seq is a high-water mark: it ends a run's ordinals and never
    starts them. A run the controller created for its case started at launch_seq 0, so
    its first launch spent ordinal 1, and a $200 first attempt that failed before the
    ANALYZE_EXECUTE that counted breaks the cost margin. A record that leaves it out is
    short of ordinal 1, and one that cannot show where the run's launches start is short
    of that record: either way its cost is unknown, never the cheaper total."""
    corpus, runs = _sample()
    baseline = _real(runs, "baseline")
    treatment = _real(_catching(runs), "treatment")
    (retried,) = [r for r in treatment if r["case"] == "page-count-partial-page~0"]
    retried["launches"].insert(0, {**retried["launches"][0], "cost_usd": 200.0})
    for ordinal, launch in enumerate(retried["launches"], 1):
        launch["ordinal"] = ordinal
    retried["launch_window"]["last"] = 5
    assert _decide(baseline, treatment, corpus) == {
        "decision": rv.NO_GO,
        "reasons": ["cost_per_validated_success rose past +50% of the baseline's"],
    }
    del retried["launches"][0]
    assert [launch["ordinal"] for launch in retried["launches"]] == [2, 3, 4, 5]
    p, _, r = _files()
    open_cost = {
        "decision": rv.INCONCLUSIVE,
        "reasons": ["cost_per_validated_success is not measured on both arms"],
    }
    ev = rv.check(p, corpus, {"schema": r["schema"], "runs": baseline + treatment})
    assert rv.decide(ev, "baseline", "treatment") == open_cost
    (row,) = [
        row for row in rv.report(ev)["arms"]["treatment"]["runs"] if row["run"] == retried["id"]
    ]
    assert row["missing_launches"] == ["the launch of ordinal 1"]
    # Without the record of the run's creation, nothing shows where its ordinals start.
    retried["launch_window"]["start"] = "unrecorded"
    ev = rv.check(p, corpus, {"schema": r["schema"], "runs": baseline + treatment})
    assert rv.decide(ev, "baseline", "treatment") == open_cost
    (row,) = [
        row for row in rv.report(ev)["arms"]["treatment"]["runs"] if row["run"] == retried["id"]
    ]
    assert row["missing_launches"] == [
        "the controller's record of where its launches start: no record shows it created "
        "the run for this case"
    ]


def _stale_at_the_end(runs: list[dict], went_on: int | None) -> list[dict]:
    """The redact runs with their final round stale. With ``went_on`` the controller
    spent that many ordinals after it, on the moved revision no record holds; with
    ``None`` no record shows where its launches ended."""
    out = copy.deepcopy(runs)
    for run in out:
        if run["case"].startswith("redact"):
            _none_issued(run["reviews"][-1], "stale")
            run["stopped"] = "the revision moved during round 2's review"
            if went_on is None:
                run["launch_window"].update(end="unrecorded", last=None)
            else:
                run["launch_window"].update(end="READY_FOR_MERGE")
                run["launch_window"]["last"] += went_on
    return out


def test_a_run_that_goes_on_past_a_stale_round_has_no_known_outcome_or_cost():
    """A stale round ends what a run record can hold, never the run: the controller
    reviews the moved revision next. Where it spent ordinals after the stale round, the
    run went on, so the stale round decides neither its certificate, its success nor its
    cost: B1, at the stale candidate, may have escaped, and the cost is unknown. Where no
    record shows where its launches ended, the same holds. Only a run whose record shows
    it stopped at the stale round ends there
    (test_a_clean_reply_alone_cannot_trigger_the_hard_gate)."""
    corpus, runs = _sample()
    caught = _catching(runs)
    baseline = _real(
        [
            copy.deepcopy(c) if c["case"].startswith("redact") else copy.deepcopy(r)
            for r, c in zip(runs, caught, strict=True)
        ],
        "baseline",
    )
    for run in caught:
        if run["case"].startswith("redact"):
            run["commits"][0]["fixes"] = ["B2"]  # B1 is still there at the stale round
    caught = _real(caught, "treatment")
    for went_on in (2, None):
        treatment = _stale_at_the_end(caught, went_on)
        decision = _decide(baseline, treatment, corpus)
        assert decision["decision"] == rv.INCONCLUSIVE, went_on
        assert "cost_per_validated_success is not measured on both arms" in decision["reasons"]
        assert [r for r in decision["reasons"] if "critical defect" in r] == [
            f"critical defect B1 of run 'treatment/redact-authorization-header~{k}' may have "
            "escaped the treatment's review: the controller's record does not settle the "
            "run's outcome"
            for k in range(7)
        ]
        p, _, r = _files()
        ev = rv.check(p, corpus, {"schema": r["schema"], "runs": baseline + treatment})
        arm = rv.report(ev)["arms"]["treatment"]
        (row,) = [row for row in arm["runs"] if row["case"] == "redact-authorization-header~0"]
        # B1 is critical, and still no reason to call the run no success: the candidate
        # it ended on is not the stale one.
        assert (row["ended"], row["validated_success"]) == ("unknown", None)
        missing = row["missing_launches"]
        if went_on is None:
            assert missing == [
                "the controller's record of where its launches end: the run was not found "
                "at a stop the evaluation ended it at"
            ]
        else:
            assert missing == ["the launches of ordinals 5 to 6"]


# Ways a treatment run's record can lose the stop that ties its outcome to its final
# round: no launch window, a window whose end was not found, an ordinal spent after the
# last launch it holds, or a last launch that is no fix of the final round.
_LOST_STOPS = {
    "no launch window": lambda run: run.update(launch_window=None),
    "no recorded end": lambda run: run["launch_window"].update(end="unrecorded", last=None),
    "an ordinal after the record": lambda run: run["launch_window"].update(
        last=run["launch_window"]["last"] + 1
    ),
    "a replan after the final round": lambda run: (
        run["launches"].append(
            {
                **run["launches"][0],
                "phase": "REPLAN_REEXECUTE",
                "ordinal": run["launch_window"]["last"] + 1,
            }
        ),
        run["launch_window"].update(last=run["launch_window"]["last"] + 1),
    ),
}


@pytest.mark.parametrize("lost", sorted(_LOST_STOPS))
def test_a_missing_stop_leaves_an_issued_certificate_undecided(lost):
    """A certificate the controller issued is the outcome of its round, and the run's
    only on the controller's record that the run stopped right after it: it can resume a
    certified run once the revision moves, and certify another candidate or none. The
    treatment's B1 sits at a candidate the controller certified, a new critical escape
    with the stop recorded. With the stop lost the run's outcome is open: B1 may have
    escaped, the run's success is unknown, and the gate decides nothing."""
    corpus, runs = _sample()
    caught = _catching(runs)
    baseline = _real(
        [
            copy.deepcopy(c) if c["case"].startswith("redact") else copy.deepcopy(r)
            for r, c in zip(runs, caught, strict=True)
        ],
        "baseline",
    )
    treatment = _real(caught, "treatment")
    for run in treatment:
        if run["case"].startswith("redact"):
            run["commits"][0]["fixes"] = ["B2"]
    assert _decide(baseline, treatment, corpus)["decision"] == rv.NO_GO
    for run in treatment:
        if run["case"].startswith("redact"):
            _LOST_STOPS[lost](run)
    decision = _decide(baseline, treatment, corpus)
    assert decision["decision"] == rv.INCONCLUSIVE
    assert [r for r in decision["reasons"] if "critical defect" in r] == [
        f"critical defect B1 of run 'treatment/redact-authorization-header~{k}' may have "
        "escaped the treatment's review: the controller's record does not settle the "
        "run's outcome"
        for k in range(7)
    ]
    p, _, r = _files()
    ev = rv.check(p, corpus, {"schema": r["schema"], "runs": baseline + treatment})
    arm = rv.report(ev)["arms"]["treatment"]
    (row,) = [row for row in arm["runs"] if row["case"] == "redact-authorization-header~0"]
    assert (row["ended"], row["validated_success"], row["escaped"]) == ("unknown", None, [])
    assert arm["metrics"]["escaped_critical_defects"]["value"] is None


def test_a_run_that_went_on_past_a_defective_candidate_has_no_known_success():
    """The controller spent an ordinal after the record's last launch: the run went on
    past its final round, so the candidate it ended on is another. The major C2 at
    retry-attempt-bound's final candidate, or a deliberately empty validation set
    there, rules out a success only at the candidate the run ended on, so neither does:
    the run's success is unknown, and so are the arm's cost per validated success and
    its escape count, whatever the final round holds."""

    def went_on(case: str, also: dict):
        def mutate(run, controller):
            controller.update(also)
            run["launch_window"]["last"] += 1

        return _final_of(case, mutate)

    for case, also in (
        ("retry-attempt-bound", {}),
        ("page-count-partial-page", {"validation": "empty_set"}),
    ):
        metrics, row = went_on(case, also)
        assert (row["ended"], row["validated_success"]) == ("unknown", None), case
        assert metrics["cost_per_validated_success"]["value"] is None
        escapes = metrics["escaped_critical_defects"]
        assert (escapes["value"], escapes["known_part"]) == (None, 1)
        assert f"'{ARM}/{case}'" in escapes["why_unknown"]
        # Stopped where the record does, the same final round rules the success out.
        metrics, row = _final_of(case, lambda run, c, also=also: c.update(also))
        assert (row["ended"], row["validated_success"]) == ("certified", False), case


def _beside_a1(runs: list[dict], verdict: str, count: int = 1) -> list[dict]:
    """The runs with ``count`` more findings at page-count-partial-page's round 1, beside
    its two reports of A1, each labeled ``verdict`` (``confirmed`` names A1)."""
    out = copy.deepcopy(runs)
    for run in out:
        if not run["case"].startswith("page-count-partial-page"):
            continue
        review = run["reviews"][0]
        for n in range(4, 4 + count):
            review["result"]["findings"].append(
                {
                    "id": f"R1-F{n}",
                    "classification": "non-blocked",
                    "title": "The page count is wrong for a partial last page",
                    "location": "pages.py:page_count",
                    "required_resolution": "Count a partial last page as a page.",
                }
            )
            label = {
                "finding": f"R1-F{n}",
                "round": 1,
                "candidate_sha": review["result"]["reviewed_head_sha"],
                "verdict": verdict,
                "adjudicator": AUTHOR,
                "labeled_at": LABELED,
            }
            if verdict == "confirmed":
                label["defect"] = "A1"
            else:
                label["reason"] = "Not settled: it may restate A1 or describe another rounding."
            run["labels"].append(label)
            run["fixes"][0]["resolutions"][f"R1-F{n}"] = "fixed"
    return out


def test_unknown_findings_that_could_be_duplicates_rule_out_go():
    """Unknown findings beside reports of A1 could each be one more report of A1. Until
    they are settled the duplicate ratio is unknown, so its margin is never accepted on
    them, even where the precision band stays under its limit."""
    corpus, runs = _sample()
    p, _, r = _files()
    one_each = _beside_a1(_catching(runs), "unknown")
    ev = rv.check(
        p,
        corpus,
        {"schema": r["schema"], "runs": _real(runs, "baseline") + _real(one_each, "treatment")},
    )
    assert rv.decide(ev, "baseline", "treatment") == {
        "decision": rv.INCONCLUSIVE,
        "reasons": ["duplicate_ratio is unknown on an arm"],
    }
    metrics = rv.report(ev)["arms"]["treatment"]["metrics"]
    assert metrics["precision"]["status"] == "known"
    # 7 duplicates of 49 confirmed; 14 of 56 if all 7 restate A1. A1 and A2 are the only
    # registered defects of that round and both are named, so an unknown finding there is
    # a duplicate or false, never a first report: the lower bound stays 7 of 49.
    assert metrics["duplicate_ratio"]["value"] == round(7 / 49, 6)
    assert metrics["duplicate_ratio"]["label_bounds"] == [round(7 / 49, 6), 0.25]
    # Three of them at every copy are enough to decide the margin: settled as reports of
    # A1 they break it on its whole interval, and unsettled they leave it open.
    settled = _beside_a1(_catching(runs), "confirmed", 3)
    assert _decide(_real(runs, "baseline"), _real(settled, "treatment"), corpus) == {
        "decision": rv.NO_GO,
        "reasons": ["duplicate_ratio's whole interval is worse than its margin 0.1"],
    }
    open_ = _beside_a1(_catching(runs), "unknown", 3)
    decision = _decide(_real(runs, "baseline"), _real(open_, "treatment"), corpus)
    assert decision["decision"] == rv.INCONCLUSIVE
    assert "duplicate_ratio is unknown on an arm" in decision["reasons"]


def _final_of(case: str, mutate) -> tuple[dict, dict]:
    """The fixture's metrics and ``case``'s run row, its final round's controller record
    and run changed by ``mutate(run, controller)``."""
    prereg, corpus, runs = _files()
    run = _run(runs, case)
    mutate(run, run["reviews"][-1]["controller"])
    report = rv.report(rv.check(prereg, corpus, runs))["arms"][ARM]
    (row,) = [row for row in report["runs"] if row["case"] == run["case"]]
    return report["metrics"], row


def _redact_final(mutate) -> tuple[dict, dict]:
    """:func:`_final_of` the redact run. As registered, B1 escapes there."""
    return _final_of("redact-authorization-header", mutate)


# The redact run's final round, a clean reply over a passing validation of its own
# candidate, with the controller's record of what it certified changed.
_ROUND_1_SHA = "06398f9ea48e0f466fe9234deb58980a0d74752b"
_NONE_ISSUED = {"certificate": "not_issued", "certified_as": None}
_NOT_CERTIFIED = {
    "the controller found the round stale": {**_NONE_ISSUED, "disposition": "stale"},
    "a clean round it issued no certificate for": _NONE_ISSUED,
    "a clean round over a failed validation": {**_NONE_ISSUED, "validation": "failed"},
    "a clean round over another candidate's validation": {
        **_NONE_ISSUED,
        "validated_sha": _ROUND_1_SHA,
    },
    "a failed validation, the certificate unrecorded": {
        "certificate": "unrecorded",
        "certified_as": None,
        "validation": "failed",
    },
}


@pytest.mark.parametrize("record", sorted(_NOT_CERTIFIED))
def test_a_clean_reply_the_controller_would_not_certify_escapes_nothing(record):
    """An escape is a registered defect at a candidate the controller certified. B1 sits
    at the redact run's clean reply whatever its record says. The controller records a
    clean round before it issues its certificate, and can block with none: over a round
    it records issued none for, a clean round over its own passing validation included,
    over a stale round, a failed validation or another candidate's, it certified nothing,
    and nothing escapes. The run did not end on a certificate, so it says why it stopped,
    and it stopped short of READY_FOR_MERGE."""
    change = _NOT_CERTIFIED[record]

    def mutate(run, controller):
        controller.update(change)
        run["stopped"] = "BLOCKED: no review certificate was issued"
        run["launch_window"]["end"] = "BLOCKED"

    metrics, row = _redact_final(mutate)
    assert metrics["escaped_critical_defects"] == {"value": 0, "defects": [], "possible": []}
    assert (row["escaped"], row["possible_escapes"]) == ([], [])
    assert (row["ended"], row["validated_success"]) == ("stopped", False)
    assert row["claims"]["final_reply_clean"] is True
    prereg, corpus, runs = _files()
    run = _run(runs, "redact-authorization-header")
    run["reviews"][-1]["controller"].update(change)
    run["launch_window"]["end"] = "BLOCKED"
    _refused(prereg, corpus, runs, "stopped must be a non-empty string")
    prereg, corpus, runs = _files()
    run = _run(runs, "redact-authorization-header")
    run["reviews"][-1]["controller"].update(change)
    run["stopped"] = "BLOCKED: no review certificate was issued"
    _refused(prereg, corpus, runs, "enters READY_FOR_MERGE only over")


def test_a_clean_reply_over_a_missing_record_decides_no_escape():
    """A clean history entry and a passing validation of the candidate are what issuance
    needs, not proof that it happened. With no record of the round's certificate, B1 may
    have escaped: the count is unknown and never read as zero, whatever else the
    controller recorded of the round. A run whose success that record would decide leaves
    the cost per validated success unknown too; the redact run is no success either way,
    since B1 is critical."""
    unrecorded = {"certificate": "unrecorded", "certified_as": None}
    for also in (
        {},
        {"disposition": "unrecorded"},
        {"validation": "unrecorded", "validated_sha": None},
    ):
        metrics, row = _redact_final(lambda run, c, also=also: c.update(unrecorded, **also))
        escapes = metrics["escaped_critical_defects"]
        assert (escapes["value"], escapes["known_part"], escapes["defects"]) == (None, 0, [])
        assert escapes["possible"] == [
            {"case": "redact-authorization-header", "defect": "B1", "belongs_to": "case"}
        ]
        assert "does not settle the outcome" in escapes["why_unknown"]
        assert (row["ended"], row["validated_success"], row["escaped"]) == ("unknown", False, [])
        assert metrics["cost_per_validated_success"]["value"] == 3.666667

    prereg, corpus, runs = _files()
    _run(runs, "page-count-partial-page")["reviews"][-1]["controller"].update(unrecorded)
    arm = rv.report(rv.check(prereg, corpus, runs))["arms"][ARM]
    (row,) = [row for row in arm["runs"] if row["case"] == "page-count-partial-page"]
    assert (row["ended"], row["validated_success"]) == ("unknown", None)
    per_success = arm["metrics"]["cost_per_validated_success"]
    assert per_success["value"] is None
    assert per_success["validated_successes"] == 2
    assert "is unknown" in per_success["why_unknown"]


def test_a_deliberately_empty_validation_set_certifies_and_validates_nothing():
    """No validation command configured is a set the controller presents, not missing
    evidence: B1 escapes the certified candidate, and the run is no validated success."""
    metrics, row = _redact_final(lambda run, c: c.update(validation="empty_set"))
    assert metrics["escaped_critical_defects"]["value"] == 1
    assert (row["ended"], row["validated_success"]) == ("certified", False)


def test_the_controller_record_is_held_to_what_the_controller_does():
    """A record the controller would never write is refused: a certificate issued over a
    round it certifies nothing for, or one of another round or revision, which this round
    did not get (the controller keeps an earlier round's when it issues none)."""
    issued_elsewhere = "a certificate certifies only the round and revision it was issued for"
    cases = [
        ({"validation": "failed"}, "ADR 0006 D12.2"),
        ({"validated_sha": _ROUND_1_SHA}, "ADR 0006 D12.2"),
        ({"validation": "unrecorded", "validated_sha": None}, "ADR 0006 D12.2"),
        ({"disposition": "unrecorded"}, "ADR 0006 D12.2"),
        (
            {"certified_as": {"review_round": 1, "reviewed_head_sha": _ROUND_1_SHA}},
            issued_elsewhere,
        ),
        (
            {
                "certified_as": {
                    "review_round": 1,
                    "reviewed_head_sha": "6d81b1a41cd7675912df07e8d43501e5976dc9a0",
                }
            },
            issued_elsewhere,
        ),
        ({"certified_as": None}, "certified_as must be an object"),
        ({"certificate": "not_issued"}, "records no issued certificate and names one"),
        ({"certificate": "probably"}, "certificate must be one of"),
        ({"disposition": "needs_fix"}, "needs a fix over a clean reply"),
        ({"validation": "unrecorded"}, "records no validation and names a validated SHA"),
        ({"validated_sha": None}, "validated_sha must be a full"),
        ({"disposition": "approved"}, "disposition must be one of"),
    ]
    for change, needle in cases:
        prereg, corpus, runs = _files()
        _run(runs, "redact-authorization-header")["reviews"][-1]["controller"].update(change)
        _refused(prereg, corpus, runs, needle)
    prereg, corpus, runs = _files()
    _run(runs, "redact-authorization-header")["reviews"][0]["controller"]["disposition"] = "clean"
    _refused(prereg, corpus, runs, "is clean over a reply that needs a fix")
    prereg, corpus, runs = _files()
    _run(runs, "page-count-partial-page")["reviews"][0]["controller"]["disposition"] = "stale"
    _refused(prereg, corpus, runs, "a run's record ends at a stale round")


def test_a_clean_reply_alone_cannot_trigger_the_hard_gate():
    """The treatment confirms B1 and its fix leaves it in place, so B1 sits at the clean
    reply of its last round, where the baseline fixed it. Certified, that is a new
    critical escape and NO_GO, over the empty validation set too. Over a stale round the
    run stopped at, or a clean round it issued no certificate for, the controller
    certified nothing, so nothing escaped and the gate does not stand in the way; with no
    record of the certificate it decides nothing either way."""
    corpus, runs = _sample()
    caught = _catching(runs)
    baseline = [
        copy.deepcopy(c) if c["case"].startswith("redact") else copy.deepcopy(r)
        for r, c in zip(runs, caught, strict=True)
    ]

    def treatment(change: dict, stopped: str = "") -> list[dict]:
        out = copy.deepcopy(caught)
        for run in out:
            if run["case"].startswith("redact"):
                run["commits"][0]["fixes"] = ["B2"]
                run["reviews"][-1]["controller"].update(change)
                if stopped:
                    run["stopped"] = stopped
                    run["launch_window"]["end"] = "BLOCKED"
        return _real(out, "treatment")

    certified = _decide(_real(baseline, "baseline"), treatment({}), corpus)
    assert certified["decision"] == rv.NO_GO
    assert certified["reasons"] == [
        f"critical defect B1 of case 'redact-authorization-header~{k}' escaped the "
        "treatment's review"
        for k in range(7)
    ]
    empty = _decide(_real(baseline, "baseline"), treatment({"validation": "empty_set"}), corpus)
    assert empty == certified
    go = {
        "decision": rv.GO,
        "reasons": ["weighted_recall rose by +0.1 or more and every bound held"],
    }
    # The controller found the round stale, or recorded it clean over its own passing
    # validation and issued no certificate, and the run stopped there: nothing escaped.
    for change in ({**_NONE_ISSUED, "disposition": "stale"}, _NONE_ISSUED):
        assert _decide(_real(baseline, "baseline"), treatment(change, "BLOCKED"), corpus) == go
    unrecorded = _decide(
        _real(baseline, "baseline"),
        treatment({"certificate": "unrecorded", "certified_as": None}),
        corpus,
    )
    assert unrecorded["decision"] == rv.INCONCLUSIVE
    assert [r for r in unrecorded["reasons"] if "critical defect" in r] == [
        f"critical defect B1 of run 'treatment/redact-authorization-header~{k}' may have "
        "escaped the treatment's review: the controller's record does not settle the "
        "run's outcome"
        for k in range(7)
    ]
    # A certified escape is new only when the baseline's run is known to have held it
    # back: where the baseline's record is the incomplete one, it decides nothing either.
    unknown_before = copy.deepcopy(runs)
    for run in unknown_before:
        if run["case"].startswith("redact"):
            run["reviews"][-1]["controller"].update(certificate="unrecorded", certified_as=None)
    decision = _decide(_real(unknown_before, "baseline"), treatment({}), corpus)
    assert [r for r in decision["reasons"] if "critical defect" in r] == [
        f"critical defect B1 of case 'redact-authorization-header~{k}' escaped the "
        "treatment's review, and whether it escaped the baseline's is unknown"
        for k in range(7)
    ]
    assert decision["decision"] != rv.GO


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


def test_the_baseline_cost_facts_are_the_codes():
    """Section 2's cost fact. A launch's provider summary carries ``cost_micro_usd`` from
    the Claude stream reducer alone, which Claude Code reads its reply through with its
    default ``stream-json`` output, and the Claude Agent SDK provider through its helper;
    no other provider, and Claude Code with ``output_format: text``, records a cost. The
    reducer turns a result's ``total_cost_usd`` into it, and the SDK's value through the
    real helper is held by test_claude_sdk.py::test_a_completed_session_through_the_real_helper.
    """
    from autoforge import providers
    from autoforge.claude_stream import ClaudeStream
    from autoforge.config import KNOWN_PROVIDERS

    base = rv.read_json(rv.PREREGISTRATION)["baseline"]["launch_cost"]
    src = REPO_ROOT / "src" / "autoforge"
    field = base["summary_field"]
    writers = sorted(p.name for p in src.rglob("*.py") if f'"{field}"' in p.read_text("utf-8"))
    assert writers == ["claude_stream.py"]
    built_in: dict[str, set[str]] = {}
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for node in ast.walk(cls):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "ClaudeStream"
                ):
                    built_in.setdefault(path.name, set()).add(cls.name)
    assert built_in == {
        "providers.py": {"ClaudeCodeProvider"},
        "claude_sdk.py": {"ClaudeSdkSession"},
    }
    assert set(base["providers"]) <= set(KNOWN_PROVIDERS)
    assert providers._REGISTRY["claude"] is providers.ClaudeCodeProvider
    assert providers._REGISTRY["claude-sdk"] is providers.ClaudeSdkProvider
    assert providers.CLAUDE_DEFAULT_OUTPUT_FORMAT == base["claude_output_format"]
    stream = ClaudeStream(cwd="/work")
    record = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    stream.feed(json.dumps({**record, "total_cost_usd": 0.0125}).encode())
    stream.stream_ended()
    assert stream.summary()[field] == 12_500


def test_a_launch_window_is_read_in_the_controllers_terms():
    """A run record's launch window is read in the controller's own terms: a run it
    creates starts at launch_seq 0, so its first launch spends ordinal 1; a window ends
    at a phase the run stops at with the merge gate closed, never at DONE, which follows
    a merge; and a record's launches are of the phases that launch an agent before one,
    which UPDATE_EPIC follows."""
    from autoforge.state import AutoForgeState
    from autoforge.transitions import AGENT_PHASES, STOP_PHASES

    assert AutoForgeState.__dataclass_fields__["launch_seq"].default == 0
    assert set(rv.WINDOW_ENDS) - {"unrecorded"} == {p.value for p in STOP_PHASES} - {"DONE"}
    assert set(rv.LAUNCH_PHASES) == {p.value for p in AGENT_PHASES} - {"UPDATE_EPIC"}


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
