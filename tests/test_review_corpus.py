"""REV-R1's corpus checks, git verification and offline scorecards (#356)."""

import json
import os
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from autoforge.git_transport import NO_AUTO_MAINTENANCE
from tests import review_corpus
from tests.review_corpus import (
    BOOTSTRAP_DRAWS,
    BOOTSTRAP_TABLES,
    MAX_FILE_BYTES,
    MAX_PLANNED_PAIRS,
    MAX_VARIANTS,
    SEED,
    SYNTHETIC_CORPUS,
    SYNTHETIC_SUBMISSIONS,
    CorpusError,
    check_corpus,
    check_submissions,
    corpus_digest,
    describe,
    input_digest,
    main,
    multiplicities,
    pins_digest,
    read_json,
    score,
    verify,
)

DOC = Path(__file__).resolve().parents[1] / "docs" / "review-corpus.md"


@pytest.fixture
def corpus():
    return json.loads(SYNTHETIC_CORPUS.read_bytes())


@pytest.fixture
def subs():
    return json.loads(SYNTHETIC_SUBMISSIONS.read_bytes())


def repin(subs):
    """Record every run as launched under its variant's pins as they now stand."""
    for run in subs["runs"]:
        run["pins_sha256"] = pins_digest(subs["variants"][run["variant"]])
    return subs


def rebind(corpus, subs):
    """Point the submissions at the corpus as mutated, inputs and pins included."""
    subs["corpus"] = corpus_digest(corpus)
    for run in subs["runs"]:
        run["input"] = input_digest(corpus["cases"][run["case"]])
    return repin(subs)


def scored(corpus, subs):
    return score(corpus, rebind(corpus, subs))


def run_of(subs, variant, case, seed=1):
    return next(
        r for r in subs["runs"] if (r["variant"], r["case"], r["seed"]) == (variant, case, seed)
    )


def label(by, verdict, role="adjudicator", decided="2026-06-25", defect=None, match=False):
    item = {"by": by, "role": role, "verdict": verdict, "decided": decided, "artifact": "test"}
    if match:
        item["defect"] = defect
    return item


def items(case):
    return case["context"]["items"]


def uncaptured(item_id, kind, ref, source=None):
    return {
        "id": item_id,
        "kind": kind,
        "source": source,
        "ref": ref,
        "observed_at": None,
        "status": "not_captured",
        "text": None,
        "sha256": None,
    }


def captured(item_id, kind, ref, observed_at, text, source):
    item = uncaptured(item_id, kind, ref, source)
    item.update(status="captured", observed_at=observed_at)
    set_text(item, text)
    return item


def metric(report, variant, key):
    return report["variants"][variant]["metrics"][key]


def contrast(report, key):
    return report["contrasts"]["variant-a"]["metrics"][key]


# -- the synthetic fixture, by hand -------------------------------------------


def test_synthetic_scorecards_match_hand_computed_values(corpus, subs):
    report = score(corpus, subs)
    assert report["evidence"] == "synthetic"
    assert "no benchmark decision" in report["claim"]
    assert report["plan"] == {"cases": 6, "seeds": [1], "clusters": 3}
    assert report["labels"]["defects"] == {"confirmed": 5, "rejected": 1, "unknown": 2}
    assert report["labels"]["negatives"] == {
        "settled": 2,
        "censored": 1,
        "unlabeled": 0,
        "refuted": 0,
    }
    base, other = report["variants"]["baseline"], report["variants"]["variant-a"]
    assert base["runs"] == {"completed": 6, "failed": 0, "excluded": 0, "missing": 0}
    assert base["coverage_complete"] and other["coverage_complete"]
    assert base["findings"] == {"confirmed": 4, "false": 4, "unknown": 2, "duplicates": 1}
    assert other["findings"] == {"confirmed": 3, "false": 1, "unknown": 2, "duplicates": 0}
    assert base["cost_microusd"] == 6000 and other["cost_microusd"] is None
    # Weighted recall: alpha 4 of 12 (its unknown finding may have found the
    # critical 8), beta 1 of 9, gamma 2 of 2 (its rejected and unknown defects
    # are outside the population).
    recall = metric(report, "baseline", "recall")
    assert recall["observations"] == [7, 8, 23]
    assert recall["point"] == "7/23" and recall["bounds"] == ["7/23", "15/23"]
    assert metric(report, "variant-a", "recall")["observations"] == [20, 1, 23]
    precision = metric(report, "baseline", "precision")
    assert precision["observations"] == [4, 2, 10]
    assert precision["point"] == "1/2" and precision["bounds"] == ["2/5", "3/5"]
    other_precision = metric(report, "variant-a", "precision")
    assert other_precision["point"] == "3/4" and other_precision["bounds"] == ["1/2", "5/6"]
    # Only the two settled negatives: the censored one's false alarm is
    # precision's, not FPR's.
    fpr = metric(report, "baseline", "false_positive_rate")
    assert fpr["observations"] == [1, 0, 2] and fpr["point"] == "1/2"
    other_fpr = metric(report, "variant-a", "false_positive_rate")
    assert other_fpr["point"] == "0" and other_fpr["bounds"] == ["0", "1/2"]
    pairs = report["contrasts"]["variant-a"]["pairs"]
    assert pairs == {"paired": 6, "excluded": 0, "incomplete": []}
    assert contrast(report, "recall")["difference"] == "13/23"
    assert contrast(report, "recall")["bounds"] == ["5/23", "14/23"]
    assert contrast(report, "precision")["difference"] == "1/4"
    assert contrast(report, "precision")["bounds"] == ["-1/10", "13/30"]
    assert contrast(report, "false_positive_rate")["difference"] == "-1/2"
    assert contrast(report, "false_positive_rate")["bounds"] == ["-1/2", "0"]
    # Label uncertainty wider than 0.10 leaves every headline contrast unusable.
    for key in ("recall", "precision", "false_positive_rate"):
        assert not contrast(report, key)["label_bands_within"]
        assert not contrast(report, key)["usable"]


def test_strata_split_recall_by_severity_discovery_and_risk(corpus, subs):
    report = score(corpus, subs)

    def obs(variant, key):
        return metric(report, variant, key)["observations"]

    assert obs("baseline", "recall/severity/critical") == [0, 1, 2]
    assert obs("variant-a", "recall/severity/critical") == [2, 0, 2]
    assert obs("baseline", "recall/severity/high") == [1, 0, 1]
    assert obs("variant-a", "recall/severity/medium") == [0, 0, 1]
    assert obs("variant-a", "recall/severity/low") == [0, 1, 1]
    assert obs("baseline", "recall/discovery/same_review") == [1, 0, 1]
    assert obs("baseline", "recall/discovery/later_review") == [0, 1, 1]
    assert obs("baseline", "recall/discovery/post_merge") == [1, 0, 2]
    assert obs("variant-a", "recall/discovery/post_merge") == [1, 1, 2]
    assert obs("variant-a", "recall/discovery/revert") == [0, 0, 1]
    assert obs("baseline", "recall/risk/elevated") == [4, 8, 12]
    assert obs("baseline", "recall/risk/critical") == [1, 0, 9]
    assert obs("variant-a", "recall/risk/routine") == [0, 0, 2]
    assert obs("baseline", "precision/classification/blocked") == [3, 0, 4]
    assert obs("baseline", "precision/classification/nit") == [0, 2, 3]
    assert obs("variant-a", "precision/classification/non-blocked") == [1, 1, 2]
    nit = metric(report, "variant-a", "precision/classification/nit")
    assert nit["point"] is None and nit["undefined_draws"] == BOOTSTRAP_DRAWS


def test_one_cluster_resamples_to_its_point_and_is_never_usable(corpus, subs):
    report = score(corpus, subs)
    revert = contrast(report, "recall/discovery/revert")
    assert revert["clusters"] == 1 and revert["difference"] == "-1"
    assert revert["interval"] == ["-1", "-1"] and revert["undefined_draws"] == 0
    assert revert["label_bands_within"] and not revert["usable"]
    blocked = contrast(report, "precision/classification/blocked")
    assert blocked["clusters"] == 3 and blocked["label_bands_within"] and blocked["usable"]


def test_bootstrap_is_seeded_by_cluster_count_and_counts_undefined_draws(corpus, subs):
    assert multiplicities(3) == multiplicities(3)
    assert len(multiplicities(3)) == BOOTSTRAP_DRAWS
    assert all(sum(draw) == 3 for draw in multiplicities(3))
    first, second = score(corpus, subs), score(corpus, deepcopy(subs))
    assert first == second
    # Regression pins of the seeded draws (seed 356): the variant's FPR is
    # undefined whenever only the negative holding its unknown finding is drawn.
    other_fpr = metric(first, "variant-a", "false_positive_rate")
    assert other_fpr["clusters"] == 2 and other_fpr["undefined_draws"] == 2444
    assert metric(first, "baseline", "recall")["interval"] == ["1/9", "1"]
    assert contrast(first, "recall")["interval"] == ["-1", "7/9"]


def test_settled_labels_make_a_contrast_usable(corpus, subs):
    gamma = corpus["cases"]["gamma-revert"]["defects"]["d3"]
    gamma["labels"].append(label("adjudicator-2", "confirmed"))
    run_of(subs, "baseline", "alpha-defect")["findings"][2]["labels"] = [
        label("adjudicator-4", "false", match=True)
    ]
    run_of(subs, "variant-a", "beta-escape")["findings"][1]["labels"].append(
        label("adjudicator-5", "false", match=True)
    )
    run_of(subs, "variant-a", "gamma-clean")["findings"][0]["labels"] = [
        label("adjudicator-4", "false", match=True)
    ]
    report = scored(corpus, subs)
    assert metric(report, "baseline", "precision")["bounds"] == ["1/2", "1/2"]
    assert metric(report, "variant-a", "precision")["bounds"] == ["1/2", "1/2"]
    item = contrast(report, "precision")
    assert item["difference"] == "0" and item["undefined_draws"] == 0 and item["usable"]
    # The critical defect a second adjudicator settled now enters recall, and
    # with no unknown finding left the recall bounds close too.
    assert metric(report, "baseline", "recall")["observations"] == [15, 0, 31]


# -- coverage, exclusions and seeds -------------------------------------------


def test_a_missing_or_failed_run_leaves_the_contrast_incomplete(corpus, subs):
    subs["runs"].remove(run_of(subs, "variant-a", "beta-clean"))
    failed = run_of(subs, "baseline", "gamma-clean")
    failed.update(status="failed", findings=None)
    report = score(corpus, subs)
    assert report["variants"]["variant-a"]["runs"]["missing"] == 1
    assert report["variants"]["baseline"]["runs"]["failed"] == 1
    assert not report["variants"]["baseline"]["coverage_complete"]
    pairs = report["contrasts"]["variant-a"]["pairs"]
    assert pairs["paired"] == 4
    assert pairs["incomplete"] == [["beta-clean", 1], ["gamma-clean", 1]]
    assert not any(item["usable"] for item in report["contrasts"]["variant-a"]["metrics"].values())
    # A failed run is missing evidence, never zero findings.
    assert metric(report, "baseline", "false_positive_rate")["observations"] == [1, 0, 1]


def test_an_exclusion_removes_its_pair_without_breaking_coverage(corpus, subs):
    run = run_of(subs, "variant-a", "beta-clean")
    run.update(status="excluded", exclusion="service_unavailable", findings=None)
    report = score(corpus, subs)
    card = report["variants"]["variant-a"]
    assert card["runs"]["excluded"] == 1 and card["coverage_complete"]
    pairs = report["contrasts"]["variant-a"]["pairs"]
    assert pairs == {"paired": 5, "excluded": 1, "incomplete": []}
    # The excluded run's partner leaves the paired precision too.
    assert contrast(report, "precision")["baseline"] == "4/7"
    assert contrast(report, "precision/classification/blocked")["usable"]


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (
            lambda r: r.update(status="excluded", exclusion="model_was_wrong", findings=None),
            "documented exclusion",
        ),
        (lambda r: r.update(status="failed"), "only a completed run holds findings"),
        (lambda r: r.update(exclusion="infrastructure"), "only an excluded run"),
        (lambda r: r.update(seed=2), "planned seed"),
        (lambda r: r.update(cost_microusd=-1), "run cost"),
        (lambda r: r["findings"].append(dict(r["findings"][0])), "finding id"),
        (lambda r: r["findings"][0].update(classification="major"), "classification"),
    ],
)
def test_malformed_runs_are_refused(corpus, subs, change, rule):
    change(run_of(subs, "baseline", "alpha-defect"))
    with pytest.raises(CorpusError, match=rule):
        score(corpus, subs)


def test_seeds_pool_inside_their_cluster(corpus, subs):
    subs["plan"]["seeds"] = [1, 2]
    for run in [r for r in subs["runs"] if r["seed"] == 1]:
        subs["runs"].append(dict(deepcopy(run), seed=2))
    report = score(corpus, subs)
    recall = metric(report, "baseline", "recall")
    assert recall["observations"] == [14, 16, 46] and recall["point"] == "7/23"
    assert recall["clusters"] == 3
    assert report["contrasts"]["variant-a"]["pairs"]["paired"] == 12
    subs["runs"].append(deepcopy(subs["runs"][0]))
    with pytest.raises(CorpusError, match="one run per variant"):
        score(corpus, subs)


# -- labels and adjudication --------------------------------------------------


