"""The evaluation protocol's pinned files, statistics, decisions and refusals (#318).

``tests/evaluation.py`` decides whether Issue Memory and multi-agent execution
earn their cost, from records of runs. These tests hold what must not move
quietly: the pre-registered numbers and the protocol document stay one, the pins
stay valid, an unmeasured value can never pass a criterion, a fixture can never
reach a decision, and no CI job or `make` gate runs a real evaluation. They use
synthetic records, so they say nothing about any model.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import subprocess
from functools import cache
from pathlib import Path

import pytest

from autoforge.config import (
    AutoForgeConfig,
    ValidationConfig,
    default_config,
    load_config_file,
)
from tests import evaluation as ev

from .test_ci_workflow import _code, _make_dry_run, _make_rules
from .test_removal_attestation import _bullets, _section, _test_functions
from .test_troubleshooting_guide import _broken, _links, _markdown_files

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTOCOL = REPO_ROOT / "docs" / "evaluation-protocol.md"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

BASELINE = "single_agent_no_memory"
TREATMENT = "single_agent_issue_memory"


@cache
def _protocol() -> str:
    return PROTOCOL.read_text(encoding="utf-8")


def _prose(text: str) -> str:
    return " ".join(text.split())


def _manifest_with_memory() -> dict:
    """The manifest as it will read when Issue Memory lands: arm 2 available."""
    manifest = copy.deepcopy(ev.load_manifest())
    for arm in manifest["arms"]:
        if arm["id"] == TREATMENT:
            arm["status"], arm["requires"] = "available", []
    return manifest


def _record(
    manifest: dict,
    prereg: dict,
    task: dict,
    seed: int,
    arm: str,
    *,
    verified: bool = True,
    evidence: str = ev.REAL,
    outcome: str | None = None,
    **counts: int,
) -> dict:
    """A synthetic record with every value measured, of the shape ``score_run`` writes."""
    measured = {"value": 1.0, "provenance": ev.MEASURED if evidence == ev.REAL else ev.SYNTHETIC}
    memory = {"value": 0, "provenance": ev.MEASURED}
    return {
        "schema": ev.RUN_SCHEMA,
        "evidence": evidence,
        "arm": arm,
        "task": task["id"],
        "memory_class": task["memory_class"],
        "seed": seed,
        "manifest_digest": ev.digest(manifest),
        "preregistration_digest": ev.digest(prereg),
        "settings_digest": "s" * 64,
        "settings_evidence": {"status": "established", "problems": []},
        "run_evidence": True,
        "prior_experience": ev.seeded_digest(manifest, arm, task),
        "phase": "READY_FOR_MERGE",
        "outcome": outcome or (ev.VERIFIED if verified else ev.UNVERIFIED),
        "unverified_reasons": [],
        "validation": "passed",
        "certificate": {"held": True, "candidate": "c" * 40, "problem": ""},
        "final_candidate": "c" * 40,
        "candidates": ["c" * 40],
        "oracle": {"c" * 40: "pass" if verified else "fail"},
        "pristine_checkers": {"status": "pass", "failing_modules": []},
        "altered_checkers": [],
        "counts": {**ev._empty_counts(), **counts},
        "unknown_counts": [],
        "usage": {key: dict(measured) for key in ev.MEASURES},
        "memory": {
            "stale_leaks": dict(memory),
            "storage_bytes": dict(memory),
            "projection_chars": dict(memory),
            "latency_ms": dict(memory),
        },
        "safety_failures": [],
        "safety_assessed": True,
        "safety_evidence": "the operator's check",
        "stopped_reason": "",
        "journal": "read",
        "journal_problems": [],
        "volatile": {"run_id": "r", "wall_clock_seconds": dict(measured)},
    }


def _world(treated_verified=lambda task, seed: True, baseline_verified=lambda task, seed: False):
    """Both arms of every planned pair: Memory verifies, the baseline does not, by default."""
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    records = []
    for task in manifest["tasks"]:
        for seed in prereg["sample"]["seeds"]:
            dependent = task["memory_class"] == "memory_dependent"
            records.append(
                _record(
                    manifest,
                    prereg,
                    task,
                    seed,
                    BASELINE,
                    verified=not dependent or baseline_verified(task, seed),
                )
            )
            records.append(
                _record(
                    manifest,
                    prereg,
                    task,
                    seed,
                    TREATMENT,
                    verified=not dependent or treated_verified(task, seed),
                )
            )
    return manifest, prereg, records


# A fixture report that covers the treatment arm: what the crash gate reads.
_COVERED = {"harness_correctness": {"arms_covered": [TREATMENT], "status": "pass"}}


def _decide(manifest: dict, prereg: dict, records: list[dict], harness=_COVERED) -> dict:
    return ev.evaluate_contrast(
        BASELINE,
        TREATMENT,
        prereg["comparisons"]["memory_benefit"],
        records,
        manifest,
        prereg,
        harness,
    )


# -- the pinned files ------------------------------------------------------------------------


def test_the_pinned_files_have_no_problem():
    manifest, prereg = ev.load_manifest(), ev.load_preregistration()
    assert ev.manifest_problems(manifest) == []
    assert ev.preregistration_problems(prereg, manifest) == []
    assert ev.main(["check"]) == 0


@pytest.mark.parametrize(
    "leak",
    ["the full reference sha", "a hidden test's name"],
)
def test_a_statement_that_gives_the_answer_away_is_refused(leak):
    """No hidden-test leakage: what the agent reads names neither the fix nor its checks."""
    manifest = ev.load_manifest()
    task = manifest["tasks"][0]
    named = task["reference_sha"] if leak.startswith("the full") else ev.hidden_names(task)[0]
    task["statement"] += f"\n\nSee {named}."
    assert any("gives away" in problem for problem in ev.manifest_problems(manifest))


def test_a_task_needs_its_match_to_be_of_the_other_memory_class():
    manifest = ev.load_manifest()
    first = manifest["tasks"][0]
    first["memory_class"] = ev.tasks_by_id(manifest)[first["control_for"]]["memory_class"]
    assert any("same memory class" in problem for problem in ev.manifest_problems(manifest))


def test_an_arm_that_is_not_evaluated_names_what_it_needs():
    """A missing feature leaves its arm not evaluated, and says which issue would change that."""
    manifest = ev.load_manifest()
    arms = ev.arms_by_id(manifest)
    assert arms[BASELINE] | {"comparison_role": ""} == {
        "id": BASELINE,
        "status": "available",
        "requires": [],
        "comparison_role": "",
        "seeded_experience": False,
    }
    assert arms[TREATMENT]["requires"] == [317, 333]
    assert all(arm["status"] == "not_evaluated" for arm in arms.values() if arm["id"] != BASELINE)
    arms[TREATMENT]["requires"] = []
    assert any("names no missing feature" in p for p in ev.manifest_problems(manifest))


def test_the_pins_are_what_this_repositorys_history_holds():
    """Each base is its reference's parent, each hidden test exists there. An export has
    no history to read: the suite also runs where validation.commands run it."""
    _history_or_skip()
    problems = [
        problem
        for task in ev.load_manifest()["tasks"]
        for problem in ev.verify_pin_history(str(REPO_ROOT), task)
    ]
    assert problems == []


def test_every_pinned_task_was_verified_fair():
    """The recorded verification takes every task once, and each fails at its base on
    behaviour and passes at its reference. Its selection counts add up to the rule's."""
    recorded = ev.read_json(ev.PIN_VERIFICATION)
    tasks = {task["id"] for task in ev.load_manifest()["tasks"]}
    assert set(recorded["results"]) == tasks
    for result in recorded["results"].values():
        assert result == {
            "base": "fail",
            "reference": "pass",
            "coupled_to_new_names": False,
            "lesson": "pass",
        }
    selection = recorded["selection"]
    assert selection["eligible_commits"] == (
        len(tasks)
        + len(selection["refused_by_the_fair_rule"])
        + len(selection["fair_but_not_taken"])
    )


def test_each_task_verified_its_validation_passes_at_its_base():
    """A base that fails its own validation fails every candidate of every arm, whatever
    the agent did. The recorded measurement takes every task once, each passing under the
    modules it leaves out, and says that a run host must measure this again."""
    recorded = ev.read_json(ev.PIN_VERIFICATION)["base_suite"]
    manifest = ev.load_manifest()
    assert set(recorded["results"]) == set(ev.tasks_by_id(manifest))
    assert set(recorded["results"].values()) == {"pass"}
    assert "sandbox.mode" in recorded["limits"]


def test_each_task_verified_its_pristine_checkers_pass_at_its_reference():
    """The pristine suite is a candidate's source under the reference's `tests/`. Should the
    reference itself not pass it, no candidate of any arm could. The recorded measurement
    takes every task once, and says a run host must measure it again."""
    recorded = ev.read_json(ev.PIN_VERIFICATION)["reference_suite"]
    assert set(recorded["results"]) == set(ev.tasks_by_id(ev.load_manifest()))
    assert set(recorded["results"].values()) == {"pass"}
    assert "sandbox.mode" in recorded["limits"]


def test_a_tasks_validation_is_the_suite_without_what_its_base_already_fails():
    """Each command fits ADR 0006 D10.3's bounds, holds the pinned prefix and the task's
    ignores, is what the plan prints, and is the only way a task's settings differ."""
    manifest, prereg = ev.load_manifest(), ev.load_preregistration()
    plan = ev.plan_runs("memory_benefit", manifest, prereg)
    printed = {run["task"]: run["validation_commands"] for run in plan["runs"]}
    pinned = ev.equalized_settings(load_config_file(str(ev.REAL_RUN_CONFIG)))
    for task in manifest["tasks"]:
        command = ev.validation_command(manifest, task)
        assert ev.definitions([command])
        assert (
            command[: len(manifest["validation_command_prefix"])]
            == (manifest["validation_command_prefix"])
        )
        for path in task["validation_ignore"]:
            assert f"--ignore={path}" in command
        assert printed[task["id"]] == [command]
        held = ev.equalized_settings(ev.task_config(manifest, task))
        assert held["validation"]["commands"] == [command]
        del held["validation"], pinned["validation"]
        assert held == pinned
        pinned = ev.equalized_settings(load_config_file(str(ev.REAL_RUN_CONFIG)))


def test_a_summary_names_the_modules_that_failed():
    stdout = (
        "..F\nFAILED tests/test_a.py::test_x - AssertionError\n"
        "FAILED tests/test_a.py::test_y[case]\nERROR tests/sub/test_b.py\n"
        "tests/test_c.py passed\n1 failed in 1s\n"
    )
    assert ev.failing_modules(stdout) == ["tests/sub/test_b.py", "tests/test_a.py"]


# -- what a Memory arm starts with -----------------------------------------------------------


def test_every_task_holds_a_lesson_and_only_a_memory_arm_starts_with_it():
    """A memory-dependent task means nothing to a Memory that starts empty, and a control
    with an empty Memory would be no control. The plan prints what each run starts with, so
    the seed is fixed before any run: the task's own lesson, or nothing."""
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    arms = ev.arms_by_id(manifest)
    assert [arm for arm, held in arms.items() if not held["seeded_experience"]] == [BASELINE]
    runs = ev.plan_runs("memory_benefit", manifest, prereg)["runs"]
    tasks = ev.tasks_by_id(manifest)
    assert len({run["task"] for run in runs}) == len(tasks) == 12
    for run in runs:
        lesson = tasks[run["task"]]["prior_experience"]["lesson"]
        assert run["prior_experience"] == (lesson if run["arm"] == TREATMENT else None)
        assert lesson not in tasks[run["task"]]["statement"]


@pytest.mark.parametrize(
    "spoil, problem",
    [
        (lambda task: task["prior_experience"].update(lesson=" "), "the lesson is empty"),
        (
            lambda task: task["prior_experience"].update(
                lesson=f"See {ev.hidden_names(task)[0]}, which decides this."
            ),
            "the lesson gives away",
        ),
        (
            lambda task: task["prior_experience"].update(
                lesson=f"The fix was {task['reference_sha'][:12]}."
            ),
            "the lesson gives away",
        ),
        (
            lambda task: task["prior_experience"].update(evidence_sha=task["reference_sha"]),
            "evidence is the reference commit",
        ),
        (
            lambda task: task["prior_experience"].update(evidence_sha="abc"),
            "evidence_sha is no full SHA",
        ),
        (
            lambda task: task["prior_experience"].update(evidence_test="tests/test_engine.py"),
            "evidence_test names no test",
        ),
        (
            lambda task: task["prior_experience"].update(lesson="x" * 2000),
            "more than twice its match's length",
        ),
    ],
    ids=["empty", "hidden-test", "reference-sha", "the-reference", "short-sha", "no-test", "long"],
)
def test_a_lesson_that_could_not_seed_a_memory_is_refused(spoil, problem):
    manifest = ev.load_manifest()
    spoil(manifest["tasks"][0])
    assert any(problem in found for found in ev.manifest_problems(manifest))


def test_an_arm_seeded_otherwise_than_its_memory_says_is_refused():
    manifest = ev.load_manifest()
    ev.arms_by_id(manifest)[BASELINE]["seeded_experience"] = True
    assert any("only an arm with Issue Memory" in p for p in ev.manifest_problems(manifest))


def _history_or_skip() -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("no git history: run `python -m tests.evaluation verify-pins --git .`")
    known = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", ev.load_manifest()["tasks"][0]["base_sha"]],
        capture_output=True,
    )
    if known.returncode != 0:
        pytest.skip("a shallow clone holds no pinned commit")


def test_a_lesson_must_be_earlier_than_its_base_and_must_not_copy_the_fix():
    """Earlier: evidence past the base would be what this very run could teach. Not the
    answer: a line the reference commit added is the fix, whichever words surround it."""
    _history_or_skip()
    task = ev.tasks_by_id(ev.load_manifest())["fix-entry-retarget"]
    assert ev.verify_pin_lesson(str(REPO_ROOT), task) == []

    later = copy.deepcopy(task)
    later["prior_experience"]["evidence_sha"] = task["reference_sha"]
    assert any("not at or before" in p for p in ev.verify_pin_lesson(str(REPO_ROOT), later))

    absent = copy.deepcopy(task)
    absent["prior_experience"]["evidence_test"] = "tests/test_engine.py::test_no_such_test"
    assert any("defines no" in p for p in ev.verify_pin_lesson(str(REPO_ROOT), absent))

    diff = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "-U0", task["base_sha"], task["reference_sha"]]
        + ["--", "src/"],
        capture_output=True,
        text=True,
    ).stdout
    line = next(
        added[1:].strip()
        for added in diff.splitlines()
        if added.startswith("+") and not added.startswith("+++") and len(added.strip()) > 30
    )
    copying = copy.deepcopy(task)
    copying["prior_experience"]["lesson"] += f" Write: {line}"
    assert any("copies the fix" in p for p in ev.verify_pin_lesson(str(REPO_ROOT), copying))


def test_a_run_that_did_not_start_with_its_tasks_lesson_leaves_no_result():
    """Memory that held something else, or an unseeded arm that held anything, is another
    intervention than the one registered: no result, in either direction."""
    manifest, prereg, records = _world()
    next(r for r in records if r["arm"] == TREATMENT)["prior_experience"] = "0" * 64
    other = _decide(manifest, prereg, records)
    assert other["decision"] == ev.NOT_EVALUATED and "prior experience" in other["why"]

    manifest, prereg, records = _world()
    next(r for r in records if r["arm"] == BASELINE)["prior_experience"] = "0" * 64
    assert _decide(manifest, prereg, records)["decision"] == ev.NOT_EVALUATED


# -- what the arms hold equal ----------------------------------------------------------------


def test_every_section_of_the_configuration_is_classified():
    """A new top-level section fails here until someone says whether arms hold it equal."""
    names = {field.name for field in dataclasses.fields(AutoForgeConfig)}
    exempt = {path.partition(".")[0] for path in ev.EXEMPT_SETTINGS if "." not in path}
    assert names == set(ev.EQUALIZED_SECTIONS) | exempt


def test_a_new_section_is_refused_until_it_is_classified():
    extended = dataclasses.make_dataclass(
        "Extended",
        [("memory", dict, dataclasses.field(default_factory=dict))],
        bases=(AutoForgeConfig,),
    )
    with pytest.raises(ev.EvaluationError, match=r"\['memory'\] are neither held equal nor exempt"):
        ev.equalized_settings(extended())


def test_a_new_key_under_validation_is_held_equal_the_day_it_lands():
    """ADR 0010 D11's `validation.fast_commands` moves wall-clock time, attempts and
    BLOCKED interventions, so two arms that differ in it are not comparable. It needs no
    entry here: any key under `validation:` reaches the digest."""
    grown = dataclasses.make_dataclass(
        "Grown",
        [("fast_commands", list, dataclasses.field(default_factory=list))],
        bases=(ValidationConfig,),
    )
    plain, fast = default_config(), default_config()
    plain.validation = grown()
    fast.validation = grown(fast_commands=[["make", "test-fast"]])
    assert ev.settings_digest(plain) != ev.settings_digest(fast)


@pytest.mark.parametrize(
    "change",
    [
        lambda cfg: setattr(cfg.validation, "commands", [["make", "test"]]),
        lambda cfg: setattr(cfg.sandbox, "mode", "required"),
        lambda cfg: setattr(cfg.profiles["analyze_execute"], "model", "another"),
        lambda cfg: setattr(cfg.execution, "default_idle_timeout_seconds", 60),
        lambda cfg: setattr(cfg.workflow, "max_review_rounds", 3),
        lambda cfg: setattr(cfg.review.replan, "enabled", False),
    ],
    ids=["validation", "sandbox", "model", "limit", "loop-bound", "review"],
)
def test_each_setting_the_issue_names_changes_the_digest(change):
    plain, changed = default_config(), default_config()
    change(changed)
    assert ev.settings_digest(plain) != ev.settings_digest(changed)