def test_a_curator_proposes_and_never_settles(corpus, subs):
    case = corpus["cases"]["gamma-clean"]
    case["negative"]["labels"] = case["negative"]["labels"][:1]
    alpha = corpus["cases"]["alpha-defect"]["defects"]["d1"]
    alpha["labels"] = alpha["labels"][:1]
    labels = describe(corpus)["labels"]
    assert labels["negatives"]["unlabeled"] == 1 and labels["negatives"]["settled"] == 1
    assert labels["defects"]["unknown"] == 3
    report = scored(corpus, subs)
    # The baseline's match to the now-unsettled defect is unknown, not confirmed.
    assert metric(report, "baseline", "precision")["observations"] == [3, 3, 10]


def test_a_critical_defect_needs_two_adjudicators_either_way(corpus):
    d3 = corpus["cases"]["gamma-revert"]["defects"]["d3"]
    assert describe(corpus)["labels"]["defects_by_severity"]["critical"]["unknown"] == 1
    d3["labels"] = [label("adjudicator-1", "rejected")]
    assert describe(corpus)["labels"]["defects_by_severity"]["critical"]["unknown"] == 1
    d3["labels"].append(label("adjudicator-3", "rejected"))
    assert describe(corpus)["labels"]["defects_by_severity"]["critical"]["rejected"] == 1


def test_a_disagreement_stays_unknown_until_an_arbiter_settles_it(corpus):
    d3 = corpus["cases"]["alpha-defect"]["defects"]["d3"]
    assert describe(corpus)["labels"]["defects_by_severity"]["medium"]["unknown"] == 1
    d3["labels"].append(label("arbiter-1", "confirmed", role="arbiter"))
    assert describe(corpus)["labels"]["defects_by_severity"]["medium"]["confirmed"] == 2
    d3["labels"].append(label("arbiter-2", "rejected", role="arbiter"))
    assert describe(corpus)["labels"]["defects_by_severity"]["medium"]["unknown"] == 1
    lone = corpus["cases"]["beta-escape"]["defects"]["d2"]
    lone["labels"].append(label("arbiter-1", "rejected", role="arbiter"))
    with pytest.raises(CorpusError, match="arbiter settles a disagreement only"):
        check_corpus(corpus)


@pytest.mark.parametrize(
    ("extra", "rule"),
    [
        (label("reviewer-alpha", "confirmed"), "an actor cannot adjudicate"),
        (label("adjudicator-1", "rejected"), "one label per identity"),
        (label("adjudicator-9", "confirmed", decided="2026-07-01"), "after the freeze"),
        (label("adjudicator-9", "approved"), "label verdict"),
        (label("adjudicator-9", "confirmed", role="reviewer"), "label verdict"),
        (dict(label("adjudicator-9", "confirmed"), artifact=" "), "label provenance"),
    ],
)
def test_defect_labels_are_refused(corpus, extra, rule):
    corpus["cases"]["alpha-defect"]["defects"]["d1"]["labels"].append(extra)
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


@pytest.mark.parametrize(
    ("labels", "rule"),
    [
        ([label("reviewer-variant-a", "false", match=True)], "an actor cannot adjudicate"),
        ([label("reviewer-alpha", "false", match=True)], "an actor cannot adjudicate"),
        ([label("adjudicator-4", "match", defect="d9", match=True)], "registered defect"),
        ([label("adjudicator-4", "false", defect="d1", match=True)], "only a match names"),
        ([label("adjudicator-4", "false", decided="2026-07-16", match=True)], "after the freeze"),
    ],
)
def test_match_labels_are_refused(corpus, subs, labels, rule):
    run_of(subs, "baseline", "alpha-defect")["findings"][0]["labels"] = labels
    with pytest.raises(CorpusError, match=rule):
        score(corpus, subs)


@pytest.mark.parametrize(
    "change",
    [
        # The baseline as the sole adjudicator of a defect.
        lambda c: c["cases"]["beta-escape"]["defects"]["d2"].update(
            labels=[label("reviewer-baseline", "confirmed")]
        ),
        lambda c: c["cases"]["alpha-defect"]["defects"]["d1"]["labels"].append(
            label("reviewer-variant-a", "confirmed")
        ),
        # An arbiter of a real disagreement.
        lambda c: c["cases"]["alpha-defect"]["defects"]["d3"]["labels"].append(
            label("reviewer-variant-a", "confirmed", role="arbiter")
        ),
        lambda c: c["cases"]["alpha-negative"]["negative"]["labels"].append(
            label("reviewer-baseline", "clean")
        ),
        # A curator only proposes, but has still seen the answers.
        lambda c: c["cases"]["beta-clean"]["negative"]["labels"].append(
            label("reviewer-variant-a", "clean", role="curator")
        ),
    ],
)
def test_a_reviewer_variant_cannot_label_the_corpus(corpus, subs, change):
    change(corpus)
    # The corpus alone cannot know who will be scored against it.
    check_corpus(corpus)
    with pytest.raises(CorpusError, match="a reviewer variant cannot label the corpus"):
        scored(corpus, subs)


def test_a_variant_cannot_label_even_a_case_it_is_not_planned_on(corpus, subs):
    corpus["cases"]["gamma-clean"]["negative"]["labels"].append(label("reviewer-baseline", "clean"))
    subs["plan"]["cases"].remove("gamma-clean")
    subs["runs"] = [run for run in subs["runs"] if run["case"] != "gamma-clean"]
    with pytest.raises(CorpusError, match="a reviewer variant cannot label the corpus"):
        scored(corpus, subs)


def test_a_match_to_a_critical_defect_needs_two_adjudicators(corpus, subs):
    finding = run_of(subs, "variant-a", "alpha-defect")["findings"][0]
    finding["labels"] = finding["labels"][:1]
    report = score(corpus, subs)
    # alpha's critical defect is now only possibly found: 4 of 12, up to 12.
    assert metric(report, "variant-a", "recall")["observations"] == [12, 9, 23]


# -- censoring and negatives --------------------------------------------------


def test_a_negative_settles_only_after_thirty_days(corpus, subs):
    case = corpus["cases"]["beta-clean"]
    case["review"]["cutoff"] = "2026-05-31T08:00:00Z"
    case["context"]["items"][0]["observed_at"] = "2026-05-30T10:00:00Z"
    case["exposure"]["possible_from"] = "2026-05-31"
    report = scored(corpus, deepcopy(subs))
    assert report["labels"]["negatives"]["settled"] == 3
    # beta-clean's baseline false alarm now counts against the FPR.
    assert metric(report, "baseline", "false_positive_rate")["observations"] == [2, 0, 3]
    case["review"]["cutoff"] = "2026-06-01T08:00:00Z"
    assert describe(corpus)["labels"]["negatives"]["censored"] == 1


def test_a_refuted_negative_leaves_the_fpr(corpus, subs):
    corpus["cases"]["alpha-negative"]["negative"]["labels"] = [label("adjudicator-2", "defective")]
    report = scored(corpus, subs)
    assert report["labels"]["negatives"]["refuted"] == 1
    assert metric(report, "baseline", "false_positive_rate")["observations"] == [0, 0, 1]


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (lambda c: c["negative"].update(observed_through="2026-07-01"), "observed between"),
        (lambda c: c["negative"].update(observed_through="2026-04-30"), "observed between"),
        (lambda c: c.update(defects={"d1": None}), "a negative case registers no defect"),
        (lambda c: c.update(negative=None), "fields: observed_through labels"),
    ],
)
def test_negative_cases_are_refused(corpus, change, rule):
    change(corpus["cases"]["alpha-negative"])
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


# -- cutoff and leakage -------------------------------------------------------


def withheld(corpus):
    return corpus["cases"]["alpha-defect"]["withheld"][0]


def set_text(item, text):
    item.update(text=text, sha256=sha256(text.encode()).hexdigest())


@pytest.mark.parametrize(
    ("text", "leaks"),
    [
        (lambda w: f"Fixed upstream in {w[:7]}.", True),
        (lambda w: f"See {w.upper()[:12]}.", True),
        (lambda w: f"digest {'0' * 3}{w}{'f' * 21}", True),
        (lambda w: f"An abbreviation inside a run: f{w[:9]}.", True),
        (lambda w: f"Six digits {w[:6]} name nothing.", False),
    ],
)
def test_context_naming_a_withheld_commit_is_refused(corpus, text, leaks):
    item = corpus["cases"]["alpha-defect"]["context"]["items"][0]
    set_text(item, text(withheld(corpus)))
    if leaks:
        with pytest.raises(CorpusError, match="names a withheld commit"):
            check_corpus(corpus)
    else:
        check_corpus(corpus)


@pytest.mark.parametrize("digits", [7, 12, 40])
@pytest.mark.parametrize(
    "place",
    [
        lambda c, w: items(c)[0].update(ref=f"synthetic issue alpha, as edited by {w}"),
        lambda c, w: items(c)[1].update(id=f"fix-{w}"),
        lambda c, w: items(c)[1].update(source=f"issue/1/comment/{w.lower()}"),
        lambda c, w: items(c).append(
            uncaptured("later", "issue_comment", f"the review of {w}", "issue/1/comment/2")
        ),
        lambda c, w: c["environment"]["validation"].append(f"git show {w}"),
        lambda c, w: c["environment"]["files"].update({f"pins/{w}.txt": "1" * 40}),
        lambda c, w: c["review"].update(base_ref=f"refs/heads/{w}"),
    ],
)
def test_every_reviewer_visible_field_is_held_to_the_leakage_rule(corpus, place, digits):
    case = corpus["cases"]["alpha-defect"]
    place(case, withheld(corpus)[:digits])
    case["context"]["inventory"]["sources"] = [item["source"] for item in items(case)]
    with pytest.raises(CorpusError, match="the reviewer's input names a withheld commit"):
        check_corpus(corpus)


def test_an_uncaptured_reference_is_held_to_the_leakage_rule():
    seed = read_json(SEED)
    case = seed["cases"]["pr539-round1"]
    fix = case["defects"]["r1-f1-reply-text"]["discovery"]["fix"]
    items(case)[1]["ref"] = f"#539 as of {fix[:7]}"
    with pytest.raises(CorpusError, match="names a withheld commit"):
        check_corpus(seed)


# -- the context inventory ----------------------------------------------------


def complete(case, taken="2026-06-28T00:00:00Z"):
    case["context"]["inventory"] = {
        "status": "complete",
        "taken": taken,
        "artifact": "test: the listing as of the cutoff",
        "sources": [item["source"] for item in items(case) if item["source"] is not None],
    }


def never_taken(case):
    case["context"]["inventory"] = {
        "status": "not_captured",
        "taken": None,
        "artifact": None,
        "sources": None,
    }


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        # The seed's records deleted, the inventory left uncaptured.
        (lambda c: c["context"].update(items=[]), "the context holds the case's issue"),
        (lambda c: items(c).pop(1), "the context holds the case's pr_body"),
        (lambda c: items(c).pop(0), "the context holds the case's issue"),
        # ...or the inventory declared complete over nothing.
        (
            lambda c: (c["context"].update(items=[]), complete(c, "2026-11-27T00:00:00Z")),
            "the context holds the case's issue",
        ),
        # A second issue, whether a copy of the first or another source.
        (lambda c: items(c).append(dict(items(c)[0], id="again")), "context source repeated"),
        (lambda c: items(c).append(dict(items(c)[0], id="again", source=None)), "one issue"),
        (
            lambda c: items(c).append(dict(items(c)[0], id="again", source="issue/330")),
            "a context source is on the case's issue",
        ),
        (
            lambda c: c["context"]["inventory"].update(artifact="a listing"),
            "an uncaptured inventory holds nothing",
        ),
    ],
)
def test_omitting_required_context_never_makes_a_seed_case_runnable(change, rule):
    seed = read_json(SEED)
    change(seed["cases"]["pr539-round1"])
    with pytest.raises(CorpusError, match=rule):
        check_corpus(seed)


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (lambda c: c["context"].update(items=[]), "the context holds the case's issue"),
        # A comment the inventory lists is left out.
        (lambda c: items(c).pop(1), "the inventory lists exactly the context's sources"),
        # A comment the inventory does not list is added, named or not.
        (
            lambda c: items(c).append(uncaptured("comment-2", "issue_comment", "comment 2")),
            "the inventory lists exactly the context's sources",
        ),
        (
            lambda c: items(c).append(
                uncaptured("comment-2", "issue_comment", "comment 2", "issue/1/comment/2")
            ),
            "the inventory lists exactly the context's sources",
        ),
        (lambda c: complete(c, "2026-05-01T11:59:59Z"), "inventory taken between"),
        (lambda c: complete(c, "2026-07-01T00:00:00Z"), "inventory taken between"),
        (lambda c: c["context"]["inventory"].update(artifact=" "), "inventory artifact"),
        (lambda c: c["context"]["inventory"].update(sources="issue/1"), "inventory sources"),
        (lambda c: c["context"]["inventory"]["sources"].append("issue/1"), "source repeated"),
        (
            lambda c: c["context"]["inventory"]["sources"].append("issue/1/comment/2"),
            "the inventory lists exactly the context's sources",
        ),
        (lambda c: c["context"]["inventory"]["sources"].append("ticket/1"), "context source"),
        (lambda c: c["context"]["inventory"].pop("sources"), "fields: status"),
        (lambda c: c["context"]["inventory"].update(status="partial"), "inventory status"),
        (lambda c: c.update(context=items(c)), "fields: inventory items"),
    ],
)
def test_the_context_inventory_is_refused(corpus, change, rule):
    change(corpus["cases"]["alpha-defect"])
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        # Another copy of the comment, under a local id of its own, in place
        # of a comment the listing names: the count is the same, the source
        # is not.
        (
            lambda c: items(c).append(dict(items(c)[1], id="comment-2")),
            "context source repeated",
        ),
        # The same copy given a source the listing does not hold.
        (
            lambda c: items(c).append(
                dict(items(c)[1], id="comment-2", source="issue/1/comment/2")
            ),
            "the inventory lists exactly the context's sources",
        ),
        # A source of another kind, of another issue, of the PR the case has
        # not, or none for a captured item.
        (lambda c: items(c)[1].update(source="issue/1"), "kind is its source's"),
        (lambda c: items(c)[1].update(source="issue/2/comment/1"), "on the case's issue"),
        (lambda c: items(c)[1].update(kind="pr_comment", source="pr/1/comment/1"), "case's pr"),
        (lambda c: items(c)[1].update(source=None), "a captured context names its source"),
        (lambda c: items(c)[1].update(source="issue/01/comment/1"), "context source"),
    ],
)
def test_each_context_item_is_bound_to_its_listed_source(corpus, change, rule):
    case = corpus["cases"]["alpha-defect"]
    complete(case)
    sources = list(case["context"]["inventory"]["sources"])
    change(case)
    # The listing stands as it was taken, whatever the items now hold.
    case["context"]["inventory"]["sources"] = sources
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