def test_what_no_run_records_is_each_setting_but_its_routing_and_sandbox_mode():
    """Listed by what a run's files hold, not by what is left out: a key that lands tomorrow is
    unrecorded until something records it, and a profile's options are never recorded."""
    cfg = load_config_file(str(ev.REAL_RUN_CONFIG))
    unrecorded = set(ev.unrecorded_settings(cfg))
    assert {
        "workflow.max_review_rounds",
        "review.replan.enabled",
        "validation.commands",
        "safety.allow_merge",
        "execution.max_correction_attempts",
        "profiles.fix.options.permission_mode",
        "profiles.fix.extra_args",
        "profiles.fix.command",
        "profiles.review_round_1.options.permission_mode",
    } <= unrecorded
    assert not unrecorded & {
        "sandbox.mode",
        "prompt_version",
        "profiles.fix.provider",
        "profiles.fix.model",
        "profiles.fix.effort",
        "profiles.fix.idle_timeout_seconds",
        "profiles.fix.max_runtime_seconds",
        "execution.default_idle_timeout_seconds",
        "execution.default_max_runtime_seconds",
    }
    assert not unrecorded & set(ev.EXEMPT_SETTINGS)
    # A profile with no options still holds the setting, for a run to have set otherwise.
    assert ev._setting_paths({"profiles": {"fix": {"options": {}}}}) == ["profiles.fix.options"]


def test_where_a_host_keeps_things_does_not_change_the_digest():
    plain, moved = default_config(), default_config()
    moved.state_dir = "/elsewhere/state"
    moved.execution.worktree_dir = "/elsewhere/worktrees"
    moved.github.command = "/elsewhere/gh"
    moved.sandbox.read_only_paths = ["/elsewhere/venv"]
    moved.safety.allow_merge_source = "/elsewhere/config.yaml"
    assert ev.settings_digest(plain) == ev.settings_digest(moved)


def test_the_real_run_configuration_is_the_pinned_one_and_safe():
    """The file every real run holds equal: its digest is the manifest's, the merge gate
    closed, the sandbox required, validation not empty and every profile on one provider."""
    cfg = load_config_file(str(ev.REAL_RUN_CONFIG))
    assert ev.settings_digest(cfg) == ev.load_manifest()["equalized_settings"]["digest"]
    assert cfg.safety.allow_merge is False
    assert cfg.sandbox.enabled and cfg.sandbox.mode == "required"
    assert cfg.validation.commands
    assert {profile.provider for profile in cfg.profiles.values()} == {"claude"}


# -- statistics ------------------------------------------------------------------------------

_STATS = {"seed": 318, "resamples": 2000, "confidence": 0.95}


def test_the_interval_is_as_wide_as_the_tasks_make_it_however_many_seeds_they_have():
    """A task's seeds are not independent evidence: a report that treated each run as a
    sample would narrow with every repetition. The rows are per task, so more seeds of
    the same per-task means leave the interval exactly where it was."""
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    tasks = manifest["tasks"][:4]

    def pairs(seeds):
        found = []
        for index, task in enumerate(tasks):
            for seed in seeds:
                base = _record(manifest, prereg, task, seed, BASELINE, verified=False)
                treated = _record(manifest, prereg, task, seed, TREATMENT, verified=index % 2 == 0)
                found.append((base, treated))
        return found

    few, _ = ev._per_task_rows(pairs([1]), "verified_success", "all")
    many, _ = ev._per_task_rows(pairs([1, 2, 3, 4, 5, 6]), "verified_success", "all")
    assert few == many
    assert ev.percentile_interval(few, ev.mean_difference, _STATS) == ev.percentile_interval(
        many, ev.mean_difference, _STATS
    )


def test_the_interval_is_reproducible_from_its_predeclared_seed():
    rows = [(0.0, (i * i % 13) / 13) for i in range(12)]
    first = ev.percentile_interval(rows, ev.mean_difference, _STATS)
    assert first == ev.percentile_interval(rows, ev.mean_difference, _STATS)
    assert first != ev.percentile_interval(rows, ev.mean_difference, {**_STATS, "seed": 319})


def test_a_relative_change_from_nothing_has_no_value():
    assert ev.relative_change([(0.0, 0.0)]) == 0.0
    assert ev.relative_change([(0.0, 1.0)]) is None
    assert ev.relative_change([(2.0, 3.0)]) == 0.5


# -- decisions -------------------------------------------------------------------------------


def test_go_needs_the_effect_every_bound_and_every_value_measured():
    result = _decide(*_world())
    assert result["decision"] == ev.GO, result["why"]
    assert result["primary"]["verdict"] == "pass"
    assert result["primary"]["effect"] == 1.0
    assert result["pairs"] == result["planned_pairs"] == 36
    assert {row["seeds"] for row in result["per_task"]} == {3}


def test_unknown_usage_rules_out_go_and_says_which():
    """Today's real run: no provider's run log reports tokens (#154). The best it can
    reach is INCONCLUSIVE, and a value left out is never read as a pass or as zero."""
    manifest, prereg, records = _world()
    for record in records:
        record["usage"]["context_tokens"] = {"value": None, "provenance": ev.UNKNOWN}
    result = _decide(manifest, prereg, records)
    assert result["decision"] == ev.INCONCLUSIVE
    assert "context_tokens" in result["why"]
    verdicts = {c["metric"]: c for c in result["non_regression"]}
    assert verdicts["context_tokens"]["verdict"] == "unknown"
    assert verdicts["context_tokens"]["provenance"] == {ev.UNKNOWN: 72}


@pytest.mark.parametrize("provenance", [ev.ESTIMATED, ev.SYNTHETIC])
def test_an_estimate_never_passes_a_criterion(provenance):
    """A real outcome and an estimate are different things: an estimated cost is shown
    apart in the report, and the criterion it would have satisfied stays unknown."""
    manifest, prereg, records = _world()
    for record in records:
        record["usage"]["cost_usd"] = {"value": 0.01, "provenance": provenance}
    result = _decide(manifest, prereg, records)
    assert result["decision"] == ev.INCONCLUSIVE
    assert "cost_per_verified_success" in result["why"]
    report = ev.build_report(records, manifest, prereg)
    uncertainty = report["model_quality"]["uncertainty"]
    assert uncertainty["measured_cost_usd"] is None
    if provenance == ev.ESTIMATED:
        assert uncertainty["estimated_cost_usd"] == pytest.approx(0.72)


def test_fixture_runs_reach_no_decision_however_good_they_look():
    """Harness correctness is not model quality: a fixture is never decided over, and a
    report keeps the two apart."""
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    records = [
        _record(manifest, prereg, task, seed, arm, evidence=ev.FIXTURE, verified=arm == TREATMENT)
        for task in manifest["tasks"]
        for seed in prereg["sample"]["seeds"]
        for arm in (BASELINE, TREATMENT)
    ]
    result = _decide(manifest, prereg, records)
    assert result["decision"] == ev.NOT_EVALUATED
    assert result["why"] == "no real run has been made"
    report = ev.build_report(records, manifest, prereg)
    assert report["model_quality"]["real_runs"] == 0
    assert len(report["harness_correctness"]["runs"]) == len(records)
    assert "no evidence of model quality" in report["harness_correctness"]["says"]


def test_a_missing_arm_is_not_evaluated_and_never_blocks():
    """Today's manifest: arm 2 needs #317 and #333. It is reported, nothing raises, and
    every contrast that needs a missing arm reads the same way."""
    manifest, prereg = ev.load_manifest(), ev.load_preregistration()
    report = ev.build_report([], manifest, prereg)
    for contrasts in report["model_quality"]["comparisons"].values():
        for contrast in contrasts:
            assert contrast["decision"] == ev.NOT_EVALUATED
            assert "needs #" in contrast["why"]
    assert report["model_quality"]["comparisons"]["memory_benefit"][0]["why"] == (
        "single_agent_issue_memory needs #317, #333"
    )


@pytest.mark.parametrize(
    "harm, needle",
    [
        ("safety", "safety_failures"),
        ("stale", "stale_memory_leaks"),
        ("regression", "verified_success"),
        ("lost", "lost_success"),
    ],
)
def test_evidence_of_harm_is_no_go_however_large_the_benefit(harm, needle):
    manifest, prereg, records = _world()
    independents = {t["id"] for t in manifest["tasks"] if t["memory_class"] == "memory_independent"}
    for record in records:
        if record["arm"] != TREATMENT:
            continue
        if harm == "safety":
            record["safety_failures"] = ["an agent merged"]
        elif harm == "stale":
            record["memory"]["stale_leaks"]["value"] = 1
        elif harm == "regression" and record["task"] in independents:
            record["outcome"] = ev.UNVERIFIED
        elif harm == "lost":
            record["counts"]["lost_success"] = 1
    result = _decide(manifest, prereg, records)
    assert result["decision"] == ev.NO_GO
    assert needle in result["why"]


def test_memory_over_its_ceiling_is_no_go_and_unmeasured_overhead_rules_out_go():
    """The issue measures Memory's overhead, so a Memory arm owes it: past a pre-registered
    ceiling is harm, and an arm that reports none cannot pass."""
    manifest, prereg, records = _world()
    ceiling = prereg["ceilings"]["memory_overhead"]["projection_chars_per_launch"]
    treated = [record for record in records if record["arm"] == TREATMENT]
    treated[0]["memory"]["projection_chars"]["value"] = ceiling
    assert _decide(manifest, prereg, records)["decision"] == ev.GO
    treated[0]["memory"]["projection_chars"]["value"] = ceiling + 1
    over = _decide(manifest, prereg, records)
    assert over["decision"] == ev.NO_GO and "memory_overhead" in over["why"]

    treated[0]["memory"]["projection_chars"] = {"value": None, "provenance": ev.NOT_APPLICABLE}
    unmeasured = _decide(manifest, prereg, records)
    assert unmeasured["decision"] == ev.INCONCLUSIVE and "memory_overhead" in unmeasured["why"]


def test_no_effect_is_no_go_and_a_doubtful_one_is_inconclusive():
    no_effect = _decide(*_world(treated_verified=lambda task, seed: False))
    assert no_effect["decision"] == ev.NO_GO
    assert "smallest effect" in no_effect["why"]

    doubtful = _decide(
        *_world(treated_verified=lambda task, seed: task["id"] == "fix-entry-retarget")
    )
    assert doubtful["primary"]["effect"] == pytest.approx(1 / 6)
    assert doubtful["primary"]["verdict"] == "inconclusive"
    assert doubtful["decision"] == ev.INCONCLUSIVE


def _one_pair(manifest: dict, prereg: dict, *, treated_verified: bool, **treated_counts) -> list:
    """The first memory-dependent task's first seed, on both arms, and nothing else."""
    task = next(t for t in manifest["tasks"] if t["memory_class"] == "memory_dependent")
    return [
        _record(manifest, prereg, task, 1, BASELINE, verified=False),
        _record(manifest, prereg, task, 1, TREATMENT, verified=treated_verified, **treated_counts),
    ]


def test_an_incomplete_sample_reaches_no_primary_conclusion():
    """One pair without an effect and 35 with no record say nothing of the effect: the
    registered rule is that a planned pair with no record leaves the comparison inconclusive,
    and it comes before the primary is read, or stopping early could decide it."""
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    result = _decide(manifest, prereg, _one_pair(manifest, prereg, treated_verified=False))
    assert result["primary"]["verdict"] == "fail"
    assert len(result["missing"]) == 35
    assert result["decision"] == ev.INCONCLUSIVE
    assert "35 planned pair(s) have no record" in result["why"]


def test_too_many_exclusions_reach_no_primary_conclusion():
    manifest, prereg, records = _world(treated_verified=lambda task, seed: False)
    tasks = ev.tasks_by_id(manifest)
    swept = [
        ev.not_run_record(
            tasks[record["task"]],
            TREATMENT,
            record["seed"],
            "infra_failure",
            "the host went away",
            ev.digest(manifest),
            ev.digest(prereg),
        )
        if record["arm"] == TREATMENT and record["memory_class"] == "memory_independent"
        else record
        for record in records
    ]
    result = _decide(manifest, prereg, swept)
    assert result["primary"]["verdict"] == "fail"
    assert result["decision"] == ev.INCONCLUSIVE and "over the bound" in result["why"]


def test_observed_harm_still_decides_an_incomplete_sample():
    """What was seen to go wrong does not wait for the rest of the plan: a safety failure
    or a value wholly past its margin is NO_GO however few pairs there are."""
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    unsafe = _one_pair(manifest, prereg, treated_verified=True)
    unsafe[1]["safety_failures"] = ["an agent merged"]
    result = _decide(manifest, prereg, unsafe)
    assert result["decision"] == ev.NO_GO and "safety_failures" in result["why"]
    harmed = _decide(
        manifest, prereg, _one_pair(manifest, prereg, treated_verified=True, lost_success=1)
    )
    assert harmed["decision"] == ev.NO_GO and "lost_success" in harmed["why"]


def test_two_records_of_one_planned_run_leave_no_result_whichever_comes_first():
    """Keeping the last record would let the order of the arguments choose an arm's score,
    and let a failed attempt and its cost vanish. Nobody may choose between them afterwards,
    and neither disappears from the report."""
    manifest, prereg, records = _world()
    planned = next(r for r in records if r["arm"] == TREATMENT and r["outcome"] == ev.VERIFIED)
    failed = copy.deepcopy(planned)
    failed["outcome"] = "failed"
    first = _decide(manifest, prereg, [failed, *records])
    second = _decide(manifest, prereg, [*records, failed])
    for result in (first, second):
        assert result["decision"] == ev.NOT_EVALUATED
        assert "1 planned run(s) have more than one record" in result["why"]
        assert result["repeated_runs"] == [
            {
                "arm": TREATMENT,
                "task": planned["task"],
                "seed": planned["seed"],
                "outcomes": ["failed", ev.VERIFIED],
            }
        ]
    report = ev.build_report([failed, *records], manifest, prereg, harness_report=_COVERED)
    assert failed in report["model_quality"]["runs"] and planned in report["model_quality"]["runs"]
    assert f"repeated {TREATMENT} on {planned['task']}" in ev.render(report)


def test_a_repeat_of_another_contrasts_arm_does_not_touch_this_one():
    manifest, prereg, records = _world()
    other = copy.deepcopy(records[0])
    other["arm"] = "fixed_role_multi_agent_issue_memory"
    result = _decide(manifest, prereg, [*records, other, other])
    assert result["decision"] == ev.GO


@pytest.mark.parametrize("ceiling", ev.CEILING_OUTCOMES)
def test_a_run_stopped_at_a_ceiling_is_inconclusive_and_keeps_what_it_spent(ceiling):
    """As registered: a ceiling reached leaves the comparison incomplete. The pair stays, as
    a run that did not succeed, with its settings and what it cost."""
    manifest, prereg, records = _world()
    stopped = next(r for r in records if r["arm"] == TREATMENT)
    stopped["outcome"] = ceiling
    stopped["stopped_reason"] = "the bill passed the ceiling"
    stopped["usage"]["cost_usd"]["value"] = 12.5
    result = _decide(manifest, prereg, records)
    assert result["decision"] == ev.INCONCLUSIVE and "reached a ceiling" in result["why"]
    assert result["pairs"] == 36 and result["excluded"] == []
    assert result["ceiling_stopped"] == [
        {
            "arm": TREATMENT,
            "task": stopped["task"],
            "seed": stopped["seed"],
            "ceiling": ceiling,
            "cost_usd": 12.5,
            "wall_clock_seconds": 1.0,
            "reason": "the bill passed the ceiling",
        }
    ]
    report = ev.build_report(records, manifest, prereg, harness_report=_COVERED)
    rendered = ev.render(report)
    assert f"{stopped['task']} seed {stopped['seed']} {TREATMENT}: {ceiling}" in rendered
    assert "cost 12.5 (measured)" in rendered and "the bill passed the ceiling" in rendered


@pytest.mark.parametrize("ceiling", ev.CEILING_OUTCOMES)
def test_a_run_never_started_for_a_ceiling_is_inconclusive_never_unequal_settings(ceiling):
    """No state directory, so nothing is known of its settings. That is no inequality: it
    must not turn an incomplete comparison into one that broke its own terms."""
    manifest, prereg, records = _world()
    planned = next(r for r in records if r["arm"] == TREATMENT)
    task = ev.tasks_by_id(manifest)[planned["task"]]
    replaced = ev.not_run_record(
        task,
        TREATMENT,
        planned["seed"],
        ceiling,
        "the comparison's ceiling was reached",
        ev.digest(manifest),
        ev.digest(prereg),
    )
    records[records.index(planned)] = replaced
    result = _decide(manifest, prereg, records)
    assert result["decision"] == ev.INCONCLUSIVE
    assert "reached a ceiling" in result["why"]
    assert result["ceiling_stopped"][0]["cost_usd"] is None


@pytest.mark.parametrize(
    "spoil",
    [
        lambda r: r["usage"]["cost_usd"].update(value=26.0),
        lambda r: r["usage"]["cost_usd"].update(value=None, provenance=ev.UNKNOWN, known_part=26.0),
        lambda r: r["volatile"]["wall_clock_seconds"].update(value=8 * 3600 + 1),
    ],
    ids=["cost", "known-part-of-a-cost", "wall-clock"],
)
def test_a_run_measured_past_a_ceiling_is_inconclusive_though_nobody_said_so(spoil):
    manifest, prereg, records = _world()
    spoil(next(r for r in records if r["arm"] == TREATMENT))
    result = _decide(manifest, prereg, records)
    assert result["decision"] == ev.INCONCLUSIVE and "reached a ceiling" in result["why"]


def test_harm_outweighs_a_ceiling():
    manifest, prereg, records = _world()
    stopped = next(r for r in records if r["arm"] == TREATMENT)
    stopped["outcome"] = "cost_ceiling"
    stopped["safety_failures"] = ["an agent merged"]
    assert _decide(manifest, prereg, records)["decision"] == ev.NO_GO


def test_an_unassessed_safety_gate_is_unknown_and_rules_out_go():
    """An empty list of safety failures may only mean nobody looked: the gate needs someone
    to say they looked."""
    manifest, prereg, records = _world()
    next(r for r in records if r["arm"] == TREATMENT)["safety_assessed"] = False
    result = _decide(manifest, prereg, records)
    gate = next(g for g in result["hard_gates"] if g["gate"] == "safety_failures")
    assert gate["verdict"] == "unknown" and "1 run(s)" in gate["why"]
    assert result["decision"] == ev.INCONCLUSIVE and "safety_failures" in result["why"]


def test_a_found_safety_failure_is_no_go_whether_or_not_the_rest_was_assessed():
    manifest, prereg, records = _world()
    record = next(r for r in records if r["arm"] == TREATMENT)
    record["safety_assessed"], record["safety_failures"] = False, ["an agent merged"]
    assert _decide(manifest, prereg, records)["decision"] == ev.NO_GO


def _treated(records: list[dict]) -> dict:
    return next(r for r in records if r["arm"] == TREATMENT)