def test_a_case_runs_only_on_a_complete_and_captured_inventory(corpus, subs):
    assert describe(corpus)["runnable"] == sorted(corpus["cases"])
    # An explicitly uncaptured item leaves a complete inventory unrunnable.
    comment = items(corpus["cases"]["alpha-defect"])[1]
    comment.update(status="not_captured", observed_at=None, text=None, sha256=None)
    # An inventory never taken leaves captured items unrunnable.
    never_taken(corpus["cases"]["beta-clean"])
    assert describe(corpus)["runnable"] == [
        "alpha-negative",
        "beta-escape",
        "gamma-clean",
        "gamma-revert",
    ]
    for case_id in ("alpha-defect", "beta-clean"):
        planned = deepcopy(subs)
        planned["plan"]["cases"] = [case_id]
        planned["runs"] = [run for run in planned["runs"] if run["case"] == case_id]
        with pytest.raises(CorpusError, match="a planned case is runnable"):
            scored(corpus, planned)


def test_a_seed_inventory_declared_complete_runs_nothing_uncaptured():
    seed = read_json(SEED)
    for case_id, case in seed["cases"].items():
        if case_id != "issue262-round1":
            complete(case, "2026-11-27T00:00:00Z")
    assert describe(seed)["runnable"] == []
    # No commit states the PR of issue 262's branch, so its body has no
    # source and no listing can name it.
    complete(seed["cases"]["issue262-round1"], "2026-11-27T00:00:00Z")
    with pytest.raises(CorpusError, match="the inventory lists exactly"):
        check_corpus(seed)


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (lambda c: items(c)[1].update(observed_at="2026-05-01T12:00:01Z"), "after the cutoff"),
        (lambda c: items(c)[0].update(sha256="0" * 64), "context digest"),
        (lambda c: items(c)[0].update(status="not_captured"), "uncaptured context holds"),
        (lambda c: items(c).append(dict(items(c)[0])), "context id repeated"),
        (lambda c: c["defects"]["d1"]["discovery"].update(fix="9" * 40), "withheld from"),
        (lambda c: c["withheld"].append(c["review"]["head"]), "candidate is not withheld"),
        (
            lambda c: c["defects"]["d1"].update(introduced=c["withheld"][0]),
            "withheld commit cannot introduce",
        ),
        (
            lambda c: c["defects"]["d1"].update(introduced=c["review"]["merge_base"]),
            "above the merge base",
        ),
        (
            lambda c: c["defects"]["d1"]["discovery"].update(discovered="2026-04-30"),
            "discovered between cutoff and freeze",
        ),
        (
            lambda c: c["defects"]["d1"]["discovery"].update(discovered="2026-07-01"),
            "discovered between cutoff and freeze",
        ),
        (lambda c: c["defects"]["d1"]["discovery"].update(round=2), "discovery round"),
        (lambda c: c["defects"]["d2"]["discovery"].update(round=1), "discovery round"),
        (lambda c: c["defects"]["d1"]["location"][0].update(path="../x"), "repository path"),
        (lambda c: c["defects"]["d1"]["location"][0].update(lines=[3, 2]), "defect lines"),
        (lambda c: c["defects"]["d1"].update(location=[]), "defect location"),
        (lambda c: c["review"].update(cutoff="2026-05-01 12:00:00"), "instant shape"),
        (lambda c: c.update(stratum="escape"), "a defect of the case's stratum"),
        (lambda c: c.update(defects={}), "a defect case registers its defects"),
        (lambda c: c.update(extra=1), "fields: stratum"),
    ],
)
def test_corpus_cases_are_refused(corpus, change, rule):
    change(corpus["cases"]["alpha-defect"])
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


def test_a_run_must_review_the_frozen_input(corpus, subs):
    run_of(subs, "variant-a", "alpha-defect")["input"] = "0" * 64
    with pytest.raises(CorpusError, match="frozen input"):
        score(corpus, subs)
    subs = json.loads(SYNTHETIC_SUBMISSIONS.read_bytes())
    corpus["cases"]["alpha-defect"]["environment"]["validation"].append("pytest -x")
    subs["corpus"] = corpus_digest(corpus)
    with pytest.raises(CorpusError, match="frozen input"):
        score(corpus, subs)


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        # A pin left out, every pin left out, or a file the repository does
        # not declare.
        (
            lambda r, c: c["environment"]["files"].pop("uv.lock"),
            "a case pins each of the repository's environment files",
        ),
        (
            lambda r, c: c["environment"].update(files={}),
            "a case pins each of the repository's environment files",
        ),
        (
            lambda r, c: c["environment"]["files"].update({"Makefile": "1" * 40}),
            "a case pins each of the repository's environment files",
        ),
        (lambda r, c: c["environment"]["files"].update({"uv.lock": "soon"}), "object id shape"),
        # No command to validate with.
        (lambda r, c: c["environment"].update(validation=[]), "environment validation"),
        (lambda r, c: c["environment"].update(validation=[" "]), "environment validation"),
        # The repository's declaration itself.
        (lambda r, c: r.update(environment=[]), "repository environment"),
        (
            lambda r, c: r.update(environment=["uv.lock", "uv.lock"]),
            "repository environment file repeated",
        ),
        (lambda r, c: r.update(environment=["../uv.lock"]), "repository path"),
        (lambda r, c: r.pop("environment"), "fields: slug root refs environment"),
    ],
)
def test_a_case_pins_its_test_environment(corpus, change, rule):
    change(corpus["repository"], corpus["cases"]["alpha-defect"])
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


def test_a_run_is_credited_only_to_the_pins_it_was_launched_with(corpus, subs):
    variant = subs["variants"]["variant-a"]
    variant["model"] = "synthetic-model-c"
    variant["prompt"]["sha256"] = "0" * 64
    # Every run left as recorded, under the earlier model and prompt.
    with pytest.raises(CorpusError, match="a run was launched with its variant's pins"):
        score(corpus, subs)
    # Recorded under the new pins they score, and the card names those pins.
    report = score(corpus, repin(subs))
    assert report["variants"]["variant-a"]["pins_sha256"] == pins_digest(variant)
    # A run of one variant never carries another's pins.
    run_of(subs, "variant-a", "alpha-defect")["pins_sha256"] = pins_digest(
        subs["variants"]["baseline"]
    )
    with pytest.raises(CorpusError, match="a run was launched with its variant's pins"):
        score(corpus, subs)


def test_the_input_holds_no_answer(corpus):
    case = corpus["cases"]["alpha-defect"]
    before = input_digest(case)
    case["defects"]["d1"]["summary"] = "something else"
    case["withheld"].append("8" * 40)
    case["risk"]["rationale"] = "other"
    assert input_digest(case) == before


def exposed(corpus, subs, knowledge_cutoff):
    subs["variants"]["baseline"]["knowledge_cutoff"] = knowledge_cutoff
    exposure = score(corpus, repin(subs))["variants"]["baseline"]["training_exposure"]
    return exposure["none"], exposure["possible"], exposure["unknown"]


def test_training_exposure_is_none_only_with_evidence(corpus, subs):
    report = score(corpus, subs)
    # The baseline's knowledge ends before any candidate existed.
    assert report["variants"]["baseline"]["training_exposure"] == {
        "none": 6,
        "possible": 0,
        "unknown": 0,
    }
    # No stated cutoff is no evidence of absence, for a negative as for a defect.
    assert report["variants"]["variant-a"]["training_exposure"] == {
        "none": 0,
        "possible": 0,
        "unknown": 6,
    }
    # gamma-revert's earliest discovery is 2026-04-02, so a cutoff that day may
    # hold it; gamma-clean existed by then, and a negative's answer is its later
    # history.
    assert exposed(corpus, subs, "2026-04-02") == (4, 2, 0)
    # beta-escape's candidate is from 2026-05-10 and its first discovery day is
    # 2026-06-01. That day bounds the first report from above only: an earlier
    # disclosure cannot be ruled out in between, so exposure is unknown, not none.
    assert exposed(corpus, subs, "2026-05-20") == (1, 4, 1)
    # The day before beta-escape's candidate existed, nothing of it could be.
    assert exposed(corpus, subs, "2026-05-09") == (2, 4, 0)


def test_a_defect_disclosed_at_an_earlier_revision_is_never_unexposed(corpus, subs):
    # beta-escape's head is from 2026-05-10, but it carries code an earlier
    # revision made public by 2026-04-20, where its defect was disclosed on
    # 2026-05-01, before this candidate existed.
    corpus["cases"]["beta-escape"]["exposure"] |= {
        "possible_from": "2026-04-20",
        "public_by": "2026-05-01",
    }
    rebind(corpus, subs)
    # A knowledge cutoff before the candidate, after the disclosure, may hold
    # it: beta-escape is possible, where its newer head alone would say none.
    assert exposed(corpus, subs, "2026-05-05") == (1, 5, 0)
    # Between the code becoming public and its disclosure nothing rules it out
    # (the alpha and beta-clean candidates are later, the gamma ones exposed).
    assert exposed(corpus, subs, "2026-04-25") == (3, 2, 1)
    # Before the code was public, nothing about it could be.
    assert exposed(corpus, subs, "2026-04-19") == (4, 2, 0)
    # A disclosure before the cutoff is exposure evidence, never a discovery.
    discovery = corpus["cases"]["beta-escape"]["defects"]["d1"]["discovery"]
    discovery["discovered"] = "2026-05-01"
    with pytest.raises(CorpusError, match="discovered between cutoff and freeze"):
        check_corpus(corpus)


def test_without_exposure_evidence_nothing_is_unexposed(corpus, subs):
    for case in corpus["cases"].values():
        case["exposure"]["possible_from"] = None
    rebind(corpus, subs)
    # Every candidate is newer than the baseline's knowledge, but no evidence
    # says its code, or a report of it, was not public before.
    assert exposed(corpus, subs, "2026-01-01") == (0, 0, 6)


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        # A day after the candidate existed cannot rule exposure out.
        (lambda e: e.update(possible_from="2026-05-11"), "exposure possible by the cutoff"),
        (
            lambda e: e.update(possible_from="2026-05-02", public_by="2026-05-01"),
            "exposure possible before it was public",
        ),
        (lambda e: e.update(public_by="2026-07-01"), "exposure public by the freeze"),
        (lambda e: e.update(artifact=" "), "exposure artifact"),
        (lambda e: e.update(possible_from="soon"), "date shape"),
        (lambda e: e.pop("public_by"), "fields: possible_from"),
    ],
)
def test_exposure_evidence_is_refused(corpus, change, rule):
    change(corpus["cases"]["beta-escape"]["exposure"])
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


# -- clusters -----------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        # A second case of issue 1 counted as its own cluster.
        lambda c: c["cases"]["alpha-negative"].update(cluster="delta"),
        # Two cases registering a defect of one commit.
        lambda c: c["cases"]["beta-escape"]["defects"]["d2"].update(
            introduced=c["cases"]["alpha-defect"]["review"]["head"]
        ),
    ],
)
def test_one_issue_or_defect_never_spans_two_clusters(corpus, change):
    change(corpus)
    with pytest.raises(CorpusError, match="cases of one issue, PR or defect share a cluster"):
        check_corpus(corpus)


def test_two_rounds_of_one_pr_are_one_cluster():
    seed = read_json(SEED)
    # Relabeling round 2 alone would turn PR 539's rounds into two samples.
    seed["cases"]["pr539-round2"]["cluster"] = "issue-329-round2"
    with pytest.raises(CorpusError, match="cases of one issue, PR or defect share a cluster"):
        check_corpus(seed)
    seed = read_json(SEED)
    # A squash of another PR of the same issue stays with its rounds too.
    seed["cases"]["pr540-squash"]["cluster"] = "pr540"
    with pytest.raises(CorpusError, match="cases of one issue, PR or defect share a cluster"):
        check_corpus(seed)