def _baseline_of(records: list[dict], treated: dict) -> dict:
    return next(
        r
        for r in records
        if r["arm"] == BASELINE and (r["task"], r["seed"]) == (treated["task"], treated["seed"])
    )


def _exclude(records: list[dict]) -> list[dict]:
    treated = _treated(records)
    treated["outcome"], treated["stopped_reason"] = "service_unavailable", "the API was down"
    return records


def _exclude_its_baseline(records: list[dict]) -> list[dict]:
    baseline = _baseline_of(records, _treated(records))
    baseline["outcome"], baseline["stopped_reason"] = "infra_failure", "the host went away"
    return records


def _lose_its_baseline(records: list[dict]) -> list[dict]:
    baseline = _baseline_of(records, _treated(records))
    return [record for record in records if record is not baseline]


_LEAVE_THE_PAIR = {
    "excluded": _exclude,
    "excluded-baseline": _exclude_its_baseline,
    "unpaired": _lose_its_baseline,
}


@pytest.mark.parametrize("leave", _LEAVE_THE_PAIR.values(), ids=_LEAVE_THE_PAIR)
@pytest.mark.parametrize(
    "harm, gate",
    [
        (lambda r: r.update(safety_failures=["an agent merged"]), "safety_failures"),
        (lambda r: r["memory"]["stale_leaks"].update(value=1), "stale_memory_leaks"),
        (lambda r: r["memory"]["storage_bytes"].update(value=2**40), "memory_overhead"),
    ],
    ids=["safety", "stale", "overhead"],
)
def test_harm_seen_on_a_run_no_pair_holds_is_still_no_go(leave, harm, gate):
    """Leaving a pair takes a run out of the statistics, not out of what was seen. An excluded
    outcome or a lost baseline cannot turn what went wrong into a clean gate."""
    manifest, prereg, records = _world()
    harm(_treated(records))
    result = _decide(manifest, prereg, leave(records))
    assert result["pairs"] == 35
    verdicts = {g["gate"]: g["verdict"] for g in result["hard_gates"]}
    assert verdicts[gate] == "fail"
    assert result["decision"] == ev.NO_GO and gate in result["why"]


def test_harm_is_no_go_with_no_pair_at_all():
    """There is no primary to read, and what happened still happened."""
    manifest, prereg, records = _world()
    treated = _treated(records)
    treated["safety_failures"] = ["an agent merged"]
    result = _decide(manifest, prereg, [treated])
    assert result["pairs"] == 0 and result["primary"] is None
    assert result["decision"] == ev.NO_GO and "safety_failures" in result["why"]

    treated["safety_failures"] = []
    clean = _decide(manifest, prereg, [treated])
    assert clean["decision"] == ev.NOT_EVALUATED and "no pair" in clean["why"]


def test_an_excluded_run_that_left_evidence_still_owes_its_safety_check():
    """An exclusion cannot be what makes a run's check unnecessary. A planned run that never
    started has nothing to check, which test_exclusions_are_listed holds."""
    manifest, prereg, records = _world()
    _treated(records)["safety_assessed"] = False
    result = _decide(manifest, prereg, _exclude(records))
    gate = next(g for g in result["hard_gates"] if g["gate"] == "safety_failures")
    assert gate["verdict"] == "unknown" and "1 run(s)" in gate["why"]
    assert result["decision"] == ev.INCONCLUSIVE and "safety_failures" in result["why"]


def test_a_smoke_runs_harm_stays_outside_every_decision():
    """A gate reads the plan's runs. The smoke run's record says what happened, on its own."""
    manifest, prereg, records = _world()
    smoke = copy.deepcopy(_treated(records))
    smoke["seed"], smoke["safety_failures"] = ev.SMOKE_SEED, ["an agent merged"]
    assert _decide(manifest, prereg, [*records, smoke])["decision"] == ev.GO


_UNKNOWN_MEASURE = {"value": None, "provenance": ev.UNKNOWN}


@pytest.mark.parametrize(
    "known, unknown, other_run, gate",
    [
        ("stale_leaks", "stale_leaks", 1, "stale_memory_leaks"),
        ("projection_chars", "storage_bytes", 1, "memory_overhead"),
        ("projection_chars", "storage_bytes", 0, "memory_overhead"),
        ("latency_ms", "latency_ms", 1, "memory_overhead"),
    ],
    ids=["stale-elsewhere", "projection-elsewhere", "projection-same-run", "latency-elsewhere"],
)
def test_a_measured_breach_fails_its_gate_whatever_else_is_unknown(known, unknown, other_run, gate):
    """What is not known of another value leaves this one past its ceiling."""
    manifest, prereg, records = _world()
    treated = [record for record in records if record["arm"] == TREATMENT]
    treated[0]["memory"][known]["value"] = 10**9
    treated[other_run]["memory"][unknown] = dict(_UNKNOWN_MEASURE)
    result = _decide(manifest, prereg, records)
    verdicts = {g["gate"]: g["verdict"] for g in result["hard_gates"]}
    assert verdicts[gate] == "fail"
    assert result["decision"] == ev.NO_GO and gate in result["why"]


@pytest.mark.parametrize(
    "key, gate",
    [("stale_leaks", "stale_memory_leaks"), ("storage_bytes", "memory_overhead")],
)
def test_an_unknown_measure_with_no_breach_stays_unknown(key, gate):
    """No breach is established, and neither is a clean gate."""
    manifest, prereg, records = _world()
    _treated(records)["memory"][key] = dict(_UNKNOWN_MEASURE)
    result = _decide(manifest, prereg, records)
    verdicts = {g["gate"]: g["verdict"] for g in result["hard_gates"]}
    assert verdicts[gate] == "unknown"
    assert result["decision"] == ev.INCONCLUSIVE and gate in result["why"]


@pytest.mark.parametrize(
    "value",
    [None, "1", True, -1, float("nan"), float("inf")],
    ids=["null", "text", "bool", "negative", "nan", "infinity"],
)
@pytest.mark.parametrize(
    "key, gate",
    [("stale_leaks", "stale_memory_leaks"), ("projection_chars", "memory_overhead")],
)
def test_a_measured_value_that_is_no_amount_is_no_measurement(key, gate, value):
    """Said to be measured and not a possible amount: it neither crashes a gate nor clears it,
    and a negative one offsets nothing."""
    manifest, prereg, records = _world()
    treated = [record for record in records if record["arm"] == TREATMENT]
    treated[0]["memory"][key]["value"] = value
    result = _decide(manifest, prereg, records)
    verdicts = {g["gate"]: g["verdict"] for g in result["hard_gates"]}
    assert verdicts[gate] == "unknown"
    assert result["decision"] == ev.INCONCLUSIVE

    treated[1]["memory"][key]["value"] = 10**9
    assert _decide(manifest, prereg, records)["decision"] == ev.NO_GO


@pytest.mark.parametrize(
    "metric", ["review_fix_cycles", "repeated_failed_approaches", "human_blocked_interventions"]
)
def test_a_lost_launch_line_leaves_its_counts_unknown_and_rules_out_go(metric):
    """A count from a log that lost a launch may be short. It reads as unknown, never as the
    zero it would be, and the run's log is named in the decision."""
    manifest, prereg, records = _world()
    lost = next(r for r in records if r["arm"] == TREATMENT)
    lost["journal_problems"] = ["1 step(s) have no line in the run log, 003-review-1 first"]
    lost["unknown_counts"] = sorted(ev.JOURNAL_COUNTS)
    result = _decide(manifest, prereg, records)
    verdicts = {c["metric"]: c for c in result["non_regression"]}
    assert verdicts[metric]["verdict"] == "unknown"
    assert verdicts[metric]["provenance"][ev.UNKNOWN] == 1
    assert result["decision"] == ev.INCONCLUSIVE and "run(s) is incomplete" in result["why"]
    report = ev.build_report(records, manifest, prereg, harness_report=_COVERED)
    uncertainty = report["model_quality"]["uncertainty"]
    assert uncertainty["incomplete_run_logs"] == 1
    assert uncertainty["unknown_counts"][metric] == 1


@pytest.mark.parametrize(
    "status, decision, why",
    [
        ("unknown", ev.INCONCLUSIVE, "settings of 1 run(s) are unknown"),
        ("mismatch", ev.NOT_EVALUATED, "other settings than its record states"),
    ],
)
def test_settings_a_run_cannot_show_rule_out_go(status, decision, why):
    manifest, prereg, records = _world()
    next(r for r in records if r["arm"] == TREATMENT)["settings_evidence"] = {
        "status": status,
        "problems": ["the run recorded no configuration"],
    }
    result = _decide(manifest, prereg, records)
    assert result["decision"] == decision and why in result["why"]


def test_a_leak_nobody_could_rule_out_rules_out_go():
    manifest, prereg, records = _world()
    next(r for r in records if r["arm"] == BASELINE)["unknown_counts"] = ["hidden_leaks"]
    result = _decide(manifest, prereg, records)
    assert result["decision"] == ev.INCONCLUSIVE and "run(s) is incomplete" in result["why"]


def test_a_run_that_failed_stays_in_the_denominator():
    """No cherry-picking: a BLOCKED or timed-out run counts as what it is, a run that did
    not succeed. Only who stopped it from outside leaves the denominator."""
    manifest, prereg, records = _world()
    for record in records:
        if record["arm"] == TREATMENT:
            record["outcome"] = "blocked"
    result = _decide(manifest, prereg, records)
    assert result["pairs"] == 36 and result["excluded"] == []
    assert result["primary"]["effect"] == 0.0
    assert result["decision"] == ev.NO_GO


def test_exclusions_are_listed_and_too_many_are_inconclusive():
    manifest, prereg, records = _world()
    tasks = {t["id"]: t for t in manifest["tasks"]}
    within = records[:]
    target = next(r for r in within if r["arm"] == TREATMENT and r["seed"] == 1)
    within[within.index(target)] = ev.not_run_record(
        tasks[target["task"]],
        TREATMENT,
        1,
        "service_unavailable",
        "the provider's API was down",
        ev.digest(manifest),
        ev.digest(prereg),
    )
    result = _decide(manifest, prereg, within)
    assert result["pairs"] == 35
    assert result["excluded"] == [
        {
            "task": target["task"],
            "seed": 1,
            "baseline": ev.VERIFIED
            if target["memory_class"] != "memory_dependent"
            else ev.UNVERIFIED,
            "treatment": "service_unavailable",
            "reason": "the provider's API was down",
        }
    ]
    assert result["decision"] == ev.GO

    swept = []
    for record in records:
        if record["arm"] == TREATMENT and record["seed"] != 3:
            swept.append(
                ev.not_run_record(
                    tasks[record["task"]],
                    TREATMENT,
                    record["seed"],
                    "infra_failure",
                    "the host went away",
                    ev.digest(manifest),
                    ev.digest(prereg),
                )
            )
        else:
            swept.append(record)
    over = _decide(manifest, prereg, swept)
    assert over["decision"] == ev.INCONCLUSIVE
    assert "excluded, over the bound" in over["why"]


def test_a_pair_with_no_record_is_inconclusive_never_a_smaller_sample():
    manifest, prereg, records = _world()
    dropped = records[1:]
    result = _decide(manifest, prereg, dropped)
    assert result["decision"] == ev.INCONCLUSIVE
    assert len(result["missing"]) == 1


@pytest.mark.parametrize(
    "spoil, why",
    [
        (lambda r: r.update(preregistration_digest="0" * 64), "another pre-registration"),
        (lambda r: r.update(manifest_digest="0" * 64), "another manifest"),
        (lambda r: r.update(settings_digest="0" * 64), "settings are not equal"),
        (lambda r: r["counts"].update(hidden_leaks=1), "hidden test reached an agent"),
    ],
    ids=["preregistration", "manifest", "settings", "leak"],
)
def test_a_comparison_that_broke_its_own_terms_is_not_evaluated(spoil, why):
    """Criteria moved after the fact, unequal settings or a leaked test leave no result
    to report, in either direction."""
    manifest, prereg, records = _world()
    spoil(next(r for r in records if r["arm"] == TREATMENT))
    result = _decide(manifest, prereg, records)
    assert result["decision"] == ev.NOT_EVALUATED
    assert why in result["why"]


def test_the_crash_gate_needs_a_fixture_report_of_the_arm():
    """Crash recovery is a deterministic test on fakes; a real pair cannot be made to
    crash at a chosen window. No report covering the arm leaves the gate unknown."""
    manifest, prereg, records = _world()
    assert _decide(manifest, prereg, records, harness=None)["decision"] == ev.INCONCLUSIVE
    failing = {"harness_correctness": {"arms_covered": [TREATMENT], "status": "fail"}}
    assert _decide(manifest, prereg, records, harness=failing)["decision"] == ev.NO_GO


# -- records and the command line ------------------------------------------------------------


def test_a_fixture_record_may_not_say_it_measured_anything():
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    task = manifest["tasks"][0]
    record = _record(manifest, prereg, task, 1, BASELINE, evidence=ev.FIXTURE)
    assert ev.record_problems(record) == []
    record["usage"]["cost_usd"]["provenance"] = ev.MEASURED
    assert ev.record_problems(record) == ["usage.cost_usd of a fixture run says measured"]
    with pytest.raises(ev.EvaluationError, match="says measured"):
        ev.build_report([record], manifest, prereg)


def test_a_record_that_leaves_something_unsaid_is_refused_never_read_as_clean():
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    record = _record(manifest, prereg, manifest["tasks"][0], 1, BASELINE)
    del record["safety_assessed"], record["unknown_counts"]
    problems = ev.record_problems(record)
    assert "safety_assessed is not stated" in problems
    assert "unknown_counts is not stated" in problems
    with pytest.raises(ev.EvaluationError, match="safety_assessed is not stated"):
        ev.build_report([record], manifest, prereg)


@pytest.mark.parametrize(
    "field, value, problem",
    [
        ("evidence", "guess", "evidence 'guess'"),
        ("outcome", "passed", "outcome 'passed'"),
        ("schema", "other", f"schema is not {ev.RUN_SCHEMA}"),
    ],
)
def test_a_malformed_record_is_refused(field, value, problem):
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    record = _record(manifest, prereg, manifest["tasks"][0], 1, BASELINE)
    record[field] = value
    assert problem in ev.record_problems(record)


@pytest.mark.parametrize("measure", [None, "a string", {"value": 0}, {"provenance": "guess"}])
def test_a_memory_measure_of_no_known_provenance_is_refused(measure):
    """A gate reads every planned treatment run's, excluded ones too."""
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    record = _record(manifest, prereg, manifest["tasks"][0], 1, TREATMENT)
    assert ev.record_problems(record) == []
    record["memory"]["stale_leaks"] = measure
    assert ev.record_problems(record) == ["memory.stale_leaks has no known provenance"]


def test_a_run_that_did_not_finish_says_why():
    manifest = ev.load_manifest()
    task = manifest["tasks"][0]
    with pytest.raises(ev.EvaluationError, match="stated reason"):
        ev.not_run_record(task, BASELINE, 1, "skipped", " ", "m", "p")
    with pytest.raises(ev.EvaluationError, match="--stopped-by"):
        ev.not_run_record(task, BASELINE, 1, "because", "x", "m", "p")
    never_run = ev.not_run_record(task, BASELINE, 1, "cost_ceiling", "spent", "m", "p")
    assert never_run["usage"]["cost_usd"] == {"value": None, "provenance": ev.UNKNOWN}
    assert never_run["run_evidence"] is False and never_run["settings_digest"] == ""
    assert never_run["settings_evidence"]["status"] == "unknown"
    assert set(never_run["unknown_counts"]) == set(ev.COUNTS)
    assert never_run["safety_assessed"] is False
    assert ev.record_problems(never_run) == []


def _collect_args(out: Path, **overrides) -> list[str]:
    values = {
        "--task": "fix-entry-retarget",
        "--arm": BASELINE,
        "--seed": "1",
        "--out": str(out),
        "--approval": "https://github.com/iceboundrock/AutoForge/issues/318#issuecomment-1",
        "--stopped-by": "skipped",
        "--reason": "not reached",
        **overrides,
    }
    args = ["collect"]
    for name, value in values.items():
        if value:
            args += [name, value]
    return args


def test_collect_needs_the_opt_in_and_a_recorded_approval(tmp_path, monkeypatch, capsys):
    """Scoring a real run is the operator's act, separately approved. Nothing is written
    until both are given."""
    out = tmp_path / "record.json"
    monkeypatch.delenv(ev.OPT_IN, raising=False)
    assert ev.main(_collect_args(out)) == 2
    assert ev.OPT_IN in capsys.readouterr().out
    assert not out.exists()

    monkeypatch.setenv(ev.OPT_IN, "true")
    assert ev.main(_collect_args(out)) == 2
    assert not out.exists()

    monkeypatch.setenv(ev.OPT_IN, "1")
    assert ev.main(_collect_args(out, **{"--approval": " "})) == 2
    assert "--approval" in capsys.readouterr().out
    assert not out.exists()

    assert ev.main(_collect_args(out, **{"--seed": "9"})) == 2
    assert ev.main(_collect_args(out, **{"--task": "no-such-task"})) == 2
    assert not out.exists()

    assert ev.main(_collect_args(out)) == 0
    record = ev.read_json(out)
    assert record["outcome"] == "skipped" and record["evidence"] == ev.REAL
    assert record["approval"].endswith("issuecomment-1")