def test_related_issues_may_share_a_cluster(corpus, subs):
    # beta's cluster already holds issues 2 and 3; gamma's two issues may join it.
    for case_id in ("gamma-revert", "gamma-clean"):
        corpus["cases"][case_id]["cluster"] = "beta"
    assert describe(corpus)["clusters"] == 2
    report = scored(corpus, subs)
    assert report["plan"]["clusters"] == 2
    assert contrast(report, "precision/classification/blocked")["clusters"] == 2


# -- files, formats and modes -------------------------------------------------


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (lambda s: s.update(corpus="0" * 64), "this exact corpus"),
        (lambda s: s.update(mode="recorded"), "submissions mode for corpus"),
        (lambda s: s.update(version=2), "submissions version"),
        (lambda s: s.update(baseline="nobody"), "baseline is a registered variant"),
        (lambda s: s["plan"]["cases"].append("alpha-defect"), "planned case repeated"),
        (lambda s: s["plan"]["cases"].append("unknown-case"), "planned case registered"),
        (lambda s: s["variants"]["variant-a"].update(identity="reviewer-baseline"), "identity"),
        (lambda s: s["variants"]["variant-a"].pop("model"), "fields: identity"),
        (lambda s: s["variants"]["variant-a"].update(knowledge_cutoff="soon"), "date shape"),
        (lambda s: s["variants"].pop("variant-a"), "a baseline and a variant"),
    ],
)
def test_submissions_are_refused(corpus, subs, change, rule):
    change(subs)
    with pytest.raises(CorpusError, match=rule):
        score(corpus, subs)