def test_collect_says_what_a_finished_run_needs_and_refuses_a_state_it_cannot_load(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(ev.OPT_IN, "1")
    out = tmp_path / "record.json"
    finished = {"--stopped-by": "", "--reason": ""}
    assert ev.main(_collect_args(out, **finished)) == 2
    said = capsys.readouterr().out
    assert "--state-dir" in said and "--oracle-repo" in said

    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text("{not json", encoding="utf-8")
    inputs = {
        **finished,
        "--state-dir": str(tmp_path / "state"),
        "--repo": str(tmp_path),
        "--base-commit": "0" * 40,
        "--oracle-repo": str(tmp_path),
    }
    assert ev.main(_collect_args(out, **inputs)) == 2
    assert "collect refuses" in capsys.readouterr().out
    assert not out.exists()


def test_a_smoke_runs_seed_is_scored_and_reaches_no_decision(tmp_path, monkeypatch):
    """The smoke run proves the procedure: its record has its own seed, outside the plan."""
    monkeypatch.setenv(ev.OPT_IN, "1")
    out = tmp_path / "smoke.json"
    assert ev.main(_collect_args(out, **{"--seed": str(ev.SMOKE_SEED)})) == 0
    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    record = ev.read_json(out)
    record["manifest_digest"] = ev.digest(manifest)
    result = _decide(manifest, prereg, [record])
    assert result["pairs"] == 0 and len(result["missing"]) == 36


def test_report_takes_records_and_prints_what_it_cannot_say(tmp_path, capsys):
    manifest, prereg = ev.load_manifest(), ev.load_preregistration()
    record = _record(manifest, prereg, manifest["tasks"][0], 1, BASELINE, verified=False)
    record["outcome"] = "blocked"
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert ev.main(["report", str(path)]) == 0
    out = capsys.readouterr().out
    assert "MODEL QUALITY (real runs: 1)" in out
    assert "not verified 1" in out
    assert "NOT_EVALUATED (single_agent_issue_memory needs #317, #333)" in out
    assert "Memdoor" in out and "Databricks" in out
    assert ev.main(["report", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == ev.REPORT_SCHEMA


def test_the_plan_launches_nothing_and_is_fixed_before_any_run(capsys, monkeypatch):
    """The plan names what would run and what cannot, in an order fixed by a digest of the
    pair, so that no arm always runs first."""

    def no_process(*args, **kwargs):
        raise AssertionError("the plan started a process")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    assert ev.main(["plan", "--comparison", "memory_benefit"]) == 0
    today = json.loads(capsys.readouterr().out)
    assert {run["arm"] for run in today["runs"]} == {BASELINE}
    assert today["not_evaluated"] == {TREATMENT: [317, 333]}
    assert len(today["runs"]) == 36

    manifest, prereg = _manifest_with_memory(), ev.load_preregistration()
    later = ev.plan_runs("memory_benefit", manifest, prereg)
    assert later == ev.plan_runs("memory_benefit", manifest, prereg)
    firsts = [later["runs"][i]["arm"] for i in range(0, len(later["runs"]), 2)]
    assert set(firsts) == {BASELINE, TREATMENT}
    with pytest.raises(SystemExit):
        ev.main(["plan", "--comparison", "nothing"])


@pytest.mark.parametrize(
    "path, removed, new, altered",
    [
        ("tests/conftest.py", 0, False, True),
        ("tests/fixtures/state_protocol_12/a.json", 0, False, True),
        ("tests/fixtures/new.json", 0, True, True),
        ("conftest.py", 0, True, True),
        ("pytest.ini", 0, True, True),
        ("setup.cfg", 0, True, True),
        ("pyproject.toml", 1, False, True),
        ("Makefile", 0, False, True),
        (".github/workflows/ci.yml", 1, False, True),
        ("tests/test_never_hidden.py", 1, False, True),
        ("tests/test_never_hidden.py", None, False, True),
        ("tests/claude_fake.py", 0, False, True),
        ("tests/sub/conftest.py", 0, True, True),
        ("tests/test_never_hidden.py", 0, False, False),
        ("tests/test_a_new_module.py", 0, True, False),
        ("tests/AGENTS.md", 2, False, False),
        ("src/autoforge/engine.py", 40, False, False),
        ("docs/usage.md", 3, False, False),
    ],
)
def test_every_real_task_holds_its_checkers(path, removed, new, altered):
    """Against the real manifest: what decides validation may not change, whichever task it
    is. A new test or a line added to a test module is what a good candidate does, so those
    fall to the pristine suite, which sees what an added line can do."""
    for task in ev.load_manifest()["tasks"]:
        change = ev.FileChange(path, removed, new)
        assert ev.checker_alterations([change], task) == ([path] if altered else []), task["id"]


def test_an_edit_to_a_file_the_reference_edited_is_left_to_the_pristine_suite():
    """A correct candidate may have to change an existing assertion the reference changed
    too, so a text rule cannot refuse it. Conftest stays protected even for the one task whose
    reference edited it: the overlay lays that copy over what the hidden tests see."""
    task = ev.tasks_by_id(ev.load_manifest())["google-api-key-redaction"]
    edit = ev.FileChange("tests/test_redaction.py", 3, False)
    assert ev.checker_alterations([edit], task) == []
    conftest = ev.FileChange("tests/conftest.py", 0, False)
    assert ev.checker_alterations([conftest], task) == ["tests/conftest.py"]


def test_a_task_that_leaves_a_checker_unprotected_is_refused():
    manifest = ev.load_manifest()
    manifest["tasks"][0]["protected_paths"].remove("tests/fixtures/")
    assert any("protected_paths leaves out" in p for p in ev.manifest_problems(manifest))


def test_the_fixtures_protect_what_the_real_tasks_protect():
    """The negative fixtures exercise the rule a real task is held to, not one of their own."""
    fixtures = ev.read_json(ev.FIXTURES / "fixture_tasks.json")
    for name, base in fixtures["bases"].items():
        assert base["protected_paths"] == list(ev.PROTECTED_PATHS), name
    assert {tuple(t["protected_paths"]) for t in ev.load_manifest()["tasks"]} == {
        ev.PROTECTED_PATHS
    }


@pytest.mark.parametrize(
    "changed, protected, expected",
    [
        (["src/a.py"], ["tests/conftest.py"], False),
        (["tests/conftest.py"], ["tests/conftest.py"], True),
        ([".github/workflows/ci.yml"], [".github/"], True),
        ([".github-notes"], [".github/"], False),
    ],
)
def test_a_protected_path_is_an_exact_path_or_a_directory(changed, protected, expected):
    assert ev.touches_protected(changed, protected) is expected


def test_the_documented_report_publishes_every_real_run_and_every_tasks_uncertainty(
    capsys, tmp_path, monkeypatch
):
    """The command the protocol says to post in full: each run's outcome, reasons and usage
    provenance, each criterion's interval, what left the sample, and every task, including one
    with no pair at all. Failures are in it, not only the summary counts."""
    manifest, prereg, records = _world()
    tasks = ev.tasks_by_id(manifest)
    failed = next(r for r in records if r["arm"] == TREATMENT and r["seed"] == 1)
    failed["outcome"], failed["unverified_reasons"] = ev.UNVERIFIED, ["oracle_failed"]
    failed["usage"]["context_tokens"] = {"value": None, "provenance": ev.UNKNOWN}
    failed["usage"]["cost_usd"] = {"value": None, "provenance": ev.UNKNOWN, "known_part": 3.5}
    victim = next(r for r in records if r["arm"] == TREATMENT and r["seed"] == 2)
    excluded = ev.not_run_record(
        tasks[victim["task"]],
        TREATMENT,
        2,
        "service_unavailable",
        "the API was down",
        ev.digest(manifest),
        ev.digest(prereg),
    )
    records[records.index(victim)] = excluded
    gone = next(
        t for t in manifest["tasks"] if t["id"] != failed["task"] and t["id"] != victim["task"]
    )
    records = [r for r in records if not (r["task"] == gone["id"] and r["arm"] == BASELINE)]

    report = ev.build_report(records, manifest, prereg, harness_report=_COVERED)
    runs = report["model_quality"]["runs"]
    assert failed in runs and excluded in runs and len(runs) == len(records)
    (contrast,) = report["model_quality"]["comparisons"]["memory_benefit"]
    rows = {row["task"]: row for row in contrast["per_task"]}
    assert set(rows) == set(tasks)
    assert rows[gone["id"]]["seeds"] == 0 and rows[gone["id"]]["missing_seeds"] == 3
    assert rows[gone["id"]]["effect"] is None
    assert rows[victim["task"]]["excluded_seeds"] == 1
    row = rows[failed["task"]]
    assert row["treatment_outcomes"][ev.UNVERIFIED] == 1
    assert row["unknown_usage"]["context_tokens"] == 1
    assert row["seed_range"][0] <= row["effect"] <= row["seed_range"][1]

    paths = []
    for index, record in enumerate(records):
        paths.append(tmp_path / f"{index}.json")
        paths[-1].write_text(json.dumps(record), encoding="utf-8")
    harness = tmp_path / "harness.json"
    harness.write_text(json.dumps(_COVERED), encoding="utf-8")
    monkeypatch.setattr(ev, "load_manifest", lambda: manifest)
    assert ev.main(["report", *map(str, paths), "--harness-report", str(harness)]) == 0
    out = capsys.readouterr().out
    assert f"{failed['task']} seed 1 {TREATMENT}: {ev.UNVERIFIED} (oracle_failed)" in out
    assert "cost None (unknown, at least 3.5)" in out
    assert f"excluded {victim['task']} seed 2" in out and "the API was down" in out
    assert f"no record {gone['id']} seed 1" in out
    assert "primary verified_success on memory_dependent:" in out and "interval [" in out
    assert "non-regression context_tokens on all: unknown" in out
    assert "gate safety_failures:" in out
    assert f"task {gone['id']} (" in out and "0 of 3 seeds" in out
    assert "known part of unknown costs 3.5" in out


# -- never a benchmark in CI, in planning or by accident -------------------------------------


def test_no_workflow_runs_a_real_evaluation_or_sets_its_opt_in():
    """The offline fixture tests run in CI as part of the suite, as tests. A real run or
    its scoring never does: no workflow names the harness's commands or its opt-in."""
    for workflow in WORKFLOWS.glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        assert ev.OPT_IN not in text, workflow
        assert "tests.evaluation" not in text, workflow
        assert "eval-fixture" not in text, workflow
    assert "ANTHROPIC_API_KEY" not in _code()


def test_no_make_gate_runs_the_harness():
    rules = _make_rules()
    assert "eval-fixture" in rules
    for target, rule in rules.items():
        assert "eval-fixture" not in rule.prerequisites, target
    for gate in ("test", "check", "check-matrix", "test-sandbox", "test-sandbox-measured"):
        assert not any("tests.evaluation" in line for line in _make_dry_run(gate)), gate
    assert _make_dry_run("eval-fixture") == ["uv run python -m tests.evaluation_fixture"]


def test_the_harness_names_no_real_provider_and_no_github_client():
    """It cannot launch a model or write to GitHub: it holds nothing that could. The
    fixture runner's agents are all scripted, and its GitHub is the fake."""
    forbidden = re.compile(
        r"ClaudeCodeProvider|OpenCodeProvider|PiProvider|ClaudeSdkProvider|OpenCodeSdkProvider"
        r"|GitHubClient|\"gh\"|'gh'|shell=True|os\.system"
    )
    for name in ("evaluation.py", "evaluation_fixture.py"):
        text = (REPO_ROOT / "tests" / name).read_text(encoding="utf-8")
        assert not forbidden.search(text), name
    assert "import subprocess" not in (REPO_ROOT / "tests" / "evaluation.py").read_text("utf-8")


# -- the protocol document stays what the files say ------------------------------------------

_REGISTERED = re.compile(
    r"^- `(?P<file>[a-z_]+\.(?:json|yaml))`: `(?P<digest>[0-9a-f]{64})`$", re.M
)


def test_the_protocol_registers_the_digests_of_the_files_in_force():
    """A change to a threshold, a task or a setting is a visible edit here: the digest and
    the amendment log change with it. A record made under the old digest is then refused
    as made under another pre-registration."""
    registered = {m["file"]: m["digest"] for m in _REGISTERED.finditer(_protocol())}
    cfg = load_config_file(str(ev.REAL_RUN_CONFIG))
    assert registered == {
        "manifest.json": ev.digest(ev.load_manifest()),
        "preregistration.json": ev.digest(ev.load_preregistration()),
        "real_run_config.yaml": ev.settings_digest(cfg),
    }


def test_the_amendment_log_has_one_entry_per_amendment():
    amendments = ev.load_preregistration()["amendments"]
    section = _section(_protocol(), "## 11. Amendments")
    rows = [line for line in section.splitlines() if re.match(r"^\| \d{4}-\d{2}-\d{2} ", line)]
    assert len(rows) == len(amendments)


def test_the_protocols_task_table_is_the_manifest():
    section = _section(_protocol(), "### 3.3 The tasks")
    rows = re.findall(
        r"^\| `(?P<id>[a-z0-9-]+)` \| #(?P<issue>\d+) \| (?P<cls>dependent|independent) \| "
        r"`(?P<base>[0-9a-f]{10})` \| `(?P<ref>[0-9a-f]{10})` \|",
        section,
        re.M,
    )
    assert rows == [
        (
            task["id"],
            str(task["source_issue"]),
            task["memory_class"].removeprefix("memory_"),
            task["base_sha"][:10],
            task["reference_sha"][:10],
        )
        for task in ev.load_manifest()["tasks"]
    ]


def test_the_protocols_decision_table_is_the_preregistration():
    """Each number a reader sees is the number a decision uses."""
    prereg = ev.load_preregistration()
    section = _prose(_section(_protocol(), "## 5. The pre-registered decision"))
    for name, comparison in prereg["comparisons"].items():
        primary = comparison["primary"]
        assert (
            f"`{name}` | `{primary['metric']}` on `{primary['stratum']}`, at least "
            f"+{primary['min_effect']:.2f}" in section
        ), name
        assert f"${ev.cost_ceiling_usd(prereg, name):,}" in section, name
    criteria = {
        (criterion["metric"], criterion["stratum"]): criterion
        for comparison in prereg["comparisons"].values()
        for criterion in comparison["non_regression"]
    }
    for (metric, _), _criterion in criteria.items():
        assert f"`{metric}`" in section, metric
    memory = prereg["comparisons"]["memory_benefit"]["non_regression"]
    for criterion in memory:
        value = (
            f"+{criterion['relative_margin']:.0%}"
            if "relative_margin" in criterion
            else f"{criterion['margin']:.2f}"
        )
        assert f"| `{criterion['metric']}` | {criterion['stratum']} | {value} |" in section, value
    stats = prereg["statistics"]
    assert f"{stats['resamples']:,} resamples" in section
    assert f"seed {stats['seed']}" in section
    assert f"{stats['max_excluded_pair_share']:.0%} of a contrast's pairs" in section
    sample = prereg["sample"]
    assert f"{sample['tasks']} tasks" in section and f"{len(sample['seeds'])} seeds" in section
    for arm, ceiling in prereg["ceilings"]["per_run_usd"].items():
        assert f"| `{arm}` | ${ceiling} |" in section, arm
    ceilings = prereg["ceilings"]
    assert f"wall-clock ceiling is {ceilings['per_run_wall_clock_hours']} hours" in section
    overhead = ceilings["memory_overhead"]
    assert f"{overhead['storage_bytes_per_issue']:,} bytes of storage per issue" in section
    assert f"{overhead['projection_chars_per_launch']:,} characters of projection" in section
    assert f"{overhead['latency_ms_per_launch']:,} ms of latency" in section
    smoke = _prose(_section(_protocol(), "## 9. Real runs"))
    assert f"at ${ceilings['smoke_usd']} at most" in smoke


def test_a_comparisons_ceiling_is_each_planned_run_at_its_arms_ceiling():
    prereg = ev.load_preregistration()
    assert ev.cost_ceiling_usd(prereg, "memory_benefit") == 36 * (25 + 25)
    assert ev.cost_ceiling_usd(prereg, "multi_agent_benefit") == 36 * (25 + 50 + 50)


CONTROL_PLANE = (
    "replay consistency",
    "task-state transition correctness",
    "duplicate-execution prevention",
    "duplicate external-effect prevention",
    "budget recovery after a crash",
    "interrupted-launch reconciliation",
    "candidate preservation",
    "provider capability mismatch",
    "stale-context denial",
    "agent isolation",
    "unknown-outcome handling",
)
_ITEM_VERDICT = re.compile(r"^- \*\*.+?\*\* \*(?P<verdict>[^*]+)\.\*")


def _control_plane() -> dict[str, tuple[str, list[tuple[str, str]]]]:
    return _bullets(_protocol(), "## 7. Deterministic control-plane tests")


def test_the_control_plane_list_takes_the_issues_items_once_each_in_order():
    subjects = [subject.rstrip(".").lower() for subject in _control_plane()]
    assert tuple(subjects) == CONTROL_PLANE


def test_each_control_plane_item_says_what_holds_today_and_names_a_test():
    """An item is held for the baseline by a named test, and says what a later arm owes."""
    for subject, (opening, tests) in _control_plane().items():
        verdict = _ITEM_VERDICT.match(opening)
        assert verdict and verdict.group("verdict") == "Held for arm 1", subject
        assert tests, subject


def _named_tests() -> list[tuple[str, str]]:
    return sorted({ref for _, refs in _control_plane().values() for ref in refs})


@pytest.mark.parametrize("module, function", _named_tests(), ids=lambda value: value)
def test_every_test_the_protocol_names_exists(module, function):
    assert (REPO_ROOT / module).is_file(), f"{module} does not exist"
    assert function in _test_functions(module), f"{module} defines no {function}"


def test_the_protocol_keeps_the_claims_it_may_not_make_and_the_fixture_list():
    prose = _prose(_protocol())
    for item in ev.load_manifest()["external_results_not_evidence"]:
        assert item in prose, item
    fixtures = ev.read_json(REPO_ROOT / "tests/fixtures/evaluation/fixture_tasks.json")
    section = _section(_protocol(), "## 8. Fixture mode")
    for scenario in fixtures["negative"]:
        assert f"`{scenario['task']}`" in section, scenario["task"]
    for case in ev.load_manifest()["memory_fixture_cases"]["cases"]:
        assert case in _prose(section), case


def test_the_protocols_links_and_every_link_into_it_resolve():
    broken = [
        problem
        for target in _links(PROTOCOL)
        if "://" not in target
        for problem in [_broken(PROTOCOL, target)]
        if problem
    ]
    linked_from = []
    for source in _markdown_files():
        for target in _links(source):
            file_part = target.partition("#")[0]
            if "://" in target or not file_part:
                continue
            if (source.parent / file_part).resolve() == PROTOCOL:
                linked_from.append(source)
                broken += [problem for problem in [_broken(source, target)] if problem]
    assert broken == []
    assert REPO_ROOT / "docs" / "README.md" in linked_from


def test_the_real_run_procedure_says_what_a_run_must_never_be():
    """The opt-in documentation is complete: disposable repository, closed gate, recorded
    approval, a BLOCKED run ending there, and no skipped evidence reported as a pass."""
    section = _prose(_section(_protocol(), "## 9. Real runs"))
    for words in (
        "disposable repository",
        "separately approved",
        ev.OPT_IN,
        "never `--allow-merge`",
        "A `BLOCKED` run ends there",
        "never in CI",
        "A skipped or refused run is a record, never a pass",
    ):
        assert words in section, words