def test_planned_work_beyond_its_limits_is_refused_before_it_expands(
    corpus, subs, tmp_path, monkeypatch
):
    checked = check_corpus(corpus)
    base = deepcopy(subs)
    # Six cases and a million seeds, no runs: under the file-size cap, and six
    # million planned pairs if expanded.
    subs["plan"]["seeds"] = list(range(1, 1_000_001))
    subs["runs"] = []
    body = json.dumps(subs, separators=(",", ":"))
    assert len(body.encode()) < MAX_FILE_BYTES
    path = tmp_path / "subs.json"
    path.write_text(body)
    with pytest.raises(SystemExit, match="2"):
        main(["score", str(SYNTHETIC_CORPUS), str(path)])
    with pytest.raises(CorpusError, match="the planned work is within its limits"):
        check_submissions(checked, subs)
    # The pair limit is the product of cases and seeds.
    subs["plan"]["seeds"] = list(range(1, MAX_PLANNED_PAIRS // 6 + 1))
    check_submissions(checked, subs)
    subs["plan"]["seeds"].append(MAX_PLANNED_PAIRS)
    with pytest.raises(CorpusError, match="the planned work is within its limits"):
        check_submissions(checked, subs)
    # Every variant is scored over the whole plan.
    subs = deepcopy(base)
    for n in range(MAX_VARIANTS - 2):
        subs["variants"][f"extra-{n}"] = dict(
            deepcopy(subs["variants"]["variant-a"]), identity=f"reviewer-extra-{n}"
        )
    check_submissions(checked, subs)
    subs["variants"]["one-more"] = dict(
        deepcopy(subs["variants"]["variant-a"]), identity="reviewer-one-more"
    )
    with pytest.raises(CorpusError, match="the planned work is within its limits"):
        check_submissions(checked, subs)
    # The bootstrap's tables grow with the planned clusters, and so many of
    # them are kept at once.
    assert multiplicities.cache_info().maxsize == BOOTSTRAP_TABLES
    monkeypatch.setattr("tests.review_corpus.MAX_PLANNED_CLUSTERS", 2)
    with pytest.raises(CorpusError, match="the planned work is within its limits"):
        check_submissions(checked, deepcopy(base))


def test_no_run_is_scored_by_scanning_its_cases_defects(corpus, subs, monkeypatch):
    """A plan within the byte and plan caps that once cost a scan of a case's
    every defect for each run and each of its 17 metrics: one cluster, 12,000
    registered defects and 20,000 completed runs. Each run's observations are
    computed once, from its findings and its case's tally, counted once."""
    case = corpus["cases"]["alpha-defect"]
    copy = deepcopy(case["defects"]["d3"])
    copy["labels"] = [label("adjudicator-1", "confirmed")]
    for n in range(12_000):
        case["defects"][f"x{n}"] = deepcopy(copy)
    seeds = list(range(1, MAX_PLANNED_PAIRS + 1))
    subs.update(corpus=corpus_digest(corpus), plan={"cases": ["alpha-defect"], "seeds": seeds})
    subs["runs"] = [
        {
            "variant": variant,
            "case": "alpha-defect",
            "seed": seed,
            "input": input_digest(case),
            "pins_sha256": pins_digest(subs["variants"][variant]),
            "status": "completed",
            "exclusion": None,
            "cost_microusd": 0,
            "findings": [],
        }
        for variant in sorted(subs["variants"])
        for seed in seeds
    ]
    for value in (corpus, subs):
        assert len(json.dumps(value, separators=(",", ":")).encode()) <= MAX_FILE_BYTES
    calls: Counter = Counter()
    counts, tally = review_corpus.run_counts, review_corpus.Tally.of

    def counted(name, real):
        def call(*args):
            calls[name] += 1
            return real(*args)

        return call

    monkeypatch.setattr(review_corpus, "run_counts", counted("run", counts))
    monkeypatch.setattr(review_corpus.Tally, "of", counted("tally", tally))
    report = score(corpus, subs)
    assert report["plan"] == {"cases": 1, "seeds": seeds, "clusters": 1}
    assert calls == {"run": 20_000, "tally": 1}
    # d1 (high), d2 (critical) and the 12,000 copies (medium) are confirmed.
    weight = 4 + 8 + 2 * 12_000
    assert metric(report, "baseline", "recall")["observations"] == [0, 0, weight * 10_000]
    assert contrast(report, "recall")["clusters"] == 1


def test_the_corpus_digest_ignores_layout_but_not_content(corpus):
    assert corpus_digest(json.loads(json.dumps(corpus, indent=None))) == corpus_digest(corpus)
    corpus["frozen"] = "2026-06-29"
    assert corpus_digest(corpus) != corpus_digest(json.loads(SYNTHETIC_CORPUS.read_bytes()))


@pytest.mark.parametrize(
    "body",
    ['{"a": 1, "a": 2}', '{"a": NaN}', "[1,", b"\xff"],
)
def test_the_command_line_refuses_bad_files_without_echoing_them(tmp_path, capsys, body):
    path = tmp_path / "corpus.json"
    path.write_bytes(body if isinstance(body, bytes) else body.encode())
    with pytest.raises(SystemExit, match="2"):
        main(["describe", str(path)])
    out = capsys.readouterr()
    assert not out.out and out.err == "Invalid review corpus or submissions.\n"


def test_files_are_read_only_as_regular_files(tmp_path):
    link = tmp_path / "corpus.json"
    link.symlink_to(SYNTHETIC_CORPUS)
    with pytest.raises(OSError):
        read_json(link)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(CorpusError, match="regular file"):
        read_json(fifo)


def test_the_command_line_scores_and_describes(capsys):
    assert main(["score"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["contrasts"]["variant-a"]["metrics"]["recall"]["difference"] == "13/23"
    assert main(["describe"]) == 0
    assert json.loads(capsys.readouterr().out)["corpus"]["name"] == "autoforge-seed"


def test_the_document_pins_the_fixture_bytes():
    text = DOC.read_text(encoding="utf-8")
    for path in (SEED, SYNTHETIC_CORPUS, SYNTHETIC_SUBMISSIONS):
        assert sha256(path.read_bytes()).hexdigest() in text, path.name


# -- the seed -----------------------------------------------------------------


def test_the_seed_is_historical_pinned_and_wholly_unadjudicated():
    seed = read_json(SEED)
    report = describe(seed)
    assert report["corpus"]["mode"] == "historical" and report["corpus"]["release"] == 1
    assert report["cases"] == 8 and report["clusters"] == 3
    assert report["strata"] == {"hard_negative": 2, "reviewed_defect": 6}
    labels = report["labels"]
    assert labels["defects"] == {"confirmed": 0, "rejected": 0, "unknown": 20}
    assert labels["defects_by_discovery"]["same_review"]["unknown"] == 12
    assert labels["defects_by_discovery"]["later_review"]["unknown"] == 8
    assert labels["negatives"] == {"settled": 0, "censored": 0, "unlabeled": 2, "refuted": 0}
    # No context is captured yet, so no reviewer can be run on any case.
    assert report["runnable"] == []
    for case in seed["cases"].values():
        assert case["context"]["inventory"]["status"] == "not_captured"
        assert all(item["status"] == "not_captured" for item in case["context"]["items"])
        # No review record evidences a later boundary or the base revision a
        # review read, no merge record a squash's PR, no lineage record a
        # fix, and no exposure evidence rules any day out.
        assert case["record"]["status"] == "not_captured"
        assert case["exposure"]["possible_from"] is None
        for defect in case["defects"].values():
            assert {item["role"] for item in defect["labels"]} == {"curator"}
            assert defect["discovery"]["lineage"]["status"] == "not_captured"


def test_the_seed_names_no_person():
    text = SEED.read_text(encoding="utf-8")
    assert "@" not in text


def test_the_seed_keeps_two_defects_a_fix_introduced():
    cases = read_json(SEED)["cases"]
    round1, round2, round3 = (cases[f"pr539-round{n}"]["review"]["head"] for n in (1, 2, 3))
    later = cases["pr539-round4"]["defects"]["r4-f2-k13-inventory"]
    assert cases["pr539-round2"]["defects"]["r2-f1-workdir"]["introduced"] == round2
    assert later["introduced"] == round3
    assert "r2-f1-workdir" not in cases["pr539-round1"]["defects"]
    assert cases["pr539-round1"]["defects"]["r4-f1-probe-shape"]["introduced"] == round1


def test_no_submission_may_plan_an_uncaptured_case(subs):
    seed = read_json(SEED)
    subs.update(mode="recorded", corpus=corpus_digest(seed), runs=[])
    subs["plan"]["cases"] = ["pr539-round1"]
    with pytest.raises(CorpusError, match="a planned case is runnable"):
        check_submissions(check_corpus(seed), subs)
    # Nor is a historical corpus scored before it verifies.
    with pytest.raises(CorpusError, match="a recorded score verifies its corpus"):
        score(seed, subs)


# -- verify against a git object store ----------------------------------------


def epoch(when: str) -> int:
    return int(datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())


class Repo:
    """A throwaway repository whose commits carry fixed dates."""

    def __init__(self, path: Path):
        self.path = path
        self.env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "corpus test",
            "GIT_AUTHOR_EMAIL": "corpus@example.invalid",
            "GIT_COMMITTER_NAME": "corpus test",
            "GIT_COMMITTER_EMAIL": "corpus@example.invalid",
        }
        path.mkdir(parents=True)
        self.git("init", "-q")

    def git(self, *args: str, input: bytes | None = None, when: str | None = None) -> str:
        env = dict(self.env)
        if when is not None:
            stamp = f"{epoch(when)} +0000"
            env |= {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        done = subprocess.run(
            ["git", "-C", str(self.path), *NO_AUTO_MAINTENANCE, *args],
            env=env,
            input=input,
            capture_output=True,
            check=True,
        )
        return done.stdout.decode().strip()

    def commit(
        self, files: dict[str, bytes], parents: list[str], when: str, message: str = "commit"
    ) -> str:
        self.git("read-tree", "--empty")
        for path, body in files.items():
            blob = self.git("hash-object", "-w", "--stdin", input=body)
            self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        tree = self.git("write-tree")
        parent_args = [arg for parent in parents for arg in ("-p", parent)]
        return self.git("commit-tree", tree, *parent_args, "-m", message, when=when)


def unrecorded():
    return {"status": "not_captured", "source": None, "started": None, "text": None, "sha256": None}


def unlineaged():
    return {"status": "not_captured", "source": None, "text": None, "sha256": None}


def lineage_binding(case, defect, **change):
    """The binding statement of ``defect``'s lineage record, as one line of its text."""
    found = defect["discovery"]
    statement = {
        "pr": case["provenance"]["pr"],
        "introduced": defect["introduced"],
        "paths": sorted({place["path"] for place in defect["location"]}),
        "merged": found["merged"],
        "fix": found["fix"],
    } | change
    return "lineage-record: " + json.dumps(statement)


def lineaged(case, key, text=None, source=None, **change):
    """``key``'s lineage record, retained apart from the corpus, as its defect
    stands: the evidence that its fix, and any merge it escaped through, are
    what they are named as."""
    defect = case["defects"][key]
    text = text or f"{lineage_binding(case, defect, **change)}\nThe fix of {key}, as recorded."
    defect["discovery"]["lineage"] = {
        "status": "captured",
        "source": source or f"document/lineage/{key}.md",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
    }
    return case


def merge_record(case, text=None, source="document/merges/pr-2.json", **change):
    """A squash candidate's merge record: the commit its PR merged as."""
    statement = {
        "pr": case["provenance"]["pr"],
        "base_ref": case["review"]["base_ref"],
        "merged": case["review"]["head"],
    } | change
    text = text or f"merge-record: {json.dumps(statement)}\nPR merged."
    case["record"] = {
        "status": "captured",
        "source": source,
        "started": None,
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
    }
    return case


@pytest.fixture
def history(tmp_path):
    repo = Repo(tmp_path / "repo")
    env = {"pyproject.toml": b"[project]\n", "uv.lock": b"version = 1\n"}
    root = repo.commit({"README.md": b"root\n"}, [], "2026-01-01T00:00:00Z")
    base = repo.commit({"README.md": b"root\n", **env}, [root], "2026-01-02T00:00:00Z")
    code = b"".join(b"line %d\n" % n for n in range(1, 6))
    head = repo.commit({**env, "src/a.py": code}, [base], "2026-01-03T00:00:00Z")
    fixed = repo.commit({**env, "src/a.py": code + b"fixed\n"}, [head], "2026-01-05T00:00:00Z")
    # main: root, base, the branch's squash, then a fix of the squash; off it,
    # a commit on base that the squash never reached.
    squash = repo.commit(
        {**env, "src/a.py": code + b"fixed\n"}, [base], "2026-01-06T00:00:00Z", "Add a.py (#2)"
    )
    after = repo.commit(
        {**env, "src/a.py": b"LINE 1\n" + code[7:]}, [squash], "2026-01-07T00:00:00Z"
    )
    other = repo.commit({**env, "src/b.py": b"other\n"}, [base], "2026-01-08T00:00:00Z")
    ids = {
        "root": root,
        "base": base,
        "head": head,
        "fix": fixed,
        "squash": squash,
        "after": after,
        "other": other,
    }
    return repo, ids


def historical(repo: Repo, ids) -> dict:
    def pinned(head, base, cutoff):
        return {
            "base_ref": "main",
            "base_tip": base,
            "merge_base": base,
            "head": head,
            "tree": repo.git("rev-parse", f"{head}^{{tree}}"),
            "cutoff": cutoff,
        }, {
            "files": {
                p: repo.git("rev-parse", f"{head}:{p}") for p in ("pyproject.toml", "uv.lock")
            },
            "validation": ["make check"],
        }

    def case(stratum, head, source, rnd, withheld, cutoff, defects, negative):
        review, environment = pinned(head, ids["base"], cutoff)
        return {
            "stratum": stratum,
            "cluster": "one",
            "provenance": {
                "source": source,
                "issue": 1,
                "pr": 2,
                "round": rnd,
                "selection": "test",
                "note": "",
            },
            "review": review,
            "environment": environment,
            "context": {
                "inventory": {
                    "status": "not_captured",
                    "taken": None,
                    "artifact": None,
                    "sources": None,
                },
                "items": [
                    uncaptured("issue", "issue", "#1", "issue/1"),
                    uncaptured("pr-body", "pr_body", "#2", "pr/2"),
                ],
            },
            "record": unrecorded(),
            "actors": ["producer"],
            "risk": {"stratum": "routine", "rationale": "test"},
            "exposure": {"possible_from": None, "public_by": None, "artifact": "test"},
            "defects": defects,
            "negative": negative,
            "withheld": withheld,
        }

    defect = {
        "severity": "low",
        "class": "correctness",
        "introduced": ids["head"],
        "summary": "an unfixed line",
        "location": [{"path": "src/a.py", "lines": [2, 5]}],
        "discovery": {
            "source": "same_review",
            "round": 1,
            "finding": "R1-F1",
            "discovered": "2026-01-04",
            "fix": ids["fix"],
            "merged": None,
            "lineage": unlineaged(),
        },
        "labels": [],
    }
    return {
        "format": "autoforge-review-corpus",
        "version": 1,
        "mode": "historical",
        "name": "test",
        "release": 1,
        "frozen": "2026-02-01",
        "repository": {
            "slug": "example/test",
            "root": ids["root"],
            "refs": {"main": ids["after"]},
            "environment": ["pyproject.toml", "uv.lock"],
        },
        "cases": {
            "round1": case(
                "reviewed_defect",
                ids["head"],
                "pr_branch",
                1,
                [ids["fix"], ids["squash"]],
                "2026-01-03T00:00:00Z",
                {"d1": defect},
                None,
            ),
            "merged": case(
                "hard_negative",
                ids["squash"],
                "squash",
                None,
                [],
                "2026-01-06T00:00:00Z",
                {},
                {"observed_through": "2026-02-01", "labels": []},
            ),
        },
    }


def test_verify_rederives_every_pin_from_git(history):
    repo, ids = history
    report = verify(historical(repo, ids), repo.path)
    assert report["ok"], report
    assert set(report["cases"]) == {"round1", "merged"}


def test_verify_ignores_the_operators_git_variables(history, monkeypatch, tmp_path):
    repo, ids = history
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/other/")
    assert verify(historical(repo, ids), repo.path)["ok"]


def test_verify_reads_no_replacement_object(history):
    repo, ids = history
    corpus = historical(repo, ids)
    other = repo.commit({"x": b"x\n"}, [ids["base"]], "2026-01-03T00:00:00Z")
    repo.git("replace", ids["head"], other)
    assert verify(corpus, repo.path)["ok"]


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (lambda c, i: c["review"].update(tree=i["root"]), "head tree"),
        (lambda c, i: c["review"].update(cutoff="2026-01-02T23:59:59Z"), "committer time"),
        (lambda c, i: c["environment"]["files"].update({"uv.lock": i["root"]}), "environment"),
        (lambda c, i: c["withheld"].append(i["root"]), "is not in head"),
        (lambda c, i: c["withheld"].append(i["root"]), "is not in the base"),
        (lambda c, i: c["withheld"].append(i["root"]), "follows the cutoff"),
        (lambda c, i: c["defects"]["d1"].update(introduced=i["root"]), "above the merge base"),
        (lambda c, i: c["defects"]["d1"].update(introduced=i["squash"]), "introduced in head"),
        (lambda c, i: c["defects"]["d1"]["location"][0].update(lines=[2, 6]), "location 0"),
        (lambda c, i: c["defects"]["d1"]["location"][0].update(path="src/b.py"), "location 0"),
        # A base revision the base ref reached only after the cutoff.
        (lambda c, i: c["review"].update(base_tip=i["after"]), "its ref's tip at the cutoff"),
        (lambda c, i: c["review"].update(base_tip=i["other"]), "its ref's tip at the cutoff"),
        (
            lambda c, i: c["review"].update(merge_base=i["root"], base_tip=i["root"]),
            "its ref's tip at the cutoff",
        ),
        (lambda c, i: c["review"].update(merge_base=i["root"], base_tip=i["root"]), "squash"),
    ],
)
def test_verify_names_each_pin_git_contradicts(history, change, rule):
    repo, ids = history
    corpus = historical(repo, ids)
    target = "merged" if rule == "squash" else "round1"
    if rule == "introduced in head":
        corpus["cases"][target]["withheld"].remove(ids["squash"])
    change(corpus["cases"][target], ids)
    report = verify(corpus, repo.path)
    assert not report["ok"]
    assert any(rule in failed for failed in report["cases"][target]["failed"]), report


def test_verify_refuses_an_ancestor_that_is_not_the_merge_base(history):
    repo, ids = history
    corpus = historical(repo, ids)
    # The root is an ancestor of the head, but the diff from it is the whole
    # history, not the round's change: the merge base is bound to the base
    # revision the round was reviewed against.
    corpus["cases"]["round1"]["review"]["merge_base"] = ids["root"]
    check_corpus(corpus)
    failed = verify(corpus, repo.path)["cases"]["round1"]["failed"]
    assert failed == ["the merge base is the base revision's and head's"]


def test_two_base_revisions_cannot_both_verify_on_a_line_out_of_date_order(tmp_path):
    repo = Repo(tmp_path / "repo")
    env = {"pyproject.toml": b"[project]\n", "uv.lock": b"version = 1\n"}
    code = b"".join(b"line %d\n" % n for n in range(1, 6))
    root = repo.commit({"README.md": b"root\n"}, [], "2026-01-01T00:00:00Z")
    early = repo.commit({**env, "README.md": b"early\n"}, [root], "2026-01-02T00:00:00Z")
    late = repo.commit({**env, "README.md": b"late\n"}, [early], "2026-01-05T00:00:00Z")
    # Committed on top of ``late`` and dated before it, as a rebase or a skewed
    # clock can: by committer time ``early`` and ``back`` were each the tip at
    # the cutoff, the commit after each dated after it.
    back = repo.commit({**env, "README.md": b"back\n"}, [late], "2026-01-02T12:00:00Z")
    tip = repo.commit({**env, "README.md": b"tip\n"}, [back], "2026-01-06T00:00:00Z")
    head = repo.commit(
        {**env, "README.md": b"back\n", "src/a.py": code}, [back], "2026-01-03T00:00:00Z"
    )
    fixed = repo.commit(
        {**env, "README.md": b"back\n", "src/a.py": code + b"fixed\n"},
        [head],
        "2026-01-05T00:00:00Z",
    )
    ids = {"root": root, "head": head, "fix": fixed, "squash": tip, "after": tip, "other": tip}
    failed = {}
    for base in (early, back):
        corpus = historical(repo, ids | {"base": base})
        del corpus["cases"]["merged"]
        check_corpus(corpus)
        failed[base] = verify(corpus, repo.path)["cases"]["round1"]["failed"]
    # Two bindings of one head and cutoff, two merge bases and so two diffs:
    # git cannot tell which the review read, so neither verifies.
    assert repo.git("merge-base", early, head) != repo.git("merge-base", back, head)
    assert failed == {
        early: ["the base ref's line splits at the cutoff"],
        back: ["the base ref's line splits at the cutoff"],
    }


def test_verify_refuses_a_cutoff_after_the_head_and_the_context_it_admits(history):
    repo, ids = history
    corpus = historical(repo, ids)
    case = corpus["cases"]["round1"]
    # A cutoff a day after the head admits the round's own review comment.
    case["review"]["cutoff"] = "2026-01-04T12:00:00Z"
    items(case).append(
        captured(
            "review-1",
            "pr_comment",
            "the round 1 review",
            "2026-01-04T06:00:00Z",
            "R1-F1: src/a.py lines 2 to 5 are never fixed.",
            "pr/2/comment/review-1",
        )
    )
    # The corpus alone cannot tell; git can.
    check_corpus(corpus)
    failed = verify(corpus, repo.path)["cases"]["round1"]["failed"]
    assert failed == ["the cutoff is the head's committer time"]
    # At the head's own time the same comment is refused outright.
    case["review"]["cutoff"] = "2026-01-03T00:00:00Z"
    with pytest.raises(CorpusError, match="context observed after the cutoff"):
        check_corpus(corpus)


def test_verify_holds_a_squash_case_to_its_merge_time(history):
    repo, ids = history
    corpus = historical(repo, ids)
    corpus["cases"]["merged"]["review"]["cutoff"] = "2026-01-06T00:00:01Z"
    failed = verify(corpus, repo.path)["cases"]["merged"]["failed"]
    assert failed == ["the cutoff is the head's committer time"]


def test_verify_holds_each_environment_pin_to_the_head(history, subs):
    repo, ids = history
    corpus = reviewed(repo, ids)
    # A declared file the head does not have is pinned as absent, not left out.
    corpus["repository"]["environment"].append("requirements.txt")
    for case in corpus["cases"].values():
        case["environment"]["files"]["requirements.txt"] = None
    assert verify(corpus, repo.path)["ok"]
    assert score(corpus, recorded(corpus, subs, ["round1"]), repo.path)["verified"]["ok"]
    # A file the head has, pinned as absent, verifies nothing and scores nothing.
    files = corpus["cases"]["round1"]["environment"]["files"]
    lock = files["uv.lock"]
    files["uv.lock"] = None
    failed = verify(corpus, repo.path)["cases"]["round1"]["failed"]
    assert failed == ["environment file 2 is absent at the head"]
    with pytest.raises(CorpusError, match="a recorded score verifies its corpus"):
        score(corpus, recorded(corpus, subs, ["round1"]), repo.path)
    # Nor does a file it has not, pinned as a blob.
    files.update({"uv.lock": lock, "requirements.txt": lock})
    assert verify(corpus, repo.path)["cases"]["round1"]["failed"] == ["environment file 1"]
    # Every pin removed: the corpus check refuses it before git is read.
    corpus["cases"]["round1"]["environment"] = {"files": {}, "validation": []}
    with pytest.raises(CorpusError, match="environment validation"):
        score(corpus, recorded(corpus, subs, ["round1"]), repo.path)
    corpus["cases"]["round1"]["environment"]["validation"] = ["make check"]
    with pytest.raises(CorpusError, match="a case pins each of the repository's environment"):
        score(corpus, recorded(corpus, subs, ["round1"]), repo.path)


def test_an_absent_placeholder_pins_no_environment(history, subs):
    repo, ids = history
    corpus = reviewed(repo, ids)
    # One declared file no head has, pinned as absent in place of every real
    # pin, though each head has both of the original files.
    corpus["repository"]["environment"] = ["requirements-none.txt"]
    for case in corpus["cases"].values():
        case["environment"]["files"] = {"requirements-none.txt": None}
    # Each pin is true of its head, so git finds nothing wrong, but a case that
    # pins no file of its environment never runs.
    assert verify(corpus, repo.path)["ok"]
    assert describe(corpus)["runnable"] == []
    with pytest.raises(CorpusError, match="a planned case is runnable"):
        score(corpus, recorded(corpus, subs, ["round1"]), repo.path)
    # One declared file present is a pin, the absent one beside it still null.
    corpus["repository"]["environment"].append("uv.lock")
    for case in corpus["cases"].values():
        case["environment"]["files"]["uv.lock"] = repo.git(
            "rev-parse", f"{case['review']['head']}:uv.lock"
        )
    assert describe(corpus)["runnable"] == ["round1"]
    assert score(corpus, recorded(corpus, subs, ["round1"]), repo.path)["verified"]["ok"]


def test_a_recorded_run_is_bound_to_its_variants_pins(history, subs):
    repo, ids = history
    corpus = reviewed(repo, ids)
    recorded(corpus, subs, ["round1"])
    assert score(corpus, subs, repo.path)["verified"]["ok"]
    # The baseline relabeled as another model at another effort, its runs
    # left as they were recorded: they are stale evidence for it.
    subs["variants"]["baseline"].update(model="another-model", effort="low")
    with pytest.raises(CorpusError, match="a run was launched with its variant's pins"):
        score(corpus, subs, repo.path)


# -- the review-time boundary of a review round --------------------------------


def binding(case, started, **change):
    """The binding statement of ``case``'s review record, as one line of its text."""
    review = case["review"]
    statement = {
        "pr": case["provenance"]["pr"],
        "round": case["provenance"]["round"],
        "base_ref": review["base_ref"],
        "base_tip": review["base_tip"],
        "head": review["head"],
        "merge_base": review["merge_base"],
        "input_read": started,
    } | change
    return "review-record: " + json.dumps(statement)


def record(case, started, text=None, source="pr/2/comment/review-1"):
    """The round's review record, retained apart from the corpus, as the cutoff."""
    text = text or (
        f"{binding(case, started)}\n# Review, round 1\n"
        "R1-F1: src/a.py lines 2 to 5 are never fixed."
    )
    case["record"] = {
        "status": "captured",
        "source": source,
        "started": started,
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
    }
    case["review"]["cutoff"] = started


def reviewed(repo, ids, started="2026-01-03T02:00:00Z"):
    """round1 made runnable: its issue, and the PR body opened an hour after the
    head was committed and an hour before the review read it, both captured and
    listed, and the review's record."""
    corpus = historical(repo, ids)
    case = corpus["cases"]["round1"]
    case["context"]["items"] = [
        captured("issue", "issue", "#1", "2026-01-02T12:00:00Z", "Issue 1: add a.py.", "issue/1"),
        captured("pr-body", "pr_body", "#2", "2026-01-03T01:00:00Z", "Adds src/a.py.", "pr/2"),
    ]
    complete(case, "2026-01-10T00:00:00Z")
    record(case, started)
    lineaged(case, "d1")
    return corpus


def test_a_review_record_admits_a_pr_body_opened_after_the_head(history):
    repo, ids = history
    corpus = reviewed(repo, ids)
    assert describe(corpus)["runnable"] == ["round1"]
    assert verify(corpus, repo.path)["ok"]
    # Without the record the cutoff is the head's committer time, which the PR
    # body postdates: captured, it is refused.
    case = corpus["cases"]["round1"]
    case["record"] = unrecorded()
    case["review"]["cutoff"] = "2026-01-03T00:00:00Z"
    with pytest.raises(CorpusError, match="context observed after the cutoff"):
        check_corpus(corpus)
    # And a round whose whole context predates its head still never runs
    # without the record that evidences its boundary.
    items(case)[1]["observed_at"] = "2026-01-02T23:00:00Z"
    assert describe(corpus)["runnable"] == []
    assert verify(corpus, repo.path)["ok"]


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        # A comment after the review read its input: its own result, say.
        (
            lambda c: (
                items(c).append(
                    captured(
                        "review",
                        "pr_comment",
                        "the round 1 review",
                        "2026-01-03T02:00:01Z",
                        "R1-F1: src/a.py lines 2 to 5 are never fixed.",
                        "pr/2/comment/9",
                    )
                ),
                complete(c, "2026-01-10T00:00:00Z"),
            ),
            "context observed after the cutoff",
        ),
        # The cutoff moved past the record's start, with the record or without.
        (
            lambda c: c["review"].update(cutoff="2026-01-03T03:00:00Z"),
            "the cutoff is the review record's start",
        ),
        (
            lambda c: (
                c["record"].update(started="2026-01-03T03:00:00Z"),
                c["review"].update(cutoff="2026-01-03T03:00:00Z"),
            ),
            "the review record's binding states its start",
        ),
        (
            lambda c: record(c, "2026-01-03T03:00:00Z", text="A review of some other head."),
            "a review record states one binding",
        ),
        (lambda c: c["record"].update(sha256="0" * 64), "review record digest"),
        (lambda c: c["record"].update(source=None), "a review record names its source"),
        # The record is the review's own result, never its input.
        (
            lambda c: (
                items(c).append(
                    uncaptured("review", "pr_comment", "the review", "pr/2/comment/review-1")
                ),
                complete(c, "2026-01-10T00:00:00Z"),
            ),
            "the review record is not the reviewer's context",
        ),
        (lambda c: c["record"].update(status="partial"), "review record status"),
        (lambda c: c["record"].update(status="not_captured"), "uncaptured review record holds"),
    ],
)
def test_context_after_the_recorded_boundary_is_refused(history, change, rule):
    repo, ids = history
    corpus = reviewed(repo, ids)
    change(corpus["cases"]["round1"])
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


def test_only_a_review_round_holds_a_review_record(history, corpus):
    repo, ids = history
    # A squash's record is its PR's merge record, never a review's.
    historic = historical(repo, ids)
    record(historic["cases"]["merged"], "2026-01-06T00:00:00Z")
    with pytest.raises(CorpusError, match="a merge record states no review start"):
        check_corpus(historic)
    historic["cases"]["merged"]["record"]["started"] = None
    with pytest.raises(CorpusError, match="a merge record states one binding"):
        check_corpus(historic)
    # A synthetic case holds no record of either kind.
    alpha = corpus["cases"]["alpha-defect"]
    record(alpha, alpha["review"]["cutoff"], source="document/r.md")
    with pytest.raises(CorpusError, match="only a review round or a squash holds its record"):
        check_corpus(corpus)


def test_git_bounds_a_recorded_start_by_the_head_and_the_fix(history):
    repo, ids = history
    corpus = reviewed(repo, ids)
    case = corpus["cases"]["round1"]
    # A start before the head was committed is no start of its review.
    items(case)[1]["observed_at"] = "2026-01-02T22:00:00Z"
    record(case, "2026-01-02T23:00:00Z")
    failed = verify(corpus, repo.path)["cases"]["round1"]["failed"]
    assert failed == ["the head was committed by the review's start"]
    # A start after the round's fix was committed is after the review.
    record(case, "2026-01-05T06:00:00Z")
    case["defects"]["d1"]["discovery"]["discovered"] = "2026-01-05"
    failed = verify(corpus, repo.path)["cases"]["round1"]["failed"]
    assert failed == [f"withheld {ids['fix'][:12]} follows the cutoff"]


def test_the_review_record_names_the_base_revision_a_late_ref_update_hides(history, subs):
    repo, ids = history
    env = {"pyproject.toml": b"[project]\n", "uv.lock": b"version = 1\n"}
    # On main after the base: a commit dated before the review read its input
    # (01-03 02:00) that reached main only after it, then main's next commit.
    # Git keeps when each was made, not when main reached it.
    late = repo.commit(
        {"README.md": b"root\n", **env, "NEWS": b"late\n"}, [ids["base"]], "2026-01-02T12:00:00Z"
    )
    tip = repo.commit(
        {"README.md": b"root\n", **env, "NEWS": b"tip\n"}, [late], "2026-01-04T00:00:00Z"
    )
    corpus = reviewed(repo, ids)
    del corpus["cases"]["merged"]
    corpus["repository"]["refs"]["main"] = tip
    case = corpus["cases"]["round1"]
    # The review read main at the base, as its record states: that verifies
    # and scores, though by committer times main was already at ``late``.
    report = verify(corpus, repo.path)
    assert report["ok"], report
    assert report["cases"]["round1"]["base_tip"] == "review_record"
    assert score(corpus, recorded(corpus, subs, ["round1"]), repo.path)["verified"]["ok"]
    # ``late``, with the same merge base and so the same diff, is refused under
    # the unchanged record: the frozen input would name a base the review
    # never read.
    assert repo.git("merge-base", late, ids["head"]) == ids["base"]
    case["review"]["base_tip"] = late
    with pytest.raises(CorpusError, match="the review record's binding names the case's review"):
        check_corpus(corpus)
    # A record that names a base revision git contradicts verifies nothing:
    # one main reached only after the review read its input, or one off main.
    for base_tip, failed in (
        (tip, ["the base revision was committed by the review's start"]),
        (
            ids["other"],
            [
                "the base revision is on its ref's line",
                "the base revision was committed by the review's start",
            ],
        ),
    ):
        case["review"]["base_tip"] = base_tip
        record(case, case["record"]["started"])
        assert verify(corpus, repo.path)["cases"]["round1"]["failed"] == failed
    # Without a record, committer times are all there is. They name ``late``,
    # which verify reports as provisional, and the case never runs.
    case["review"]["base_tip"] = late
    case["record"] = unrecorded()
    case["review"]["cutoff"] = "2026-01-03T00:00:00Z"
    case["context"]["items"] = [
        uncaptured("issue", "issue", "#1", "issue/1"),
        uncaptured("pr-body", "pr_body", "#2", "pr/2"),
    ]
    never_taken(case)
    report = verify(corpus, repo.path)
    assert report["ok"] and report["cases"]["round1"]["base_tip"] == "provisional"
    assert describe(corpus)["runnable"] == []
    case["review"]["base_tip"] = ids["base"]
    assert verify(corpus, repo.path)["cases"]["round1"]["failed"] == [
        "the base revision is its ref's tip at the cutoff"
    ]


def recorded(corpus, subs, cases):
    """Recorded submissions over ``cases``: every variant completed, no findings."""
    subs.update(mode="recorded", corpus=corpus_digest(corpus), plan={"cases": cases, "seeds": [1]})
    subs["runs"] = [
        {
            "variant": variant,
            "case": case,
            "seed": 1,
            "input": input_digest(corpus["cases"][case]),
            "pins_sha256": pins_digest(subs["variants"][variant]),
            "status": "completed",
            "exclusion": None,
            "cost_microusd": 0,
            "findings": [],
        }
        for variant in sorted(subs["variants"])
        for case in cases
    ]
    return subs


def test_a_recorded_score_needs_its_exact_corpus_verified(history, subs):
    repo, ids = history
    corpus = reviewed(repo, ids)
    recorded(corpus, subs, ["round1"])
    with pytest.raises(CorpusError, match="a recorded score verifies its corpus"):
        score(corpus, subs)
    report = score(corpus, subs, repo.path)
    assert report["evidence"] == "recorded"
    assert report["verified"] == {"corpus": corpus_digest(corpus), "ok": True}
    # The merged case made runnable with its cutoff a second past its merge,
    # admitting a comment between the two: the corpus check cannot tell, so
    # the case is runnable, but verify refuses it and so does the score.
    merged = corpus["cases"]["merged"]
    merged["review"]["cutoff"] = "2026-01-06T00:00:01Z"
    merged["context"]["items"] = [
        captured("issue", "issue", "#1", "2026-01-02T12:00:00Z", "Issue 1: add a.py.", "issue/1"),
        captured("pr-body", "pr_body", "#2", "2026-01-03T01:00:00Z", "Adds src/a.py.", "pr/2"),
        captured(
            "late",
            "pr_comment",
            "after the merge",
            "2026-01-06T00:00:01Z",
            "Merged.",
            "pr/2/comment/7",
        ),
    ]
    complete(merged, "2026-01-10T00:00:00Z")
    # Its context complete, a squash still runs only with its PR's merge record.
    assert describe(corpus)["runnable"] == ["round1"]
    merge_record(merged)
    assert describe(corpus)["runnable"] == ["merged", "round1"]
    assert not verify(corpus, repo.path)["ok"]
    recorded(corpus, subs, ["round1", "merged"])
    with pytest.raises(CorpusError, match="a recorded score verifies its corpus"):
        score(corpus, subs, repo.path)
    merged["review"]["cutoff"] = "2026-01-06T00:00:00Z"
    merged["context"]["items"].pop()
    complete(merged, "2026-01-10T00:00:00Z")
    report = score(corpus, recorded(corpus, subs, ["round1", "merged"]), repo.path)
    assert report["verified"]["ok"] and report["plan"]["cases"] == 2


def test_the_command_line_scores_a_historical_corpus_only_with_its_repository(
    history, subs, tmp_path, capsys
):
    repo, ids = history
    corpus = reviewed(repo, ids)
    corpus_path, subs_path = tmp_path / "corpus.json", tmp_path / "subs.json"
    corpus_path.write_text(json.dumps(corpus))
    subs_path.write_text(json.dumps(recorded(corpus, subs, ["round1"])))
    with pytest.raises(SystemExit, match="2"):
        main(["score", str(corpus_path), str(subs_path)])
    capsys.readouterr()
    assert main(["score", str(corpus_path), str(subs_path), "--repo", str(repo.path)]) == 0
    assert json.loads(capsys.readouterr().out)["verified"]["ok"] is True


# -- what a review record binds -------------------------------------------------


def rebound(case, **change):
    """``case``'s review record again, its binding statement changed."""
    started = case["record"]["started"]
    text = f"{binding(case, started, **change)}\nR1-F1: src/a.py lines 2 to 5 are never fixed."
    record(case, started, text=text, source=case["record"]["source"])


def test_a_review_record_binds_its_input_read_not_its_publication(history, subs):
    repo, ids = history
    corpus = reviewed(repo, ids)
    case = corpus["cases"]["round1"]
    # The review read its input at 01:30 and was published at 02:00, which the
    # cutoff claims; a comment between the two would reach the reviewer.
    text = (
        f"{binding(case, '2026-01-03T01:30:00Z')}\n"
        "Published 2026-01-03T02:00:00Z.\nR1-F1: src/a.py lines 2 to 5 are never fixed."
    )
    record(case, "2026-01-03T02:00:00Z", text=text)
    items(case).append(
        captured(
            "between",
            "pr_comment",
            "a comment after the review read its input",
            "2026-01-03T01:45:00Z",
            "Is line 2 really fine?",
            "pr/2/comment/5",
        )
    )
    complete(case, "2026-01-10T00:00:00Z")
    with pytest.raises(CorpusError, match="the review record's binding states its start"):
        check_corpus(corpus)
    with pytest.raises(CorpusError, match="the review record's binding states its start"):
        score(corpus, recorded(corpus, subs, ["round1"]), repo.path)
    # Prose is never read as a binding, whatever instant it names, and a
    # record states one binding only.
    prose = (
        f"Reviewed HEAD {ids['head']} (merge base {ids['base']}); input read at"
        " 2026-01-03T02:00:00Z.\nR1-F1: src/a.py lines 2 to 5 are never fixed."
    )
    twice = f"{binding(case, '2026-01-03T02:00:00Z')}\n{binding(case, '2026-01-03T01:30:00Z')}"
    for text in (prose, twice):
        record(case, "2026-01-03T02:00:00Z", text=text)
        with pytest.raises(CorpusError, match="a review record states one binding"):
            check_corpus(corpus)
    # A binding that states 02:00 evidences it, and admits the comment before it.
    record(case, "2026-01-03T02:00:00Z")
    complete(case, "2026-01-10T00:00:00Z")
    report = score(corpus, recorded(corpus, subs, ["round1"]), repo.path)
    assert report["verified"]["ok"]


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        # A comment on another PR, a comment on the issue, or the PR body.
        (
            lambda c: c["record"].update(source="pr/3/comment/review-1"),
            "a review record is a comment on the case's PR or a document",
        ),
        (
            lambda c: c["record"].update(source="issue/1/comment/review-1"),
            "a review record is a comment on the case's PR or a document",
        ),
        (
            lambda c: c["record"].update(source="pr/2"),
            "a review record is a comment on the case's PR or a document",
        ),
        # A binding of another PR or round.
        (lambda c: rebound(c, pr=3), "the review record's binding names the case's PR and round"),
        (lambda c: rebound(c, pr=None), "binding names the case's PR and round"),
        (lambda c: rebound(c, pr="2"), "binding names the case's PR and round"),
        (lambda c: rebound(c, round=2), "binding names the case's PR and round"),
        (lambda c: rebound(c, round=True), "binding names the case's PR and round"),
        # ...of another review of the round's head.
        (
            lambda c: rebound(c, base_ref="next"),
            "the review record's binding names the case's review",
        ),
        (
            lambda c: rebound(c, base_tip=c["review"]["head"]),
            "the review record's binding names the case's review",
        ),
        (
            lambda c: rebound(c, head=c["review"]["merge_base"]),
            "the review record's binding names the case's review",
        ),
        (
            lambda c: rebound(c, merge_base=c["review"]["head"]),
            "the review record's binding names the case's review",
        ),
        (lambda c: rebound(c, input_read="2026-01-03T01:00:00Z"), "binding states its start"),
        # A binding that is none.
        (lambda c: rebound(c, extra=1), "fields: pr round"),
        (
            lambda c: record(
                c, "2026-01-03T02:00:00Z", text=binding(c, "2026-01-03T02:00:00Z")[:-1]
            ),
            "review record binding",
        ),
        (
            lambda c: record(
                c, "2026-01-03T02:00:00Z", text="> " + binding(c, "2026-01-03T02:00:00Z")
            ),
            "a review record states one binding",
        ),
    ],
)
def test_a_review_record_is_bound_to_its_case(history, change, rule):
    repo, ids = history
    corpus = reviewed(repo, ids)
    change(corpus["cases"]["round1"])
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


def test_a_review_record_may_be_a_document_kept_apart(history):
    repo, ids = history
    corpus = reviewed(repo, ids)
    case = corpus["cases"]["round1"]
    # A record the controller kept of the launch, say, rather than a comment.
    record(case, "2026-01-03T02:00:00Z", source="document/reviews/pr-2-round-1.json")
    assert describe(corpus)["runnable"] == ["round1"]
    assert verify(corpus, repo.path)["ok"]


@pytest.mark.parametrize(
    "copy",
    [
        # The record itself, its digest correct, as a document.
        lambda text: text,
        # Inside a longer document, indented and upper-cased.
        lambda text: "Notes:\n" + "\n".join(f"    {row.upper()}" for row in text.splitlines()),
        # Without its binding statement.
        lambda text: "\n".join(text.splitlines()[1:]),
        # Its binding statement alone, quoted.
        lambda text: "> " + text.splitlines()[0],
    ],
)
def test_the_review_record_never_reaches_its_reviewer_under_another_source(history, subs, copy):
    repo, ids = history
    corpus = reviewed(repo, ids)
    case = corpus["cases"]["round1"]
    items(case).append(
        captured(
            "notes",
            "document",
            "review notes",
            "2026-01-03T02:00:00Z",
            copy(case["record"]["text"]),
            "document/notes/review.md",
        )
    )
    complete(case, "2026-01-10T00:00:00Z")
    rule = "the review record is not the reviewer's context"
    with pytest.raises(CorpusError, match=rule):
        describe(corpus)
    with pytest.raises(CorpusError, match=rule):
        verify(corpus, repo.path)
    with pytest.raises(CorpusError, match=rule):
        score(corpus, recorded(corpus, subs, ["round1"]), repo.path)


@pytest.mark.parametrize(
    "place",
    [
        # The whole record as the issue's reference.
        lambda c, text: items(c)[0].update(ref=text),
        # ...as a validation command a runner is given.
        lambda c, text: c["environment"]["validation"].append(text),
        # ...its lines split over two references, one in each.
        lambda c, text: (
            items(c)[0].update(ref=text.splitlines()[1]),
            items(c)[1].update(ref=text.splitlines()[2]),
        ),
        # ...its binding statement alone, in a reference.
        lambda c, text: items(c)[1].update(ref="as of " + text.splitlines()[0]),
    ],
)
def test_no_value_the_reviewer_reads_holds_the_review_record(history, subs, place):
    repo, ids = history
    corpus = reviewed(repo, ids)
    case = corpus["cases"]["round1"]
    texts = [(item["text"], item["sha256"]) for item in items(case)]
    place(case, case["record"]["text"])
    # The context's own text and digests are as they were: only the copy differs.
    assert [(item["text"], item["sha256"]) for item in items(case)] == texts
    rule = "the review record is not the reviewer's context"
    with pytest.raises(CorpusError, match=rule):
        describe(corpus)
    with pytest.raises(CorpusError, match=rule):
        verify(corpus, repo.path)
    with pytest.raises(CorpusError, match=rule):
        score(corpus, recorded(corpus, subs, ["round1"]), repo.path)


def test_no_value_the_reviewer_reads_holds_a_merge_or_lineage_record(history):
    repo, ids = history
    corpus = reviewed(repo, ids)
    case = corpus["cases"]["round1"]
    # A lineage record's binding names the fix, which the leakage rule refuses
    # anyway; the rest of its text, without the binding, is refused as the record.
    body = case["defects"]["d1"]["discovery"]["lineage"]["text"].splitlines()[1]
    items(case)[0]["ref"] = body
    with pytest.raises(CorpusError, match="the lineage record is not the reviewer's context"):
        check_corpus(corpus)
    items(case)[0]["ref"] = "#1"
    # A squash's merge record, as its PR body's reference.
    merged = corpus["cases"]["merged"]
    merge_record(merged)
    items(merged)[1]["ref"] = merged["record"]["text"]
    with pytest.raises(CorpusError, match="the merge record is not the reviewer's context"):
        check_corpus(corpus)


# -- a defect that escaped through a squash ------------------------------------


def escaped(ids, **discovery):
    """A defect of the branch's head, found after its squash landed on main."""
    return {
        "severity": "low",
        "class": "correctness",
        "introduced": ids["head"],
        "summary": "line 1 is wrong",
        "location": [{"path": "src/a.py", "lines": [1, 1]}],
        "discovery": {
            "source": "post_merge",
            "round": None,
            "finding": None,
            "discovered": "2026-01-07",
            "fix": ids["after"],
            "merged": ids["squash"],
            "lineage": unlineaged(),
        }
        | discovery,
        "labels": [],
    }


@pytest.mark.parametrize(
    ("discovery", "failed"),
    [
        # The squash is not in the branch commit's history, nor the fix.
        ({"merged": None}, ["defect e1 fix follows its introduction"]),
        # A fix off the line the squash merged into is unrelated, and changes
        # another file.
        (
            {"fix": "other"},
            ["defect e1 fix follows its merge", "defect e1 fix changes what it is located in"],
        ),
        # A merged revision off the base line, which names no PR.
        (
            {"merged": "fix"},
            [
                "defect e1 merged on its base ref's line",
                "defect e1 merge is its PR's squash",
                "defect e1 fix follows its merge",
            ],
        ),
    ],
)
def test_verify_follows_a_squashed_defect_to_its_post_merge_fix(history, discovery, failed):
    repo, ids = history
    corpus = historical(repo, ids)
    case = corpus["cases"]["round1"]
    case["stratum"] = "escape"
    case["withheld"] += [ids["after"], ids["other"]]
    case["defects"] = {"e1": escaped(ids)}
    assert verify(corpus, repo.path)["ok"]
    case["defects"]["e1"]["discovery"] |= {
        key: None if value is None else ids[value] for key, value in discovery.items()
    }
    assert verify(corpus, repo.path)["cases"]["round1"]["failed"] == failed


def test_a_squash_lineage_needs_the_merge_to_carry_the_defect(history):
    repo, ids = history
    corpus = historical(repo, ids)
    case = corpus["cases"]["round1"]
    case["withheld"] += [ids["after"]]
    case["defects"]["e1"] = escaped(ids)
    # The squash never changed the file this defect is located in, nor did
    # the fix.
    case["defects"]["e1"]["location"] = [{"path": "pyproject.toml", "lines": [1, 1]}]
    assert verify(corpus, repo.path)["cases"]["round1"]["failed"] == [
        "defect e1 merge carries it",
        "defect e1 fix changes what it is located in",
    ]


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (lambda d, i: d.update(fix=None), "a merged revision leads to a fix"),
        (lambda d, i: d.update(merged=i["base"]), "a merged revision is withheld"),
        (lambda d, i: d.update(merged=i["after"]), "a merged revision is its own commit"),
    ],
)
def test_a_merged_revision_is_refused(history, change, rule):
    repo, ids = history
    corpus = historical(repo, ids)
    case = corpus["cases"]["round1"]
    case["withheld"] += [ids["after"]]
    case["defects"]["e1"] = escaped(ids)
    change(case["defects"]["e1"]["discovery"], ids)
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        # Nothing to bind a fix or a merge to.
        (lambda c: c["defects"]["e1"].update(location=[]), "defect location"),
        # A squash is checked by the lines it carried.
        (
            lambda c: c["defects"]["e1"]["location"][0].update(lines=None),
            "a merged defect is located by its lines",
        ),
        # A squash is of a PR.
        (
            lambda c: (c["provenance"].update(pr=None), items(c)[1].update(source=None)),
            "a merged revision follows a review round of a PR",
        ),
    ],
)
def test_a_squash_lineage_is_refused_without_what_binds_it(history, change, rule):
    repo, ids = history
    corpus = historical(repo, ids)
    case = corpus["cases"]["round1"]
    case["withheld"] += [ids["after"]]
    case["defects"]["e1"] = escaped(ids)
    change(case)
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


def test_a_same_file_rewrite_and_a_later_descendant_are_no_squash_lineage(history, subs):
    repo, ids = history
    env = {"pyproject.toml": b"[project]\n", "uv.lock": b"version = 1\n"}
    # Later on main: a commit that rewrote src/a.py on its own, its subject
    # even naming the PR, and a documentation-only commit after it.
    rewrite = repo.commit(
        {**env, "src/a.py": b"rewritten\n"}, [ids["after"]], "2026-01-09T00:00:00Z", "Redo (#2)"
    )
    docs = repo.commit(
        {**env, "src/a.py": b"rewritten\n", "README.md": b"docs\n"},
        [rewrite],
        "2026-01-10T00:00:00Z",
    )
    corpus = reviewed(repo, ids)
    corpus["repository"]["refs"]["main"] = docs
    case = corpus["cases"]["round1"]
    case["stratum"] = "escape"
    case["withheld"] += [ids["after"], rewrite, docs]
    # The squash that carried the defect and the commit that fixed it verify.
    case["defects"] = {"e1": escaped(ids)}
    lineaged(case, "e1")
    assert verify(corpus, repo.path)["ok"]
    assert score(corpus, recorded(corpus, subs, ["round1"]), repo.path)["verified"]["ok"]
    # The rewrite and its descendant named in their place do not: the
    # defect's line is not in the rewrite, and the descendant changed no file
    # the defect is in. (Their own lineage record, were there one, would not
    # make git agree.)
    case["defects"] = {"e1": escaped(ids, merged=rewrite, fix=docs)}
    lineaged(case, "e1")
    check_corpus(corpus)
    assert verify(corpus, repo.path)["cases"]["round1"]["failed"] == [
        "defect e1 survives its merge",
        "defect e1 fix changes what it is located in",
    ]
    with pytest.raises(CorpusError, match="a recorded score verifies its corpus"):
        score(corpus, recorded(corpus, subs, ["round1"]), repo.path)


def test_a_lineage_runs_only_on_its_record_since_git_only_shows_it_consistent(history, subs):
    repo, ids = history
    env = {"pyproject.toml": b"[project]\n", "uv.lock": b"version = 1\n"}
    code = b"".join(b"line %d\n" % n for n in range(1, 6))
    # Later on main, after the genuine squash: a commit whose subject names
    # the same PR and that edits src/a.py, leaving the defect's line 1 as the
    # head had it, and a descendant that only adds a comment beside line 1.
    polish = repo.commit(
        {**env, "src/a.py": code + b"fixed\npolished\n"},
        [ids["squash"]],
        "2026-01-08T00:00:00Z",
        "Polish a.py (#2)",
    )
    remark = repo.commit(
        {**env, "src/a.py": b"line 1\n# line 1 is as intended\n" + code[7:] + b"fixed\npolished\n"},
        [polish],
        "2026-01-09T00:00:00Z",
    )
    # Off the head, a comment beside the round's own defect, in place of its fix.
    aside = repo.commit(
        {**env, "src/a.py": code.replace(b"line 5\n", b"line 5\n# lines 2 to 5 are fine\n")},
        [ids["head"]],
        "2026-01-05T00:00:00Z",
    )
    corpus = reviewed(repo, ids)
    corpus["repository"]["refs"]["main"] = remark
    case = corpus["cases"]["round1"]
    case["stratum"] = "escape"
    case["withheld"] += [ids["after"], polish, remark, aside]
    case["defects"]["e1"] = escaped(ids)
    lineaged(case, "e1")
    # The genuine chains, each stated by its lineage record: git agrees, and
    # the round scores.
    assert verify(corpus, repo.path)["ok"]
    assert score(corpus, recorded(corpus, subs, ["round1"]), repo.path)["verified"]["ok"]
    # The false chains are as consistent with git as the genuine ones: each
    # merge names the PR, changed the file and holds the line, and each fix
    # touched the defect's lines. Under the genuine records they are refused.
    rule = "the lineage record's binding names the defect's lineage"
    for key, false in (("e1", {"merged": polish, "fix": remark}), ("d1", {"fix": aside})):
        genuine = deepcopy(case["defects"][key])
        case["defects"][key]["discovery"] |= false
        with pytest.raises(CorpusError, match=rule):
            check_corpus(corpus)
        # Without a record git finds nothing wrong, and the case never runs.
        case["defects"][key]["discovery"]["lineage"] = unlineaged()
        assert verify(corpus, repo.path)["ok"]
        assert describe(corpus)["runnable"] == []
        with pytest.raises(CorpusError, match="a planned case is runnable"):
            score(corpus, recorded(corpus, subs, ["round1"]), repo.path)
        case["defects"][key] = genuine
    assert describe(corpus)["runnable"] == ["round1"]


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (lambda c: lineaged(c, "d1", pr=3), "the lineage record's binding names the defect"),
        (lambda c: lineaged(c, "d1", pr=True), "the lineage record's binding names the defect"),
        (
            lambda c: lineaged(c, "d1", introduced=c["review"]["merge_base"]),
            "the lineage record's binding names the defect",
        ),
        (
            lambda c: lineaged(c, "d1", paths=["src/b.py"]),
            "the lineage record's binding names the defect",
        ),
        (
            lambda c: lineaged(c, "d1", fix=c["withheld"][1]),
            "the lineage record's binding names the defect's lineage",
        ),
        (
            lambda c: lineaged(c, "d1", merged=c["withheld"][1]),
            "the lineage record's binding names the defect's lineage",
        ),
        (lambda c: lineaged(c, "d1", extra=1), "fields: pr introduced"),
        (
            lambda c: lineaged(c, "d1", text="The fix of d1, in prose only."),
            "a lineage record states one binding",
        ),
        (
            lambda c: lineaged(c, "d1", source="pr/3/comment/fix"),
            "a lineage record is a comment on the case's PR or a document",
        ),
        (lambda c: lineaged(c, "d1", source="pr/2"), "a lineage record is a comment"),
        (lambda c: lineaged(c, "d1", source="issue/1"), "a lineage record is a comment"),
        (
            lambda c: (
                items(c).append(uncaptured("fix", "pr_comment", "the fix", "pr/2/comment/fix")),
                complete(c, "2026-01-10T00:00:00Z"),
                lineaged(c, "d1", source="pr/2/comment/fix"),
            ),
            "the lineage record is not the reviewer's context",
        ),
        (
            lambda c: c["defects"]["d1"]["discovery"]["lineage"].update(sha256="0" * 64),
            "lineage record digest",
        ),
        (
            lambda c: c["defects"]["d1"]["discovery"]["lineage"].update(source=None),
            "a lineage record names its source",
        ),
        (
            lambda c: c["defects"]["d1"]["discovery"]["lineage"].update(status="partial"),
            "lineage record status",
        ),
        (
            lambda c: c["defects"]["d1"]["discovery"]["lineage"].update(status="not_captured"),
            "an uncaptured lineage record holds nothing",
        ),
        (
            lambda c: c["defects"]["d1"]["discovery"].update(fix=None, discovered="2026-01-04"),
            "a lineage record evidences a fix",
        ),
    ],
)
def test_a_lineage_record_is_bound_to_its_defect(history, change, rule):
    repo, ids = history
    corpus = reviewed(repo, ids)
    change(corpus["cases"]["round1"])
    with pytest.raises(CorpusError, match=rule):
        check_corpus(corpus)


def test_a_squash_candidate_runs_only_as_its_own_prs_merge(history, subs):
    repo, ids = history
    corpus = reviewed(repo, ids)
    merged = corpus["cases"]["merged"]
    merged["context"]["items"] = [
        captured("issue", "issue", "#1", "2026-01-02T12:00:00Z", "Issue 1: add a.py.", "issue/1"),
        captured("pr-body", "pr_body", "#2", "2026-01-03T01:00:00Z", "Adds src/a.py.", "pr/2"),
    ]
    complete(merged, "2026-01-10T00:00:00Z")
    merge_record(merged)
    report = verify(corpus, repo.path)
    assert report["ok"] and report["cases"]["merged"]["base_tip"] == "squash_parent"
    assert score(corpus, recorded(corpus, subs, ["round1", "merged"]), repo.path)["verified"]["ok"]
    # Another PR's provenance, PR body, inventory and merge record, all
    # agreeing: nothing offline contradicts them, but the commit names PR 2.
    merged["provenance"]["pr"] = 99
    items(merged)[1] = captured(
        "pr-body", "pr_body", "#99", "2026-01-03T01:00:00Z", "PR 99's body.", "pr/99"
    )
    complete(merged, "2026-01-10T00:00:00Z")
    merge_record(merged, source="document/merges/pr-99.json")
    assert describe(corpus)["runnable"] == ["merged", "round1"]
    assert verify(corpus, repo.path)["cases"]["merged"]["failed"] == [
        "a squash candidate is its PR's squash"
    ]
    with pytest.raises(CorpusError, match="a recorded score verifies its corpus"):
        score(corpus, recorded(corpus, subs, ["round1", "merged"]), repo.path)
    # Nor can the case name PR 99 under PR 2's merge record.
    merge_record(merged, pr=2)
    with pytest.raises(CorpusError, match="the merge record's binding names the case's PR"):
        check_corpus(corpus)
    merged["provenance"]["pr"] = 2
    items(merged)[1] = captured(
        "pr-body", "pr_body", "#2", "2026-01-03T01:00:00Z", "Adds src/a.py.", "pr/2"
    )
    complete(merged, "2026-01-10T00:00:00Z")
    # ...nor under a record of another commit, or of another base ref.
    for change in ({"merged": ids["after"]}, {"base_ref": "next"}):
        merge_record(merged, **change)
        with pytest.raises(CorpusError, match="the merge record's binding names the case's merge"):
            check_corpus(corpus)
    # A squash candidate names its PR.
    merged["provenance"]["pr"] = None
    with pytest.raises(CorpusError, match="a squash candidate names its PR"):
        check_corpus(corpus)


def test_an_ordinary_merge_commit_is_no_squash(history):
    repo, ids = history
    env = {"pyproject.toml": b"[project]\n", "uv.lock": b"version = 1\n"}
    code = b"".join(b"line %d\n" % n for n in range(1, 6))
    # main merges the branch head with a merge commit whose subject names the
    # PR as a squash's would, and a fix of line 1 follows it.
    merge = repo.commit(
        {**env, "src/a.py": code}, [ids["base"], ids["head"]], "2026-01-06T12:00:00Z", "Add (#2)"
    )
    fix = repo.commit({**env, "src/a.py": b"LINE 1\n" + code[7:]}, [merge], "2026-01-07T12:00:00Z")
    corpus = historical(repo, ids)
    corpus["repository"]["refs"]["main"] = fix
    candidate = corpus["cases"]["merged"]
    candidate["review"].update(
        head=merge,
        tree=repo.git("rev-parse", f"{merge}^{{tree}}"),
        cutoff="2026-01-06T12:00:00Z",
    )
    round1 = corpus["cases"]["round1"]
    round1["stratum"] = "escape"
    round1["withheld"] += [merge, fix]
    round1["defects"]["e1"] = escaped(ids, merged=merge, fix=fix)
    check_corpus(corpus)
    report = verify(corpus, repo.path)
    assert report["cases"]["merged"]["failed"] == ["a squash candidate has one parent"]
    assert report["cases"]["round1"]["failed"] == ["defect e1 merge is its PR's squash"]


def test_a_fix_changes_what_its_defect_is_located_in(history):
    repo, ids = history
    env = {"pyproject.toml": b"[project]\n", "uv.lock": b"version = 1\n"}
    code = b"".join(b"line %d\n" % n for n in range(1, 6))
    # Two later commits on the head: one changes only another file, the other
    # only line 5 of src/a.py.
    docs = repo.commit(
        {**env, "src/a.py": code, "README.md": b"docs\n"}, [ids["head"]], "2026-01-05T00:00:00Z"
    )
    line5 = repo.commit(
        {**env, "src/a.py": code.replace(b"line 5", b"LINE 5")},
        [ids["head"]],
        "2026-01-05T00:00:00Z",
    )
    corpus = historical(repo, ids)
    case = corpus["cases"]["round1"]
    case["withheld"] += [docs, line5]
    defect = case["defects"]["d1"]
    place = defect["location"][0]

    def failed(fix, lines):
        defect["discovery"]["fix"] = fix
        place["lines"] = lines
        return verify(corpus, repo.path)["cases"]["round1"]["failed"]

    unaddressed = ["defect d1 fix changes what it is located in"]
    # Lines 2 to 5: the round's fix appends beside line 5, the other changes it.
    assert failed(ids["fix"], [2, 5]) == []
    assert failed(line5, [2, 5]) == []
    # A descendant that changed another file fixes nothing, however located.
    assert failed(docs, [2, 5]) == unaddressed
    assert failed(docs, None) == unaddressed
    # Lines 2 and 3: neither changes them, nor inserts among or beside them.
    assert failed(line5, [2, 3]) == unaddressed
    assert failed(ids["fix"], [2, 3]) == unaddressed
    # The whole file: any change to it.
    assert failed(line5, None) == []


def test_verify_holds_exposure_evidence_to_the_commits(history):
    repo, ids = history
    # The review read its input the day after the head was committed.
    corpus = reviewed(repo, ids, started="2026-01-04T06:00:00Z")
    case = corpus["cases"]["round1"]
    case["exposure"]["possible_from"] = "2026-01-03"
    assert verify(corpus, repo.path)["ok"]
    # The candidate's day does not rule out its head's code, public a day earlier.
    case["exposure"]["possible_from"] = "2026-01-04"
    check_corpus(corpus)
    failed = verify(corpus, repo.path)["cases"]["round1"]["failed"]
    assert failed == ["exposure possible no later than the commits it is about"]


def test_verify_stops_at_an_unknown_candidate(history):
    repo, ids = history
    corpus = historical(repo, ids)
    corpus["cases"]["round1"]["review"]["head"] = "7" * 40
    assert verify(corpus, repo.path)["cases"]["round1"]["failed"] == ["head is a commit"]


def test_verify_takes_only_a_historical_corpus(corpus, tmp_path):
    with pytest.raises(CorpusError, match="historical"):
        verify(corpus, tmp_path)


def test_the_command_line_verify_exits_one_on_a_contradiction(history, tmp_path, capsys):
    repo, ids = history
    corpus = historical(repo, ids)
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(corpus))
    assert main(["verify", str(path), "--repo", str(repo.path)]) == 0
    corpus["cases"]["round1"]["review"]["tree"] = ids["root"]
    path.write_text(json.dumps(corpus))
    capsys.readouterr()
    assert main(["verify", str(path), "--repo", str(repo.path)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_the_module_runs_as_a_script():
    done = subprocess.run(
        [sys.executable, "-m", "tests.review_corpus", "describe"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=True,
    )
    assert json.loads(done.stdout)["cases"] == 8
