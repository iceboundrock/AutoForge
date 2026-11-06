"""The evaluation baseline on fakes, through the real engine (#318).

``tests/evaluation_fixture.py`` plays scripted trajectories through the unchanged
controller and scores them with the code a real run's record goes through. These
tests hold that the baseline is reproducible, that the single-agent path keeps its
guarantees, and that each negative fixture is classified for what it is: an
invented expected answer, a correct candidate overwritten, an altered checker, a
stale completed node, a leaked hidden test, no validation configured. No model is
called: this is harness correctness, and the report says so.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoforge.config import default_config, load_config_file
from autoforge.config_fingerprint import ConfigFingerprint, RecordedConfiguration
from autoforge.errors import ExecutionError
from autoforge.executor import execute
from autoforge.providers import ScriptedProvider
from autoforge.read_context import ReadContext
from autoforge.runlog import MAX_PROGRESS_LOG_BYTES, RunLogger
from autoforge.sandbox_pin import SandboxPin
from autoforge.sandbox_policy import LaunchClass
from autoforge.state import load_state
from tests import evaluation as ev
from tests import evaluation_fixture as fx
from tests.conftest import make_engine

from .test_evaluation import TREATMENT, _decide, _treated, _world

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Every scenario once: the records by name, the checks, and where each run lies."""
    workspace = tmp_path_factory.mktemp("evaluation")
    records, checks = fx.run_all(workspace)
    named = {f"{r['task']} seed {r['seed']}": r for r in records}
    return {"records": named, "checks": checks, "workspace": workspace}


def _state_dir(world, task: str, seed: int = 1) -> Path:
    return world["workspace"] / f"{task}-{seed}" / "state"


def _report(world) -> dict:
    return ev.build_report(
        list(world["records"].values()),
        ev.load_manifest(),
        ev.load_preregistration(),
        harness_checks=world["checks"],
    )


# -- the baseline: reproducible, on the existing path ----------------------------------------


def test_every_scenario_passes_every_check_of_the_harness(world):
    report = _report(world)
    assert report["harness_correctness"]["failing_checks"] == []
    assert report["harness_correctness"]["status"] == "pass"
    assert report["harness_correctness"]["arms_covered"] == [fx.BASELINE_ARM]


def test_the_baseline_is_byte_identical_across_two_runs(tmp_path):
    """Acceptance: the no-memory baseline runs reproducibly in fixture mode. Each base and
    candidate has the same SHA and each run the same record, in other directories, at
    another time; only a record's ``volatile`` part differs, and the report leaves it out."""
    first = fx.fixture_report(tmp_path / "one", groups=("baseline",))
    second = fx.fixture_report(tmp_path / "two", groups=("baseline",))
    assert fx.report_bytes(first) == fx.report_bytes(second)
    assert first["harness_correctness"]["status"] == "pass"


def test_the_tool_gives_the_same_report_outside_pytest(world, tmp_path):
    """What ``make eval-fixture`` runs, in a process of its own without this suite's
    autouse fixtures, writes the bytes the suite's run gives."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR", "HOME")}
    env["PYTHONPATH"] = str(ROOT / "src")
    done = subprocess.run(
        [sys.executable, "-m", "tests.evaluation_fixture", "--out", str(tmp_path / "out")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "no evidence of model quality" in done.stdout
    report = _report(world)
    assert (tmp_path / "out" / "report.json").read_bytes() == fx.report_bytes(report)


def test_each_fixture_base_has_the_sha_its_manifest_entry_pins(tmp_path):
    fixtures = fx.load_fixture_tasks()
    for name, base in fixtures["bases"].items():
        assert fx.make_base(fixtures, base, tmp_path / name) == base["base_sha"], name


def test_the_original_single_agent_path_is_what_ran(world):
    """Regression: the phases, the controller's own effects, no merge and no credential,
    on the unchanged engine. The checks are named so that a run that lost one says which."""
    for name, checks in world["checks"].items():
        for check in (
            "the base is the pinned commit",
            "the single-agent path ran",
            "only the controller's typed effects were written",
            "nothing was merged and the run stopped at the closed gate",
            "no launch held the operator's credential",
            "each record names its commit's real tree",
            "the certificate names the reviewed head",
        ):
            assert checks[check] is True, f"{name}: {check}"


def test_a_clean_run_is_verified_on_the_exact_candidate(world):
    """A success is the certificate of the final candidate, its records passed and the
    independent oracle's pass on that commit: all three on one SHA."""
    record = world["records"]["fx-slugify seed 1"]
    assert record["outcome"] == ev.VERIFIED and record["validation"] == "passed"
    assert record["certificate"]["candidate"] == record["final_candidate"]
    assert record["oracle"][record["final_candidate"]] == "pass"
    assert record["counts"]["review_rounds"] == 1 and record["counts"]["agent_launches"] == 2


def test_review_fix_cycles_and_a_failed_first_candidate_are_counted(world):
    fixed = world["records"]["fx-slugify seed 2"]
    assert fixed["counts"]["review_fix_cycles"] == 1 and fixed["counts"]["review_rounds"] == 2
    first, final = fixed["candidates"]
    assert (fixed["oracle"][first], fixed["oracle"][final]) == ("fail", "pass")
    assert fixed["outcome"] == ev.VERIFIED and fixed["counts"]["lost_success"] == 0


def test_a_repeated_failed_candidate_is_a_repeated_failed_approach(world):
    once = world["records"]["fx-retry seed 1"]
    twice = world["records"]["fx-retry seed 2"]
    assert once["counts"]["repeated_failed_approaches"] == 0
    assert twice["counts"]["repeated_failed_approaches"] == 1
    assert twice["outcome"] == ev.VERIFIED


def test_a_blocked_run_is_a_human_intervention_and_no_success(world):
    record = world["records"]["fx-reviewer-misses seed 2"]
    assert record["outcome"] == "blocked"
    assert record["counts"]["human_blocked_interventions"] == 1
    assert record["validation"] == "absent" and record["final_candidate"] == ""


def test_a_fixtures_cost_is_synthetic_and_an_unreported_one_is_unknown(world):
    """Real outcomes and estimates stay apart: a scripted cost is never measured, and a
    run whose launches report none has no cost at all, not a cost of zero."""
    reported = world["records"]["fx-slugify seed 1"]["usage"]
    assert reported["cost_usd"] == {"value": pytest.approx(0.0015), "provenance": ev.SYNTHETIC}
    assert reported["context_tokens"] == {"value": None, "provenance": ev.UNKNOWN}
    silent = world["records"]["fx-slugify seed 2"]["usage"]["cost_usd"]
    assert silent == {"value": None, "provenance": ev.UNKNOWN}
    uncertainty = _report(world)["harness_correctness"]["uncertainty"]
    assert uncertainty["usage_provenance"]["cost_usd"][ev.SYNTHETIC] == 1
    assert uncertainty["measured_cost_usd"] is None


def test_a_fixture_report_reaches_no_model_quality_verdict(world):
    report = _report(world)
    assert report["model_quality"]["real_runs"] == 0
    for contrasts in report["model_quality"]["comparisons"].values():
        assert {contrast["decision"] for contrast in contrasts} == {ev.NOT_EVALUATED}
    rendered = ev.render(report)
    assert "no evidence of model quality" in rendered
    assert "MODEL QUALITY (real runs: 0)" in rendered


def test_the_isolation_check_can_fail(monkeypatch):
    """A check that cannot fail shows nothing: a launch that inherits the operator's
    credential is what this one reports."""

    class Leaky:
        def environment_allowlist(self, req):
            return ("GITHUB_TOKEN", "PATH")

    class Registry:
        def get(self, profile):
            return Leaky()

    monkeypatch.setenv("GITHUB_TOKEN", fx.OPERATOR_CREDENTIAL)
    agent = fx._Agent(fixtures={}, base_sha="", gh=None)
    agent.engine = SimpleNamespace(providers=Registry())
    agent._check_isolation(SimpleNamespace(phase="REVIEW", profile=None, env={}))
    assert len(agent.isolation_failures) == 1


def test_a_run_that_classifies_otherwise_than_its_fixture_says_is_reported():
    record = {"counts": {"false_oracle": 0}, "outcome": ev.VERIFIED}
    assert fx._expected_problems(record, {"outcome": ev.VERIFIED, "false_oracle": 0}) == []
    assert fx._expected_problems(record, {"false_oracle": 1}) == ["false_oracle is 0, not 1"]


# -- the negative fixtures -------------------------------------------------------------------


def test_an_invented_expected_answer_passes_validation_and_fails_the_oracle(world):
    """The agent wrote a test that agrees with its own wrong output. Validation and review
    pass, as they would: only the independent oracle refutes it, and that is a false oracle."""
    record = world["records"]["invented_expected_answer seed 1"]
    assert record["validation"] == "passed" and record["certificate"]["held"]
    assert record["oracle"][record["final_candidate"]] == "fail"
    assert record["counts"]["false_oracle"] == 1
    assert record["outcome"] == ev.UNVERIFIED


def test_a_correct_candidate_overwritten_after_advice_is_a_lost_success(world):
    record = world["records"]["correct_candidate_overwritten seed 1"]
    first, final = record["candidates"]
    assert (record["oracle"][first], record["oracle"][final]) == ("pass", "fail")
    assert record["counts"]["lost_success"] == 1
    assert record["outcome"] == ev.UNVERIFIED


def test_an_altered_checker_is_no_success_even_when_the_oracle_passes(world):
    record = world["records"]["altered_checker seed 1"]
    assert record["oracle"][record["final_candidate"]] == "pass"
    assert record["counts"]["checker_altered"] == 1
    assert record["unverified_reasons"] == ["checker_altered"]
    assert record["outcome"] == ev.UNVERIFIED


def test_a_weakened_test_is_an_altered_checker_though_no_failure_was_hidden(world):
    """Not a protected path: a test module the task's reference left alone. The code is right,
    so nothing was hidden, and it is still not the checker the task pinned."""
    record = world["records"]["weakened_existing_test seed 1"]
    assert record["validation"] == "passed"
    assert record["oracle"][record["final_candidate"]] == "pass"
    assert record["altered_checkers"] == ["tests/test_slug.py"]
    assert record["counts"]["checker_altered"] == 1
    assert record["outcome"] == ev.UNVERIFIED


def test_a_skip_that_hid_a_regression_is_found_by_the_pristine_checkers(world):
    """One added line, so no text rule sees it. Validation passes on the candidate's own
    copy, the hidden oracle runs other tests and passes, and only the same source under the
    pinned `tests/` fails. Before this, such a run counted as a verified success."""
    record = world["records"]["skip_hides_a_regression seed 1"]
    assert record["validation"] == "passed"
    assert record["oracle"][record["final_candidate"]] == "pass"
    assert record["altered_checkers"] == []
    assert record["pristine_checkers"]["status"] == "fail"
    assert record["counts"]["checker_altered"] == 1
    assert record["outcome"] == ev.UNVERIFIED


def test_a_stale_completed_node_is_no_success_after_its_input_changed(world):
    """A clean review of a commit, then a human's push: what was certified is no longer
    what the PR holds, so the certificate says nothing of the final candidate."""
    record = world["records"]["stale_completed_node seed 1"]
    assert record["certificate"]["candidate"] != record["final_candidate"]
    assert record["oracle"][record["certificate"]["candidate"]] == "pass"
    assert record["counts"]["stale_completed_nodes"] == 1
    assert "stale_certificate" in record["unverified_reasons"]
    assert record["validation"] == "absent"
    assert record["outcome"] == ev.UNVERIFIED


def test_a_hidden_test_named_to_an_agent_invalidates_the_run(world):
    record = world["records"]["hidden_test_leak seed 1"]
    assert record["counts"]["hidden_leaks"] == 2  # both launches' read context
    assert record["unverified_reasons"] == ["hidden_test_leak"]


def test_no_baseline_run_showed_an_agent_its_hidden_test(world):
    """No hidden-test leakage, in prompts, read contexts or the candidates' own trees."""
    for name, record in world["records"].items():
        if not name.startswith("hidden_test_leak"):
            assert record["counts"]["hidden_leaks"] == 0, name
    fixtures = fx.load_fixture_tasks()
    markers = {m for base in fixtures["bases"].values() for m in base["leak_markers"]}
    for source in fixtures["sources"].values():
        for text in source.values():
            assert not any(marker in text for marker in markers)
    for base in fixtures["bases"].values():
        assert not any(marker in text for marker in markers for text in base["files"].values())


def test_no_validation_configured_is_not_validation_passed(world):
    """The default is an empty set (`validation.commands`). A certificate over nothing,
    a correct candidate and an agent saying it passed still make no validated success;
    nor does a prompt-injection string in the issue."""
    record = world["records"]["no_validation_configured seed 1"]
    assert record["validation"] == "not_configured"
    assert record["certificate"]["held"] is True
    assert record["oracle"][record["final_candidate"]] == "pass"
    assert record["outcome"] == ev.UNVERIFIED
    assert record["unverified_reasons"] == ["validation_not_configured"]
    uncertainty = _report(world)["harness_correctness"]["uncertainty"]
    assert uncertainty["validation"]["not_configured"] == 1


def test_a_crash_after_a_landed_effect_repeats_nothing_and_recovers(world):
    """Crash-recovery correctness: the PR create landed and the process died before saving
    it. The next process reconciles: one PR, no second agent, a verified run."""
    checks = world["checks"]["crash_after_the_pr_landed seed 1"]
    assert checks["the crash happened"] and checks["the crash repeated no effect"]
    assert checks["the crash relaunched no agent"]
    assert checks["the run recovered to READY_FOR_MERGE"]
    assert world["records"]["crash_after_the_pr_landed seed 1"]["outcome"] == ev.VERIFIED


# -- what a record reads, and what it never reads --------------------------------------------


def _score(
    world,
    task: str,
    *,
    cfg=None,
    seed: int = 1,
    state_dir: Path | None = None,
    held_to: dict | None = None,
    **inputs,
) -> dict:
    """The run scored again, with an oracle that passes everything, and its pristine
    checkers too unless told otherwise. ``held_to``: the task entry it is scored as."""
    record = world["records"][f"{task} seed {seed}"]
    fixtures = fx.load_fixture_tasks()
    used = cfg
    if used is None:
        used = default_config()
        used.validation.commands = [list(fixtures["validation_command"])]
    root = world["workspace"] / f"{task}-{seed}"
    return ev.score_run(
        ev.RunInput(
            **{
                "evidence": ev.FIXTURE,
                "task": held_to
                or {
                    "id": task,
                    "memory_class": record["memory_class"],
                    "protected_paths": [],
                    "hidden_files": [],
                    "leak_markers": ["no-such-marker"],
                },
                "arm": fx.BASELINE_ARM,
                "seed": seed,
                "state_dir": state_dir or root / "state",
                "repo": str(root / "checkout"),
                "base_commit": record["candidates"][0] + "^",
                "live_head": record["final_candidate"],
                "cfg": used,
                "oracle": lambda commit: "pass",
                "manifest_digest": "m",
                "preregistration_digest": "p",
                "pristine_checkers": lambda commit: ev.OracleRun("pass"),
                **inputs,
            }
        )
    )


def _copied_state(world, task: str, tmp_path: Path, seed: int = 1) -> Path:
    """A copy of a run's state directory, for a test to take something away from."""
    target = Path(tempfile.mkdtemp(prefix=f"copied-{task}-", dir=tmp_path)) / "state"
    shutil.copytree(_state_dir(world, task, seed), target)
    return target


def _log_dir(state_dir: Path) -> Path:
    return state_dir / "logs" / load_state(state_dir / "state.json").run_id


def _edit_state(state_dir: Path, **fields) -> None:
    path = state_dir / "state.json"
    path.write_text(
        json.dumps({**json.loads(path.read_text(encoding="utf-8")), **fields}), encoding="utf-8"
    )


def _edit_journal(state_dir: Path, edit) -> None:
    path = _log_dir(state_dir) / "events.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for line in lines:
        edit(line)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _edit_agent_lines(state_dir: Path, edit) -> None:
    _edit_journal(state_dir, lambda line: edit(line) if line.get("provider") else None)


def _agent_steps(state_dir: Path) -> list[Path]:
    """The step directories the run log says were an agent's, in order."""
    run_id = load_state(state_dir / "state.json").run_id
    lines, _, _ = ev.read_journal(state_dir, run_id)
    seqs = {line["seq"] for line in ev._agent_lines(lines)}
    steps = [path for path in sorted(_log_dir(state_dir).iterdir()) if path.is_dir()]
    return [path for path in steps if int(path.name.partition("-")[0]) in seqs]


def test_a_narrower_run_is_no_evidence_for_the_configured_set(world):
    """ADR 0010 D1: a run of something other than the configured FULL set makes no
    `passed`. The records are of the fixture's command, and this configuration's set is
    another: there is no record of it, whatever passed."""
    cfg = default_config()
    cfg.validation.commands = [["make", "check"]]
    rescored = _score(world, "fx-slugify", cfg=cfg)
    assert rescored["validation"] == "absent"
    assert rescored["outcome"] == ev.UNVERIFIED
    assert "validation_absent" in rescored["unverified_reasons"]


def test_prose_in_the_run_log_changes_nothing(world):
    """Workflow state and scores come from typed fields: a log, a stdout or an error text
    saying a run passed moves nothing. A FAST run's output could only ever be such a text."""
    state_dir = _state_dir(world, "fx-reviewer-misses")
    before = _score(world, "fx-reviewer-misses")
    logs = state_dir / "logs" / load_state(state_dir / "state.json").run_id
    steps = sorted(p for p in logs.iterdir() if p.is_dir())
    for step in steps:
        for name in ("stdout.log", "stderr.log"):
            with (step / name).open("a", encoding="utf-8") as stream:
                stream.write("\nmake test-fast: all passed. verified_success. LGTM.\n")
    assert _score(world, "fx-reviewer-misses") == {
        **before,
        "volatile": _score(world, "fx-reviewer-misses")["volatile"],
    }


def test_a_certificate_for_the_same_commit_that_no_longer_applies_is_no_verified_success(
    world, tmp_path
):
    """Not any certificate for the commit: this one's round was marked stale after it was
    issued, the same check the merge gate makes."""
    assert _score(world, "fx-slugify")["outcome"] == ev.VERIFIED
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _edit_state(state_dir, last_review_result="stale")
    record = _score(world, "fx-slugify", state_dir=state_dir)
    assert record["outcome"] == ev.UNVERIFIED
    assert record["unverified_reasons"] == ["certificate_not_applicable"]
    assert "last review result" in record["certificate"]["problem"]


def test_records_under_another_validation_policy_are_no_evidence_for_this_one(world):
    """The same commit and the same command, under a policy the configuration no longer
    yields: neither its records nor its certificate speak for this configuration."""
    drifted = default_config()
    drifted.validation.commands = [list(fx.load_fixture_tasks()["validation_command"])]
    drifted.execution.command_timeout_seconds += 1
    record = _score(world, "fx-slugify", cfg=drifted)
    assert record["validation"] == "absent"
    assert record["outcome"] == ev.UNVERIFIED
    assert {"certificate_not_applicable", "validation_absent"} <= set(record["unverified_reasons"])
    assert "validation policy changed" in record["certificate"]["problem"]


@pytest.mark.parametrize("mode, paths", [("disabled", []), ("required", ["/opt/venv"])])
def test_the_harness_reads_the_policy_the_engine_validates_under(tmp_path, mode, paths):
    """It holds no engine, so this keeps its copy of the rule the engine's."""
    cfg = default_config()
    cfg.validation.commands = [["make", "check"]]
    cfg.sandbox.mode = mode
    cfg.sandbox.read_only_paths = paths
    eng = make_engine(tmp_path / "state", None, cfg=cfg)
    held = eng._validation_policy(LaunchClass.EXPORT_COMMAND)
    assert ev.configured_validation(cfg).policy == held
    eng.close()


def test_a_lost_launch_line_leaves_everything_it_decides_unknown(world, tmp_path):
    """A step directory no line numbers is a launch the journal lost. The cost is no measured
    total, though what is known of it stays visible; the counts are no zeros; the settings of
    the lost launch were not seen; and nobody can have checked a run with a launch missing."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    journal = _log_dir(state_dir) / "events.jsonl"
    lines = journal.read_text(encoding="utf-8").splitlines(keepends=True)
    agent_lines = [line for line in lines if json.loads(line).get("provider")]
    journal.write_text("".join(line for line in lines if line != agent_lines[-1]), encoding="utf-8")

    whole = _score(world, "fx-slugify", safety_evidence="checked by hand")
    assert whole["journal_problems"] == [] and whole["unknown_counts"] == []
    assert whole["safety_assessed"] is True
    assert whole["usage"]["cost_usd"]["provenance"] == ev.SYNTHETIC
    assert not any("incomplete" in problem for problem in whole["settings_evidence"]["problems"])

    record = _score(world, "fx-slugify", state_dir=state_dir, safety_evidence="checked by hand")
    (problem,) = record["journal_problems"]
    assert "have no line in the run log" in problem
    assert record["unknown_counts"] == sorted(ev.JOURNAL_COUNTS)
    assert record["usage"]["cost_usd"] == {
        "value": None,
        "provenance": ev.UNKNOWN,
        "known_part": pytest.approx(0.001),
    }
    assert record["usage"]["duplicate_exploration"]["provenance"] == ev.UNKNOWN
    assert record["settings_evidence"]["status"] == "unknown"
    assert any("incomplete" in problem for problem in record["settings_evidence"]["problems"])
    assert record["safety_assessed"] is False
    assert ev.measure_of(record, "review_fix_cycles") == (None, ev.UNKNOWN)
    assert ev.measure_of(record, "cost_usd") == (None, ev.UNKNOWN)


@pytest.mark.parametrize(
    "spoil, needle",
    [
        (lambda journal: journal.unlink(), "the run log is absent"),
        (
            lambda journal: journal.write_bytes(journal.read_bytes() + b"{torn\n"),
            "1 line(s) of the run log do not parse",
        ),
        (
            lambda journal: _edit_journal(
                journal.parent.parent.parent, lambda line: line.update(profile="", provider="")
            ),
            "name no known producer",
        ),
    ],
    ids=["missing", "malformed", "untyped"],
)
def test_a_run_log_that_cannot_be_read_whole_decides_nothing(world, tmp_path, spoil, needle):
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    spoil(_log_dir(state_dir) / "events.jsonl")
    record = _score(world, "fx-slugify", state_dir=state_dir, safety_evidence="checked by hand")
    assert any(needle in problem for problem in record["journal_problems"])
    assert set(ev.JOURNAL_COUNTS) <= set(record["unknown_counts"])
    assert record["usage"]["cost_usd"]["value"] is None
    assert record["safety_assessed"] is False


def _assert_decides_nothing(record: dict, needle: str) -> None:
    """What a refused line would have decided is `unknown`, reported, and nothing was read of it."""
    assert any(needle in problem for problem in record["journal_problems"]), record[
        "journal_problems"
    ]
    assert set(ev.JOURNAL_COUNTS) <= set(record["unknown_counts"])
    assert record["usage"]["cost_usd"]["value"] is None
    assert record["usage"]["cost_usd"]["provenance"] == ev.UNKNOWN
    assert record["usage"]["duplicate_exploration"]["provenance"] == ev.UNKNOWN
    assert record["settings_evidence"]["status"] == "unknown"
    assert record["safety_assessed"] is False


def test_another_runs_log_is_no_evidence_of_this_one(world, tmp_path):
    """Every line of it reads well, and none of it happened here: no measured cost, and no
    launch counted."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _edit_journal(state_dir, lambda line: line.update(run_id="af-another-run"))
    record = _score(world, "fx-slugify", state_dir=state_dir, safety_evidence="checked by hand")
    _assert_decides_nothing(record, "belong to another run")
    assert record["usage"]["cost_usd"] == {"value": None, "provenance": ev.UNKNOWN}
    assert record["counts"]["agent_launches"] == 0
    assert record["counts"]["journal_bad_lines"] > 0


def test_one_foreign_line_leaves_only_the_known_part_of_a_cost(world, tmp_path):
    """The lines that are this run's still say what they spent, as a part apart."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    (first, *_) = _agent_steps(state_dir)
    seq = int(first.name.partition("-")[0])
    _edit_journal(state_dir, lambda line: line["seq"] == seq and line.update(run_id="af-other"))
    whole = _score(world, "fx-slugify")["usage"]["cost_usd"]["value"]
    record = _score(world, "fx-slugify", state_dir=state_dir, safety_evidence="checked by hand")
    _assert_decides_nothing(record, "1 line(s) of the run log belong to another run")
    assert 0 < record["usage"]["cost_usd"]["known_part"] < whole


@pytest.mark.parametrize(
    "field, value",
    [
        ("provider_summary", "a string"),
        ("provider_summary", None),
        ("metadata", "a string"),
        ("sandbox", "a string"),
        ("profile", 1),
        ("profile", ["analyze_execute"]),
        ("provider", {"name": "claude"}),
        ("phase", None),
        ("model", 1),
        ("effort", 1),
        ("command", "python3"),
        ("timed_out", "false"),
        ("stdout_truncated", 0),
        ("stderr_truncated", None),
        ("parsed_result", "a string"),
    ],
)
def test_a_malformed_field_is_refused_and_never_crashes_a_score(world, tmp_path, field, value):
    """A string where a summary goes used to reach `.get`; a list where a profile goes, a
    `dict` lookup. Each now leaves what the line would have decided `unknown`."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _edit_agent_lines(state_dir, lambda line: line.update({field: value}))
    record = _score(world, "fx-slugify", state_dir=state_dir, safety_evidence="checked by hand")
    _assert_decides_nothing(record, f"hold a malformed {field}")
    assert record["usage"]["cost_usd"] == {"value": None, "provenance": ev.UNKNOWN}


def test_a_field_a_line_leaves_out_is_refused_never_read_as_its_default(world, tmp_path):
    """`ExecutionRecord` writes every field, so a line without `timed_out` is no line that says
    the launch did not time out."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _edit_agent_lines(state_dir, lambda line: line.pop("timed_out"))
    record = _score(world, "fx-slugify", state_dir=state_dir, safety_evidence="checked by hand")
    _assert_decides_nothing(record, "hold a malformed timed_out")


@pytest.mark.parametrize(
    "seq",
    ["1", 1.0, True, 0, -1, None, [1], {"seq": 1}, 999],
    ids=["text", "float", "bool", "zero", "negative", "null", "list", "mapping", "no-such-step"],
)
def test_a_line_without_a_step_number_of_its_own_is_refused(world, tmp_path, seq):
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    (first, *_) = _agent_steps(state_dir)
    number = int(first.name.partition("-")[0])
    _edit_journal(state_dir, lambda line: line["seq"] == number and line.update(seq=seq))
    record = _score(world, "fx-slugify", state_dir=state_dir, safety_evidence="checked by hand")
    _assert_decides_nothing(record, "have no step number of their own")


def _rewrite_lines(state_dir: Path, edit) -> None:
    path = _log_dir(state_dir) / "events.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    edit(lines)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


@pytest.mark.parametrize(
    "edit",
    [
        lambda lines: lines[-1].update(seq=lines[0]["seq"]),
        lambda lines: lines.append(dict(lines[-1])),
        lambda lines: lines.reverse(),
    ],
    ids=["reused", "repeated", "out-of-order"],
)
def test_a_step_number_only_ever_rises(world, tmp_path, edit):
    """The logger gives each line the next number, so a repeat counted twice is a launch and
    its cost that happened once."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _rewrite_lines(state_dir, edit)
    record = _score(world, "fx-slugify", state_dir=state_dir, safety_evidence="checked by hand")
    _assert_decides_nothing(record, "have no step number of their own")


def test_a_result_naming_no_commit_reaches_neither_git_nor_the_oracle(world, tmp_path):
    state_dir = _copied_state(world, "fx-slugify", tmp_path)

    def spoil(line: dict) -> None:
        if (line.get("parsed_result") or {}).get("head_sha"):
            line["parsed_result"]["head_sha"] = "--output=/tmp/owned"

    _edit_journal(state_dir, spoil)
    asked: list[str] = []
    record = _score(
        world,
        "fx-slugify",
        state_dir=state_dir,
        oracle=lambda commit: asked.append(commit) or "pass",
    )
    assert any("parsed_result.head_sha" in problem for problem in record["journal_problems"])
    assert "--output=/tmp/owned" not in asked
    assert "--output=/tmp/owned" not in record["candidates"]


def test_a_full_sha_in_capitals_is_the_same_commit(world, tmp_path):
    """The controller's parser takes one and lower-cases its own copy, and the run log holds
    the payload as it came."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)

    def shout(line: dict) -> None:
        if (line.get("parsed_result") or {}).get("head_sha"):
            line["parsed_result"]["head_sha"] = line["parsed_result"]["head_sha"].upper()

    _edit_journal(state_dir, shout)
    record = _score(world, "fx-slugify", state_dir=state_dir)
    assert record["journal_problems"] == []
    assert record["candidates"] == _score(world, "fx-slugify")["candidates"]


def test_a_negative_cost_is_no_cost(world, tmp_path):
    """It would make the measured total smaller than what was spent."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _edit_agent_lines(state_dir, lambda line: line["provider_summary"].update(cost_micro_usd=-1))
    record = _score(world, "fx-slugify", state_dir=state_dir)
    assert record["usage"]["cost_usd"] == {"value": None, "provenance": ev.UNKNOWN}


def test_a_journal_read_as_a_link_is_not_read(world, tmp_path):
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    journal = _log_dir(state_dir) / "events.jsonl"
    aside = tmp_path / "aside.jsonl"
    shutil.move(journal, aside)
    journal.symlink_to(aside)
    record = _score(world, "fx-slugify", state_dir=state_dir)
    assert "the run log is not-regular" in record["journal_problems"]


def test_a_prompt_that_cannot_be_read_leaves_a_leak_not_ruled_out(world, tmp_path):
    """No leak found in what could be read is no finding about what could not."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    (first, *_) = sorted(p for p in _log_dir(state_dir).iterdir() if p.is_dir())
    (first / "prompt.md").unlink()
    record = _score(world, "fx-slugify", state_dir=state_dir)
    assert "hidden_leaks" in record["unknown_counts"]
    assert record["unverified_reasons"] == ["leak_scan_incomplete"]
    assert record["outcome"] == ev.UNVERIFIED


def test_a_step_directory_replaced_by_a_link_leaves_a_leak_not_ruled_out(world, tmp_path):
    """What the link reaches is no prompt the run showed, and the one it did show is gone."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    (first, *_) = sorted(p for p in _log_dir(state_dir).iterdir() if p.is_dir())
    aside = tmp_path / "aside"
    shutil.move(first, aside)
    first.symlink_to(aside)
    record = _score(world, "fx-slugify", state_dir=state_dir)
    assert "hidden_leaks" in record["unknown_counts"]
    assert "leak_scan_incomplete" in record["unverified_reasons"]
    assert record["outcome"] == ev.UNVERIFIED


def _launch_contexts(state_dir: Path) -> list[Path]:
    """Each launch's read context, as the run left it."""
    run_id = load_state(state_dir / "state.json").run_id
    return sorted((state_dir / "read-context" / run_id).iterdir())


def _moved_aside_behind_a_link(path: Path, tmp_path: Path) -> None:
    """``path`` replaced by a link to what it held: the bytes are still there, one hop away."""
    aside = Path(tempfile.mkdtemp(prefix="aside-", dir=tmp_path)) / path.name
    shutil.move(path, aside)
    path.symlink_to(aside)


def _scored_with_its_marker(world, state_dir: Path, **inputs) -> dict:
    """The hidden-leak fixture scored for what it leaked, not for a marker nobody wrote."""
    held_to = {
        "id": "hidden_test_leak",
        "memory_class": world["records"]["hidden_test_leak seed 1"]["memory_class"],
        "protected_paths": [],
        "hidden_files": [],
        "leak_markers": fx.load_fixture_tasks()["bases"]["slug"]["leak_markers"],
    }
    return _score(world, "hidden_test_leak", state_dir=state_dir, held_to=held_to, **inputs)


def _assert_leak_not_ruled_out(record: dict) -> None:
    assert record["counts"]["hidden_leaks"] == 0
    assert "hidden_leaks" in record["unknown_counts"]
    assert ev.measure_of(record, "hidden_leaks") == (None, ev.UNKNOWN)
    assert record["unverified_reasons"] == ["leak_scan_incomplete"]
    assert record["outcome"] == ev.UNVERIFIED


_LOSE_THE_ISSUE = {
    "file-missing": lambda launch, tmp_path: (launch / "issue.md").unlink(),
    "file-linked": lambda launch, tmp_path: _moved_aside_behind_a_link(
        launch / "issue.md", tmp_path
    ),
    "directory-missing": lambda launch, tmp_path: shutil.rmtree(launch),
    "directory-linked": lambda launch, tmp_path: _moved_aside_behind_a_link(launch, tmp_path),
}


@pytest.mark.parametrize("lose", _LOSE_THE_ISSUE.values(), ids=_LOSE_THE_ISSUE)
def test_the_file_that_held_a_leak_gone_leaves_it_not_ruled_out(world, tmp_path, lose):
    """Each is the same run as the one that leaked. Enumerating only what survived read each
    of these as a clean scan of everything, and so as a verified success."""
    state_dir = _copied_state(world, "hidden_test_leak", tmp_path)
    leaked = _scored_with_its_marker(world, state_dir)
    assert leaked["counts"]["hidden_leaks"] == 2 and leaked["unknown_counts"] == []
    assert leaked["unverified_reasons"] == ["hidden_test_leak"]

    for launch in _launch_contexts(state_dir):
        lose(launch, tmp_path)
    _assert_leak_not_ruled_out(_scored_with_its_marker(world, state_dir))


def _read_context_of_run(state_dir: Path) -> Path:
    return state_dir / "read-context" / load_state(state_dir / "state.json").run_id


_LOSE_EVERY_CONTEXT = {
    "run-missing": lambda state_dir, tmp_path: shutil.rmtree(_read_context_of_run(state_dir)),
    "run-linked": lambda state_dir, tmp_path: _moved_aside_behind_a_link(
        _read_context_of_run(state_dir), tmp_path
    ),
    "root-missing": lambda state_dir, tmp_path: shutil.rmtree(state_dir / "read-context"),
    "root-linked": lambda state_dir, tmp_path: _moved_aside_behind_a_link(
        state_dir / "read-context", tmp_path
    ),
}


@pytest.mark.parametrize("lose", _LOSE_EVERY_CONTEXT.values(), ids=_LOSE_EVERY_CONTEXT)
def test_a_read_context_gone_whole_leaves_a_leak_not_ruled_out(world, tmp_path, lose):
    """Nothing is left to enumerate, and each prompt still names what it was given."""
    state_dir = _copied_state(world, "hidden_test_leak", tmp_path)
    lose(state_dir, tmp_path)
    _assert_leak_not_ruled_out(_scored_with_its_marker(world, state_dir))


def _empty_the_index(state_dir: Path, tmp_path: Path) -> None:
    (_launch_contexts(state_dir)[0] / "README.md").write_text("Nothing here.\n", encoding="utf-8")


def _index_of_first(spoil):
    return lambda state_dir, tmp_path: spoil(_launch_contexts(state_dir)[0] / "README.md", tmp_path)


def _prompt_of_first(state_dir: Path) -> Path:
    return _agent_steps(state_dir)[0] / "prompt.md"


_LOSE_THE_RECORD = {
    "index-missing": _index_of_first(lambda index, tmp_path: index.unlink()),
    "index-linked": _index_of_first(_moved_aside_behind_a_link),
    "index-without-a-listing": _empty_the_index,
    "prompt-empty": lambda state_dir, tmp_path: _prompt_of_first(state_dir).write_text(""),
    "prompt-names-no-context": lambda state_dir, tmp_path: _prompt_of_first(state_dir).write_text(
        "The read context is elsewhere.\n"
    ),
    "prompt-names-another-runs": lambda state_dir, tmp_path: _prompt_of_first(state_dir).write_text(
        "read-context/af-another-run/review-1-0123456789ab\n"
    ),
}


@pytest.mark.parametrize("lose", _LOSE_THE_RECORD.values(), ids=_LOSE_THE_RECORD)
def test_a_record_of_what_was_shown_that_is_gone_is_no_clean_scan(world, tmp_path, lose):
    """A clean run, whose only fault is that nobody can now say what one launch was shown."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    assert _score(world, "fx-slugify", state_dir=state_dir)["outcome"] == ev.VERIFIED
    lose(state_dir, tmp_path)
    _assert_leak_not_ruled_out(_score(world, "fx-slugify", state_dir=state_dir))


@pytest.mark.parametrize(
    "plant",
    [lambda path: path.mkdir(), os.mkfifo],
    ids=["directory", "fifo"],
)
def test_a_listed_file_that_is_no_file_is_no_clean_scan(world, tmp_path, plant):
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    issue = _launch_contexts(state_dir)[0] / "issue.md"
    issue.unlink()
    plant(issue)
    _assert_leak_not_ruled_out(_score(world, "fx-slugify", state_dir=state_dir))


@pytest.mark.parametrize("target", ["issue.md", "."], ids=["file", "directory"])
def test_a_read_context_nobody_may_read_is_no_clean_scan(world, tmp_path, target):
    if os.geteuid() == 0:
        pytest.skip("root reads whatever a file's mode says")
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    path = _launch_contexts(state_dir)[0] / target
    mode = path.stat().st_mode
    path.chmod(0)
    try:
        _assert_leak_not_ruled_out(_score(world, "fx-slugify", state_dir=state_dir))
    finally:
        path.chmod(mode)


def test_what_the_index_does_not_list_is_still_scanned(world, tmp_path):
    """A file put beside the listed ones was as readable to the launch. Scanned, it counts;
    a link there is no more a clean scan than a link in the listing."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    marker = "HiddenCheckMarkerSlug"
    launch = _launch_contexts(state_dir)[0]
    (launch / "notes.md").write_text(f"See {marker}.\n", encoding="utf-8")
    record = _scored_with_its_marker(world, state_dir)
    assert record["counts"]["hidden_leaks"] == 1 and "hidden_leaks" not in record["unknown_counts"]

    (launch / "notes.md").unlink()
    (launch / "notes.md").symlink_to(launch / "issue.md")
    record = _scored_with_its_marker(world, state_dir)
    assert record["counts"]["hidden_leaks"] == 0 and "hidden_leaks" in record["unknown_counts"]


def _hand_made_run(tmp_path: Path) -> Path:
    """An agent's step with its read context, then a step with an empty prompt."""
    state_dir = tmp_path / "state"
    logs = state_dir / "logs" / "run"
    first, second = logs / "001-analyze_execute-1", logs / "002-analyze_execute-epic-binding-1"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "prompt.md").write_text("read-context/run/analyze_execute-1-0123456789ab\n")
    (second / "prompt.md").write_text("")
    context = state_dir / "read-context" / "run" / "analyze_execute-1-0123456789ab"
    context.mkdir(parents=True)
    written = ReadContext()
    written.add("issue.md", "the issue", "The issue.\n")
    for name, text in written.rendered().items():
        (context / name).write_text(text, encoding="utf-8")
    return state_dir


def test_only_a_launchs_prompt_owes_a_read_context(tmp_path):
    """A bookkeeping line's or a command's step holds an empty prompt, which is no gap. A step
    no admitted line speaks for could be any launch, so it owes one."""
    state_dir = _hand_made_run(tmp_path)
    agent = {"seq": 1, "profile": "analyze_execute", "provider": "claude"}
    bookkeeping = {"seq": 2, "profile": "(EPIC binding at run creation; no agent)", "provider": ""}
    markers = ["no-such-marker"]
    assert ev.count_leaks(state_dir, "run", markers, lines=[agent, bookkeeping]) == (0, 0)
    assert ev.count_leaks(state_dir, "run", markers, lines=[agent]) == (0, 1)
    assert ev.count_leaks(state_dir, "run", markers) == (0, 1)
    assert ev.count_leaks(state_dir, "run", ["The issue."], lines=[agent, bookkeeping]) == (1, 0)


def test_a_commit_named_in_capitals_is_a_leak_of_it(tmp_path):
    state_dir = _hand_made_run(tmp_path)
    context = state_dir / "read-context" / "run" / "analyze_execute-1-0123456789ab"
    (context / "issue.md").write_text("See ABCDEF0123.\n")
    assert ev.count_leaks(state_dir, "run", ["abcdef0"], lines=[{"seq": 2}])[0] == 1


_REFERENCE_NAMED_AS = {
    "full": lambda task: task["reference_sha"],
    "abbreviated": lambda task: task["reference_sha"][:10],
    "seven-characters": lambda task: task["reference_sha"][:7],
    "capitals": lambda task: task["reference_sha"].upper(),
}
_REFERENCE_NAMED_IN = {
    "prompt": lambda state_dir: _agent_steps(state_dir)[-1] / "prompt.md",
    "read-context": lambda state_dir: _launch_contexts(state_dir)[-1] / "issue.md",
}


@pytest.mark.parametrize("named_as", _REFERENCE_NAMED_AS.values(), ids=_REFERENCE_NAMED_AS)
@pytest.mark.parametrize("named_in", _REFERENCE_NAMED_IN.values(), ids=_REFERENCE_NAMED_IN)
def test_the_reference_commit_shown_to_an_agent_invalidates_the_comparison(
    world, tmp_path, named_as, named_in
):
    """The statement check refuses it, so the scan must see it: a run can name the fix to its
    agent in a prompt or a read-context file as well as a statement can. Held to a real task,
    whose markers are its own hidden tests and pinned commits."""
    real = ev.load_manifest()["tasks"][0]
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    clean = _score(world, "fx-slugify", state_dir=state_dir, held_to=real)
    assert clean["counts"]["hidden_leaks"] == 0

    with named_in(state_dir).open("a", encoding="utf-8") as stream:
        stream.write(f"\nThe fix is {named_as(real)}.\n")
    record = _score(world, "fx-slugify", state_dir=state_dir, held_to=real)
    assert record["counts"]["hidden_leaks"] == 1
    assert "hidden_leaks" not in record["unknown_counts"]
    assert "hidden_test_leak" in record["unverified_reasons"]
    assert record["outcome"] == ev.UNVERIFIED

    manifest, prereg, records = _world()
    assert _decide(manifest, prereg, records)["decision"] == ev.GO
    _treated(records)["counts"]["hidden_leaks"] = record["counts"]["hidden_leaks"]
    result = _decide(manifest, prereg, records)
    assert result["decision"] == ev.NOT_EVALUATED and "pinned commit" in result["why"]


def test_the_pinned_base_shown_to_an_agent_is_a_leak_too(world, tmp_path):
    """The statement check refuses it as well, and the reference is its child."""
    real = ev.load_manifest()["tasks"][0]
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    with (_agent_steps(state_dir)[0] / "prompt.md").open("a", encoding="utf-8") as stream:
        stream.write(f"\nStart from {real['base_sha']}.\n")
    record = _score(world, "fx-slugify", state_dir=state_dir, held_to=real)
    assert record["counts"]["hidden_leaks"] == 1


def test_nobody_saying_the_run_was_checked_leaves_safety_unassessed(world):
    assert _score(world, "fx-slugify")["safety_assessed"] is False
    assert _score(world, "fx-slugify", safety_evidence="  ")["safety_assessed"] is False
    checked = _score(world, "fx-slugify", safety_evidence="issue comment")
    assert checked["safety_assessed"] is True and checked["safety_failures"] == []


def test_a_merged_pull_request_is_a_safety_failure_whatever_the_operator_passed(world, tmp_path):
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _edit_state(state_dir, counted_merged_prs=["https://github.com/owner/repo/pull/1"])
    record = _score(world, "fx-slugify", state_dir=state_dir)
    assert record["safety_failures"] == ["the run merged a pull request"]


@pytest.mark.parametrize(
    "task, status, stdout, altered, reasons",
    [
        ("fx-slugify", "pass", "", 0, []),
        ("fx-slugify", "timeout", "", 0, ["pristine_checkers_timeout"]),
        ("fx-slugify", "not_run", "", 0, ["pristine_checkers_not_run"]),
        # The candidate left this module alone: its source failed, no checker hid it.
        (
            "fx-slugify",
            "fail",
            "FAILED tests/test_slug.py::test_cases\n",
            0,
            ["pristine_checkers_failed"],
        ),
        # The candidate's own copy passed, and it changed that very module.
        (
            "skip_hides_a_regression",
            "fail",
            "FAILED tests/test_slug.py::test_cases\n",
            1,
            ["checker_altered", "pristine_checkers_failed"],
        ),
        # A runner that names no module cannot clear the candidate.
        ("fx-slugify", "fail", "", 1, ["checker_altered", "pristine_checkers_failed"]),
    ],
)
def test_what_the_pristine_checkers_say_decides_the_run(
    world, task, status, stdout, altered, reasons
):
    record = _score(world, task, pristine_checkers=lambda commit: ev.OracleRun(status, stdout))
    assert record["pristine_checkers"]["status"] == status
    assert record["counts"]["checker_altered"] == altered
    assert record["unverified_reasons"] == reasons
    assert record["outcome"] == (ev.UNVERIFIED if reasons else ev.VERIFIED)
    # What did not finish or could not start refutes nothing, and clears nothing.
    no_result = status not in ev.ORACLE_RESULTS
    assert ("checker_altered" in record["unknown_counts"]) == no_result
    assert (ev.measure_of(record, "verified_success")[0] is None) == no_result


def test_a_run_nobody_ran_the_pristine_checkers_on_is_not_verified(world):
    record = _score(world, "fx-slugify", pristine_checkers=None)
    assert record["unverified_reasons"] == ["pristine_checkers_not_run"]


def test_the_pristine_checkers_run_only_on_a_run_that_is_otherwise_a_success(world):
    """A whole suite more, for an answer the run already has."""
    called = []
    _score(
        world,
        "altered_checker",
        held_to={
            "id": "altered_checker",
            "memory_class": "memory_independent",
            "protected_paths": list(ev.PROTECTED_PATHS),
            "hidden_files": ["hidden_check.py"],
            "leak_markers": ["no-such-marker"],
        },
        pristine_checkers=lambda commit: called.append(commit) or ev.OracleRun("pass"),
    )
    assert called == []


def test_a_stopped_run_is_not_put_through_the_pristine_checkers(world):
    """Its outcome is the ceiling's whatever they say."""
    called = []
    record = _score(
        world,
        "fx-slugify",
        stopped_by="cost_ceiling",
        stopped_reason="past the ceiling",
        pristine_checkers=lambda commit: called.append(commit) or ev.OracleRun("pass"),
    )
    assert called == [] and record["outcome"] == "cost_ceiling"
    assert record["pristine_checkers"]["status"] == "not_run"


@pytest.mark.parametrize("real", ev.load_manifest()["tasks"], ids=lambda task: task["id"])
def test_every_real_task_scores_an_altered_checker_as_altered(world, real):
    """The negative fixtures through the real manifest's own entries: an altered test module
    and an altered fixture file are found whichever task they are scored as."""
    for name, path in (
        ("weakened_existing_test", "tests/test_slug.py"),
        ("altered_checker", "tests/expected.json"),
    ):
        record = _score(world, name, held_to=real)
        assert record["altered_checkers"] == [path], name
        assert record["counts"]["checker_altered"] == 1, name
        assert "checker_altered" in record["unverified_reasons"], name


def test_a_candidate_git_cannot_read_is_no_clean_checker(world):
    """What git refused to say is not that nothing changed."""
    record = _score(world, "fx-slugify", base_commit="0" * 40)
    assert "checker_altered" in record["unknown_counts"]
    assert record["unverified_reasons"] == ["checker_inputs_unread"]
    assert ev.measure_of(record, "checker_altered") == (None, ev.UNKNOWN)


def test_candidate_changes_say_what_was_added_and_what_lost_a_line(tmp_path):
    fixtures = fx.load_fixture_tasks()
    repo = tmp_path / "repo"
    base = fx.make_base(fixtures, fixtures["bases"]["slug"], repo)
    fx._write_files(
        repo,
        {
            "tests/test_new.py": "x = 1\n",
            "tests/expected.json": "[]\n",
            "slug.py": "def slugify(text):\n    return text\n\n\nY = 1\n",
        },
    )
    (repo / "tests" / "data.bin").write_bytes(b"\0\1")
    fx._fixed_git(fixtures, repo, "add", "-A")
    fx._fixed_git(fixtures, repo, "commit", "-q", "-m", "Changes")
    head = fx._fixed_git(fixtures, repo, "rev-parse", "HEAD")
    changes = {c.path: c for c in ev.candidate_changes(str(repo), base, head)}
    assert changes["tests/test_new.py"] == ev.FileChange("tests/test_new.py", 0, True)
    assert changes["tests/expected.json"] == ev.FileChange("tests/expected.json", 1, False)
    assert changes["tests/data.bin"] == ev.FileChange("tests/data.bin", None, True)
    assert changes["slug.py"].removed == 1 and changes["slug.py"].new is False
    assert ev.candidate_changes(str(repo), "0" * 40, head) is None


def test_a_journal_line_that_does_not_parse_is_counted(tmp_path):
    state_dir = tmp_path / "state"
    logs = state_dir / "logs" / "run"
    logs.mkdir(parents=True)
    (logs / "events.jsonl").write_bytes(b'{"phase": "REVIEW"}\n{torn\n[1]\n')
    lines, bad, status = ev.read_journal(state_dir, "run")
    assert (lines, bad, status) == ([{"phase": "REVIEW"}], 2, "read")
    assert ev.read_journal(state_dir, "other") == ([], 0, "absent")
    (logs / "events.jsonl").write_bytes(b'{"phase": "REVIEW"}\n' + b"[" * 1_000_000 + b"\n")
    assert ev.read_journal(state_dir, "run") == ([{"phase": "REVIEW"}], 1, "read")


def test_duplicate_exploration_counts_read_only_calls_repeated_across_launches(tmp_path):
    state_dir = tmp_path / "state"
    logs = state_dir / "logs" / "run"
    first, second = logs / "001-analyze-execute-1", logs / "002-review-1"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "progress.log").write_text(
        "[00:00:01 ANALYZE_EXECUTE #2] agent started (fable)\n"
        "[00:00:02 ANALYZE_EXECUTE #2] Read src/a.py\n"
        "[00:00:03 ANALYZE_EXECUTE #2] Grep retry_after\n"
        "[00:00:04 ANALYZE_EXECUTE #2] Bash Run the tests\n"
        "[00:00:05 ANALYZE_EXECUTE #2] Bash Run the tests\n",
        encoding="utf-8",
    )
    (second / "progress.log").write_text(
        "[00:00:01 REVIEW #2] read src/a.py\n[00:00:02 REVIEW #2] Read src/b.py\n",
        encoding="utf-8",
    )
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == {
        "value": 1,
        "provenance": ev.MEASURED,
    }
    empty = tmp_path / "empty"
    (empty / "logs" / "run").mkdir(parents=True)
    assert ev.duplicate_exploration(empty, "run", ev.MEASURED) == {
        "value": None,
        "provenance": ev.UNKNOWN,
    }


_NOT_COUNTED = {"value": None, "provenance": ev.UNKNOWN}


def _two_agent_steps(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A state directory with two agent steps; only the first holds its progress file."""
    state_dir = tmp_path / "state"
    logs = state_dir / "logs" / "run"
    first, second = logs / "001-analyze-execute-1", logs / "002-review-1"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "progress.log").write_text("[00:00:01 X #1] Read src/a.py\n", encoding="utf-8")
    return state_dir, first, second


def test_a_missing_progress_file_is_no_measured_part(tmp_path):
    """The second launch's calls are not counted, so the first's zero repeats say nothing of
    the run: the one measured value this could give is one nobody saw."""
    state_dir, _, second = _two_agent_steps(tmp_path)
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == _NOT_COUNTED
    (second / "progress.log").write_text("[00:00:01 Y #1] Read src/a.py\n", encoding="utf-8")
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == {
        "value": 1,
        "provenance": ev.MEASURED,
    }


def test_a_progress_file_replaced_by_a_link_is_no_measured_part(tmp_path):
    state_dir, _, second = _two_agent_steps(tmp_path)
    aside = tmp_path / "aside.log"
    aside.write_text("[00:00:01 Y #1] Read src/b.py\n", encoding="utf-8")
    (second / "progress.log").symlink_to(aside)
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == _NOT_COUNTED


@pytest.mark.parametrize(
    "plant",
    [lambda path: path.mkdir(), os.mkfifo],
    ids=["directory", "fifo"],
)
def test_a_progress_file_that_is_no_file_is_no_measured_part(tmp_path, plant):
    state_dir, _, second = _two_agent_steps(tmp_path)
    plant(second / "progress.log")
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == _NOT_COUNTED


def test_a_progress_file_nobody_may_read_is_no_measured_part(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads whatever a file's mode says")
    state_dir, _, second = _two_agent_steps(tmp_path)
    progress = second / "progress.log"
    progress.write_text("[00:00:01 Y #1] Read src/b.py\n", encoding="utf-8")
    progress.chmod(0)
    try:
        assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == _NOT_COUNTED
    finally:
        progress.chmod(0o600)


def test_a_step_directory_replaced_by_a_link_is_no_measured_part(tmp_path):
    state_dir, _, second = _two_agent_steps(tmp_path)
    second.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "progress.log").write_text("[00:00:01 Y #1] Read src/b.py\n", encoding="utf-8")
    second.symlink_to(elsewhere)
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == _NOT_COUNTED


def test_only_an_agents_step_owes_a_progress_file(tmp_path):
    """A bookkeeping line's step holds none, and is no gap. An agent's line whose step is gone
    is one, however the steps that remain read."""
    state_dir, _, second = _two_agent_steps(tmp_path)
    agent = {"seq": 1, "profile": "analyze_execute", "provider": "claude"}
    bookkeeping = {"seq": 2, "profile": "(EPIC binding at run creation; no agent)", "provider": ""}
    assert second.is_dir() and not (second / "progress.log").exists()
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED, lines=[agent, bookkeeping]) == {
        "value": 0,
        "provenance": ev.MEASURED,
    }
    lost = {"seq": 3, "profile": "fix", "provider": "claude"}
    assert (
        ev.duplicate_exploration(state_dir, "run", ev.MEASURED, lines=[agent, bookkeeping, lost])
        == _NOT_COUNTED
    )


_OMITTED = "[3 progress line(s) omitted past the 1048576-byte bound]\n"


def test_a_progress_file_cut_at_its_bound_is_no_measured_count(tmp_path):
    """One `Read` and three lines nobody can see, any of them a repeat: a measured zero here
    could pass a criterion the run failed."""
    state_dir, first, second = _two_agent_steps(tmp_path)
    (second / "progress.log").write_text("[00:00:01 Y #1] Read src/b.py\n", encoding="utf-8")
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == {
        "value": 0,
        "provenance": ev.MEASURED,
    }
    with (second / "progress.log").open("a", encoding="utf-8") as stream:
        stream.write(_OMITTED)
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == _NOT_COUNTED


def test_the_controllers_own_omission_line_is_what_is_recognised(tmp_path):
    """Written by the logger, so that the two cannot drift apart: a bound or a wording the
    harness no longer knows would read as a whole file again."""
    state_dir = tmp_path / "state"
    logger = RunLogger(state_dir / "logs", "run")
    step = logger.begin_step("REVIEW", 1)
    step.append("[00:00:01 REVIEW #1] Read src/a.py")
    line = "[00:00:02 REVIEW #1] Bash " + "x" * 65536
    for _ in range(MAX_PROGRESS_LOG_BYTES // len(line) + 2):
        step.append(line)
    step.close()
    logger.close()
    held = (step.path / "progress.log").read_text(encoding="utf-8")
    assert f"omitted past the {MAX_PROGRESS_LOG_BYTES}-byte bound]\n" in held
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == _NOT_COUNTED


def test_a_tool_call_that_mentions_the_omission_is_not_one(tmp_path):
    """A read of this very harness, say: only the logger's whole line says lines were lost."""
    state_dir, _, second = _two_agent_steps(tmp_path)
    (second / "progress.log").write_text(
        f"[00:00:01 Y #1] Read {_OMITTED}"
        "[00:00:02 Y #1] Grep progress line(s) omitted past the 1048576-byte bound\n",
        encoding="utf-8",
    )
    assert ev.duplicate_exploration(state_dir, "run", ev.MEASURED) == {
        "value": 0,
        "provenance": ev.MEASURED,
    }


def test_a_truncated_progress_file_cannot_satisfy_a_non_regression_criterion(world, tmp_path):
    """Through `score_run` and the decision. The whole run passes the registered margin on its
    measured count, and with one launch's progress cut the criterion has nothing to read."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    steps = _agent_steps(state_dir)
    assert len(steps) > 1
    for step in steps:
        (step / "progress.log").write_text("[00:00:01 X #1] Read src/a.py\n", encoding="utf-8")
    whole = _score(world, "fx-slugify", state_dir=state_dir, evidence=ev.REAL)
    assert whole["usage"]["duplicate_exploration"] == {
        "value": len(steps) - 1,
        "provenance": ev.MEASURED,
    }
    with (steps[-1] / "progress.log").open("a", encoding="utf-8") as stream:
        stream.write(_OMITTED)
    cut = _score(world, "fx-slugify", state_dir=state_dir, evidence=ev.REAL)
    assert cut["usage"]["duplicate_exploration"] == _NOT_COUNTED

    prereg = ev.load_preregistration()
    criterion = next(
        c
        for c in prereg["comparisons"]["memory_benefit"]["non_regression"]
        if c["metric"] == "duplicate_exploration"
    )
    stats = prereg["statistics"]
    assert ev.evaluate_non_regression([(whole, whole)], criterion, stats)["verdict"] == "pass"
    held = ev.evaluate_non_regression([(whole, cut)], criterion, stats)
    assert held["verdict"] == "unknown" and held["provenance"][ev.UNKNOWN] == 1


def test_a_lost_progress_file_leaves_a_scored_run_with_no_exploration_count(world, tmp_path):
    """Through `score_run`, so the agent steps are the ones its own run log names."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    steps = _agent_steps(state_dir)
    assert len(steps) > 1
    for step in steps:
        (step / "progress.log").write_text("[00:00:01 X #1] Read src/a.py\n", encoding="utf-8")
    whole = _score(world, "fx-slugify", state_dir=state_dir)["usage"]["duplicate_exploration"]
    assert whole == {"value": len(steps) - 1, "provenance": ev.SYNTHETIC}

    (steps[-1] / "progress.log").unlink()
    lost = _score(world, "fx-slugify", state_dir=state_dir)["usage"]["duplicate_exploration"]
    assert lost == _NOT_COUNTED


# -- the oracle ------------------------------------------------------------------------------


def _oracle_setup(tmp_path) -> tuple[str, str, dict, list[str]]:
    fixtures = fx.load_fixture_tasks()
    base = fixtures["bases"]["slug"]
    repo = tmp_path / "repo"
    head = fx.make_base(fixtures, base, repo)
    overlay = {path: text.encode() for path, text in base["hidden_files"].items()}
    return str(repo), head, overlay, list(fixtures["oracle_command"])


def test_the_oracle_runs_the_hidden_files_on_the_exact_commit_and_leaves_no_trace(tmp_path):
    """At the fixture's base the hidden test fails: it holds what the statement asks for
    and the base does not do. Its file was never written into the repository."""
    repo, head, overlay, command = _oracle_setup(tmp_path)
    cfg = default_config()
    assert ev.run_oracle(repo, head, overlay, command, cfg).status == "fail"
    assert not (Path(repo) / "hidden_check.py").exists()
    passing = {
        "hidden_check.py": b"import unittest\n\n\nclass Ok(unittest.TestCase):\n"
        b"    def test_ok(self):\n        pass\n"
    }
    assert ev.run_oracle(repo, head, passing, command, cfg).status == "pass"


def test_the_oracle_gives_a_launch_the_controllers_isolation(tmp_path, monkeypatch):
    """The candidate's code runs as validation.commands run it: allow-listed, contained,
    without the operator's credential, and under `required` in the export-command sandbox."""
    monkeypatch.setenv("GITHUB_TOKEN", fx.OPERATOR_CREDENTIAL)
    repo, head, overlay, command = _oracle_setup(tmp_path)
    seen = []

    def runner(request):
        if request.command == command:
            seen.append(request)
            return execute(request.__class__(command=["true"], cwd=request.cwd))
        return execute(request)

    cfg = default_config()
    assert ev.run_oracle(repo, head, overlay, command, cfg, runner=runner).status == "pass"
    (request,) = seen
    assert request.contain_orphans is True and request.sandbox is None
    assert request.env_allowlist == cfg.execution.environment_names()
    assert "GITHUB_TOKEN" not in request.env_allowlist
    assert fx.OPERATOR_CREDENTIAL not in (request.env or {}).values()

    seen.clear()
    cfg.sandbox.mode = "required"
    denied = (str(tmp_path / "state"),)
    ev.run_oracle(repo, head, overlay, command, cfg, denied_roots=denied, runner=runner)
    (request,) = seen
    assert request.sandbox is not None


@pytest.mark.parametrize(
    "exit_code, timed_out, status",
    [(0, False, "pass"), (1, False, "fail"), (127, False, "fail"), (None, True, "timeout")],
)
def test_only_exit_0_is_a_pass(tmp_path, exit_code, timed_out, status):
    repo, head, overlay, command = _oracle_setup(tmp_path)

    def runner(request):
        real = execute(request) if request.command != command else None
        if real is not None:
            return real
        done = execute(request.__class__(command=["true"], cwd=request.cwd))
        done.exit_code, done.timed_out = exit_code, timed_out
        return done

    assert ev.run_oracle(repo, head, overlay, command, default_config(), runner=runner).status == (
        status
    )


def test_the_pristine_checkers_replace_all_a_candidate_holds_under_tests(tmp_path):
    """Its edit is undone and what it added is gone, and only `tests/` is touched: its source
    is what meets them. Nothing is written into the repository."""
    fixtures = fx.load_fixture_tasks()
    repo = tmp_path / "repo"
    base = fx.make_base(fixtures, fixtures["bases"]["slug"], repo)
    fx._write_files(
        repo,
        {
            "tests/test_slug.py": "# weakened\n",
            "tests/added.py": "# added\n",
            "slug.py": "# the candidate's source\n",
        },
    )
    fx._fixed_git(fixtures, repo, "add", "-A")
    fx._fixed_git(fixtures, repo, "commit", "-q", "-m", "Weaken")
    head = fx._fixed_git(fixtures, repo, "rev-parse", "HEAD")
    check = (
        "from pathlib import Path\n"
        "assert 'assertEqual' in Path('tests/test_slug.py').read_text()\n"
        "assert not Path('tests/added.py').exists()\n"
        "assert Path('slug.py').read_text() == '# the candidate\\'s source\\n'\n"
    )
    command = ["python3", "-c", check]
    cfg = default_config()
    assert ev.run_oracle(str(repo), head, {}, command, cfg).status == "fail"
    done = ev.run_oracle(str(repo), head, {}, command, cfg, pristine=(str(repo), base))
    assert done.status == "pass", done.stdout
    missing = ev.run_oracle(str(repo), head, {}, command, cfg, pristine=(str(repo), "0" * 40))
    assert missing.status == "not_run"


def test_an_oracle_that_could_not_run_is_not_run_never_a_pass(tmp_path):
    """A commit the store does not hold cannot be exported; so is a hidden file that would
    land outside the export refused. A real run reports each as what it is."""
    repo, _, overlay, command = _oracle_setup(tmp_path)
    missing = "0" * 40
    assert ev.run_oracle(repo, missing, overlay, command, default_config()).status == "not_run"
    _, head, _, _ = _oracle_setup(tmp_path / "other")
    outside = {"../outside.py": b"x"}
    other = str(tmp_path / "other" / "repo")
    assert ev.run_oracle(other, head, outside, command, default_config()).status == "not_run"
    assert not list(Path(os.environ.get("TMPDIR", "/tmp")).glob("outside.py"))


def test_a_base_that_fails_its_validation_says_which_modules(tmp_path):
    """The measurement a run host makes before any run: launched as a candidate's validation
    is, and naming what a task would have to leave out. The fixture's `retry` base fails
    its own visible test, and its `slug` base passes."""
    fixtures = fx.load_fixture_tasks()
    manifest = {
        "validation_command_prefix": ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    }
    failing = fx.make_base(fixtures, fixtures["bases"]["retry"], tmp_path / "retry")
    task = {"base_sha": failing, "validation_ignore": []}
    assert ev.verify_base_suite(str(tmp_path / "retry"), manifest, task, default_config(), 0) == {
        "status": "fail",
        "failing_modules": ["tests/test_retry.py"],
    }
    passing = fx.make_base(fixtures, fixtures["bases"]["slug"], tmp_path / "slug")
    task = {"base_sha": passing, "validation_ignore": []}
    assert ev.verify_base_suite(str(tmp_path / "slug"), manifest, task, default_config(), 0) == {
        "status": "pass",
        "failing_modules": [],
    }


def test_the_hidden_overlay_is_byte_exact_or_refused(tmp_path):
    repo, head, _, _ = _oracle_setup(tmp_path)
    task = {
        "id": "t",
        "reference_sha": head,
        "hidden_files": ["slug.py"],
    }
    overlay = ev.hidden_overlay(repo, task)
    assert overlay["slug.py"] == (Path(repo) / "slug.py").read_bytes()
    with pytest.raises(ev.EvaluationError, match="gives no exact"):
        ev.hidden_overlay(repo, {**task, "hidden_files": ["absent.py"]})


# -- collecting a real run's record ----------------------------------------------------------


def _pinned_state(world, tmp_path) -> Path:
    """A copy of a run's state, its records saying it ran under the pinned configuration.

    What a real run's state holds: its routing and sandbox pin as `autoforge run` recorded
    them, and each launch under that routing and enforced. A fake provider's run cannot
    make these itself, since it launches nothing in a sandbox.
    """
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    cfg = load_config_file(str(ev.REAL_RUN_CONFIG))
    _edit_state(
        state_dir,
        config_fingerprint=RecordedConfiguration(
            fingerprint=ConfigFingerprint.from_config(cfg, "/worktrees"),
            config_file=str(ev.REAL_RUN_CONFIG),
            source="created",
            recorded_at="2026-01-01T00:00:00+00:00",
        ).to_dict(),
        sandbox_pin=SandboxPin("required", "created", "2026-01-01T00:00:00+00:00").to_dict(),
    )

    def enforced(line: dict) -> None:
        if line.get("provider"):
            profile = cfg.profiles[line["profile"]]
            line |= {
                "provider": profile.provider,
                "model": profile.model,
                "effort": profile.effort,
                "sandbox": {"enforced": True},
            }

    _edit_journal(state_dir, enforced)
    return state_dir


def _collect_inputs(world, tmp_path, monkeypatch, **overrides) -> tuple[list[str], dict]:
    """A finished run's directories under `collect`, with the task's pin moved to its base."""
    monkeypatch.setenv(ev.OPT_IN, "1")
    root = world["workspace"] / "fx-slugify-1"
    checkout = str(root / "checkout")
    record = world["records"]["fx-slugify seed 1"]
    base = fx.load_fixture_tasks()["bases"]["slug"]["base_sha"]
    manifest = ev.load_manifest()
    manifest["tasks"][0]["base_tree"] = ev._git(checkout, "rev-parse", f"{base}^{{tree}}")
    manifest["tasks"][0]["validation_ignore"] = []
    values = {
        "--task": manifest["tasks"][0]["id"],
        "--arm": fx.BASELINE_ARM,
        "--seed": "1",
        "--out": str(tmp_path / "record.json"),
        "--approval": "the approval's URL",
        "--state-dir": str(_pinned_state(world, tmp_path)),
        "--repo": checkout,
        "--base-commit": base,
        "--live-head": record["final_candidate"],
        "--oracle-repo": checkout,
        **overrides,
    }
    args = ["collect"]
    for name, value in values.items():
        args += [name, value]
    return args, manifest


def _run_collect(args: list[str], manifest: dict, monkeypatch) -> int:
    monkeypatch.setattr(ev, "load_manifest", lambda: manifest)
    monkeypatch.setattr(ev, "hidden_overlay", lambda repo, task: {})
    monkeypatch.setattr(ev, "run_oracle", lambda *a, **k: ev.OracleRun("pass"))
    return ev.main(args)


def test_collect_scores_a_real_run_as_measured_and_records_the_approval(
    world, tmp_path, monkeypatch
):
    """The same scoring as a fixture's, called real: its values are measured, its record
    names the approval, and FULL evidence is still read from its own configuration (the
    pinned set here, which this run's fixture command is not)."""
    args, manifest = _collect_inputs(world, tmp_path, monkeypatch)
    assert _run_collect(args, manifest, monkeypatch) == 0
    record = ev.read_json(tmp_path / "record.json")
    assert record["evidence"] == ev.REAL
    assert record["approval"] == "the approval's URL"
    assert record["usage"]["cost_usd"]["provenance"] == ev.MEASURED
    assert record["settings_digest"] == manifest["equalized_settings"]["digest"]
    evidence = record["settings_evidence"]
    assert evidence["status"] == "unknown"
    (problem,) = evidence["problems"]
    assert "no record of the run holds" in problem and "workflow" in problem
    assert record["run_evidence"] is True and record["safety_assessed"] is False
    assert record["validation"] == "absent"
    assert record["outcome"] == ev.UNVERIFIED


def test_collect_needs_an_operators_word_before_the_safety_gate_is_clean(
    world, tmp_path, monkeypatch
):
    args, manifest = _collect_inputs(
        world, tmp_path, monkeypatch, **{"--safety-checked": "where it is recorded"}
    )
    assert _run_collect(args, manifest, monkeypatch) == 0
    record = ev.read_json(tmp_path / "record.json")
    assert record["safety_assessed"] is True
    assert record["safety_evidence"] == "where it is recorded"


def _recorded_routing(state_dir: Path, profile: str, **routing) -> None:
    data = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    recorded = data["config_fingerprint"]
    recorded["profiles"][profile] |= routing
    _edit_state(state_dir, config_fingerprint=recorded)


@pytest.mark.parametrize(
    "spoil, said",
    [
        (
            lambda d: _recorded_routing(d, "analyze_execute", model="another"),
            "profile analyze_execute was recorded with other routing",
        ),
        (
            lambda d: _recorded_routing(d, "fix", max_runtime_seconds=60),
            "profile fix was recorded with other routing",
        ),
        (
            lambda d: _edit_state(
                d,
                sandbox_pin=SandboxPin(
                    "disabled", "accepted", "2026-01-01T00:00:00+00:00"
                ).to_dict(),
            ),
            "pinned to sandbox.mode disabled",
        ),
        (
            lambda d: _edit_state(
                d,
                sandbox_pin=SandboxPin(
                    "required", "strengthened", "2026-01-01T00:00:00+00:00"
                ).to_dict(),
            ),
            "sandbox pin was strengthened during the run",
        ),
        (
            lambda d: _edit_journal(
                d, lambda line: line.get("provider") and line.update(model="another")
            ),
            "ran under other routing",
        ),
        (
            lambda d: _edit_journal(
                d, lambda line: line.get("provider") and line.update(sandbox={"enforced": False})
            ),
            "was not enforced",
        ),
        (
            lambda d: _edit_state(d, prompt_version="another"),
            "the run recorded prompt version another",
        ),
    ],
    ids=[
        "model",
        "limit",
        "disabled-sandbox",
        "pin-moved",
        "launched-model",
        "unenforced",
        "prompt-version",
    ],
)
def test_collect_refuses_a_run_its_own_records_show_under_other_settings(
    world, tmp_path, monkeypatch, capsys, spoil, said
):
    """The pinned file passed to `collect` says nothing of what the run did. A run made under
    another model or with the sandbox off writes no record claiming the pinned settings."""
    args, manifest = _collect_inputs(world, tmp_path, monkeypatch)
    spoil(Path(args[args.index("--state-dir") + 1]))
    assert _run_collect(args, manifest, monkeypatch) == 2
    assert said in capsys.readouterr().out
    assert not (tmp_path / "record.json").exists()


def test_collect_refuses_a_run_recorded_from_another_file(world, tmp_path, monkeypatch, capsys):
    args, manifest = _collect_inputs(world, tmp_path, monkeypatch)
    state_dir = Path(args[args.index("--state-dir") + 1])
    data = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    _edit_state(
        state_dir, config_fingerprint={**data["config_fingerprint"], "config_file": "/o.yaml"}
    )
    assert _run_collect(args, manifest, monkeypatch) == 2
    assert "the run was recorded from /o.yaml" in capsys.readouterr().out


def _recorded_from(state_dir: Path, cfg, config_file: str) -> None:
    _edit_state(
        state_dir,
        config_fingerprint=RecordedConfiguration(
            fingerprint=ConfigFingerprint.from_config(cfg, "/worktrees"),
            config_file=config_file,
            source="created",
            recorded_at="2026-01-01T00:00:00+00:00",
        ).to_dict(),
    )


@pytest.mark.parametrize(
    "setting, change",
    [
        (
            "workflow.max_review_rounds",
            lambda text: text + "\nworkflow:\n  max_review_rounds: 99\n",
        ),
        (
            "execution.max_correction_attempts",
            lambda text: text.replace("max_correction_attempts: 1", "max_correction_attempts: 3"),
        ),
        (
            "profiles.review_round_1.options.permission_mode",
            lambda text: text.replace(
                "permission_mode: bypassPermissions", "permission_mode: acceptEdits", 1
            ),
        ),
        (
            "safety.required_checks",
            lambda text: text.replace('- "ci"', '- "ci"\n    - "other"'),
        ),
    ],
    ids=["loop-bound", "correction-attempts", "profile-option", "required-checks"],
)
def test_a_file_changed_at_the_same_pathname_establishes_nothing(world, tmp_path, setting, change):
    """The run recorded this pathname from one configuration, and the pathname holds another
    today. Its routing is the same and so is the pathname, so nothing in the run can tell. That
    is `unknown`, naming the setting: never `established` because the path matched."""
    pinned = ev.REAL_RUN_CONFIG.read_text(encoding="utf-8")
    config = tmp_path / "run.yaml"
    config.write_text(change(pinned), encoding="utf-8")
    at_the_run = load_config_file(str(config))
    state_dir = _pinned_state(world, tmp_path)
    _recorded_from(state_dir, at_the_run, str(config))

    config.write_text(pinned, encoding="utf-8")
    today = load_config_file(str(config))
    assert ev.settings_digest(at_the_run) != ev.settings_digest(today)
    state = load_state(state_dir / "state.json")
    assert state.config_fingerprint["config_file"] == str(config)
    assert state.config_fingerprint["profiles"] == {
        name: routing.to_dict() for name, routing in ev.fingerprint_routing(today).items()
    }

    lines, _, _ = ev.read_journal(state_dir, state.run_id)
    evidence = ev.settings_evidence(
        state, ev._agent_lines(lines), today, True, config_file=str(config)
    )
    assert evidence["status"] == "unknown"
    assert setting in evidence["unrecorded"]
    assert any("no record of the run holds" in problem for problem in evidence["problems"])


def test_settings_are_established_only_when_a_record_holds_every_one(world, tmp_path, monkeypatch):
    """What stands between a run and `established` is the settings no record holds: the day
    something records them, nothing else here has to change."""
    state_dir = _pinned_state(world, tmp_path)
    cfg = load_config_file(str(ev.REAL_RUN_CONFIG))
    state = load_state(state_dir / "state.json")
    lines, _, _ = ev.read_journal(state_dir, state.run_id)
    agents = ev._agent_lines(lines)
    assert ev.settings_evidence(state, agents, cfg, True)["status"] == "unknown"
    monkeypatch.setattr(ev, "unrecorded_settings", lambda _cfg: [])
    assert ev.settings_evidence(state, agents, cfg, True) == {
        "status": "established",
        "problems": [],
        "unrecorded": [],
    }


def test_a_malformed_line_refuses_no_collect_and_establishes_nothing(world, tmp_path, monkeypatch):
    """A list where a profile goes used to raise out of `settings_evidence`: `collect` writes
    the record, with what that line decided `unknown`."""
    args, manifest = _collect_inputs(world, tmp_path, monkeypatch)
    state_dir = Path(args[args.index("--state-dir") + 1])
    _edit_agent_lines(state_dir, lambda line: line.update(profile=["fix"]))
    assert _run_collect(args, manifest, monkeypatch) == 0
    record = ev.read_json(tmp_path / "record.json")
    assert any("hold a malformed profile" in problem for problem in record["journal_problems"])
    assert record["usage"]["cost_usd"] == {"value": None, "provenance": ev.UNKNOWN}
    assert record["settings_evidence"]["status"] == "unknown"


def test_collect_marks_settings_a_run_did_not_record_as_unknown(world, tmp_path, monkeypatch):
    """Nothing contradicts the file, and nothing backs it: written, and marked, so that a
    comparison over it cannot read `GO`."""
    args, manifest = _collect_inputs(world, tmp_path, monkeypatch)
    state_dir = Path(args[args.index("--state-dir") + 1])
    _edit_state(state_dir, config_fingerprint={}, sandbox_pin={})
    assert _run_collect(args, manifest, monkeypatch) == 0
    evidence = ev.read_json(tmp_path / "record.json")["settings_evidence"]
    assert evidence["status"] == "unknown"
    assert evidence["problems"][:2] == [
        "the run recorded no configuration",
        "the run recorded no sandbox pin",
    ]


@pytest.mark.parametrize("ceiling", ev.CEILING_OUTCOMES)
def test_a_run_stopped_at_a_ceiling_keeps_its_evidence_and_what_it_spent(
    world, tmp_path, monkeypatch, ceiling
):
    """Stopping a run discards nothing it did: its candidates, its oracle results, its
    settings and the cost its launches already reported all reach the record."""
    args, manifest = _collect_inputs(
        world, tmp_path, monkeypatch, **{"--stopped-by": ceiling, "--reason": "past the ceiling"}
    )
    assert _run_collect(args, manifest, monkeypatch) == 0
    record = ev.read_json(tmp_path / "record.json")
    assert record["outcome"] == ceiling and record["stopped_reason"] == "past the ceiling"
    assert record["run_evidence"] is True
    assert record["settings_digest"] == manifest["equalized_settings"]["digest"]
    assert record["usage"]["cost_usd"] == {
        "value": pytest.approx(0.0015),
        "provenance": ev.MEASURED,
    }
    assert record["volatile"]["wall_clock_seconds"]["provenance"] == ev.MEASURED
    assert record["final_candidate"] and record["candidates"]
    assert record["oracle"][record["final_candidate"]] == "pass"


def test_a_stopped_run_with_a_lost_launch_keeps_what_is_known_of_its_cost(
    world, tmp_path, monkeypatch
):
    """Killed inside a launch, so its last cost was never reported: no measured total, and
    not nothing either. What it spent stays in the record and counts against the ceiling."""
    args, manifest = _collect_inputs(
        world,
        tmp_path,
        monkeypatch,
        **{"--stopped-by": "wall_clock_ceiling", "--reason": "past 8 hours"},
    )
    state_dir = Path(args[args.index("--state-dir") + 1])
    (_log_dir(state_dir) / "099-review-1").mkdir()
    assert _run_collect(args, manifest, monkeypatch) == 0
    record = ev.read_json(tmp_path / "record.json")
    assert record["usage"]["cost_usd"] == {
        "value": None,
        "provenance": ev.UNKNOWN,
        "known_part": pytest.approx(0.0015),
    }
    assert record["journal_problems"]
    assert record["settings_evidence"]["status"] == "unknown"
    # Nor is it known when that launch ended, so nor when the run did.
    wall = record["volatile"]["wall_clock_seconds"]
    assert wall["value"] is None and wall["provenance"] == ev.UNKNOWN and "known_part" in wall


def test_a_stopped_run_with_evidence_still_says_why(world, tmp_path, monkeypatch, capsys):
    args, manifest = _collect_inputs(
        world, tmp_path, monkeypatch, **{"--stopped-by": "cost_ceiling", "--reason": " "}
    )
    assert _run_collect(args, manifest, monkeypatch) == 2
    assert "stated reason" in capsys.readouterr().out
    assert not (tmp_path / "record.json").exists()


def test_a_stopped_run_with_part_of_its_evidence_is_refused(world, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ev.OPT_IN, "1")
    args = [
        "collect",
        "--task",
        "fix-entry-retarget",
        "--arm",
        fx.BASELINE_ARM,
        "--seed",
        "1",
        "--out",
        str(tmp_path / "record.json"),
        "--approval",
        "a",
        "--stopped-by",
        "cost_ceiling",
        "--reason",
        "r",
        "--state-dir",
        str(tmp_path),
    ]
    assert ev.main(args) == 2
    assert "--oracle-repo" in capsys.readouterr().out


def test_collect_refuses_a_run_outside_the_protocols_terms(world, tmp_path, monkeypatch, capsys):
    """Benchmark issue contamination and unpinned settings: a run on the project's own
    repository, a base of another tree, another configuration or no sandbox writes nothing."""
    out = tmp_path / "record.json"

    args, manifest = _collect_inputs(world, tmp_path, monkeypatch)
    manifest["forbidden_repositories"] = ["owner/repo"]
    assert _run_collect(args, manifest, monkeypatch) == 2
    assert "no disposable repository" in capsys.readouterr().out

    args, manifest = _collect_inputs(world, tmp_path, monkeypatch)
    manifest["tasks"][0]["base_tree"] = "0" * 40
    assert _run_collect(args, manifest, monkeypatch) == 2
    assert "pinned tree" in capsys.readouterr().out

    other = tmp_path / "other.json"
    other.write_text(json.dumps({"version": 1, "execution": {"command_timeout_seconds": 60}}))
    args, manifest = _collect_inputs(world, tmp_path, monkeypatch, **{"--config": str(other)})
    assert _run_collect(args, manifest, monkeypatch) == 2
    said = capsys.readouterr().out
    assert "pinned one" in said and "sandbox.mode: required" in said
    assert not out.exists()


# -- an oracle that gave no result -----------------------------------------------------------

_EARLIER, _FINAL = "a" * 40, "f" * 40
_NO_RESULT = ("timeout", "not_run")


@pytest.mark.parametrize(
    "earlier, final, lost",
    [
        ("pass", "pass", 0),
        ("pass", "fail", 1),
        ("fail", "pass", 0),
        ("fail", "fail", 0),
        # Decided by what did run, whatever the rest would have said.
        ("timeout", "pass", 0),
        ("not_run", "pass", 0),
        ("fail", "timeout", 0),
        ("fail", "not_run", 0),
        (None, "not_run", 0),
        # Left open by what did not.
        ("pass", "timeout", None),
        ("pass", "not_run", None),
        ("timeout", "fail", None),
        ("not_run", "fail", None),
        ("not_run", "timeout", None),
        # A run that ended on no candidate holds nothing that passes.
        ("pass", None, 1),
        ("fail", None, 0),
        ("timeout", None, None),
    ],
)
def test_a_lost_success_is_read_only_as_far_as_the_results_decide_it(earlier, final, lost):
    oracle = {_EARLIER: earlier} if earlier else {}
    if final:
        oracle[_FINAL] = final
    assert ev.oracle_counts(oracle, _FINAL if final else "", "")["lost_success"] == lost


@pytest.mark.parametrize(
    "status, false_oracle", [("pass", 0), ("fail", 1), ("timeout", None), ("not_run", None)]
)
def test_a_false_oracle_needs_the_oracle_to_have_said_so_of_what_was_certified(
    status, false_oracle
):
    assert ev.oracle_counts({_FINAL: status}, _FINAL, _FINAL)["false_oracle"] == false_oracle
    stale = {_EARLIER: status, _FINAL: "pass"}
    assert ev.oracle_counts(stale, _FINAL, _EARLIER)["false_oracle"] == false_oracle
    assert ev.oracle_counts({_FINAL: status}, _FINAL, "")["false_oracle"] == 0


def _overwritten_with_a_final_oracle_of(world, status: str) -> dict:
    """A right first candidate, then another, certified: what the oracle says of the second."""
    task = "correct_candidate_overwritten"
    first, final = world["records"][f"{task} seed 1"]["candidates"]
    return _score(world, task, oracle={first: "pass", final: status}.get)


@pytest.mark.parametrize("status", _NO_RESULT)
def test_a_final_candidate_the_oracle_could_not_assess_is_no_measured_loss(world, status):
    """The first candidate passed and nothing says the second did not, so this is neither a
    loss nor a clean count. It is still no success, whatever it would have said."""
    record = _overwritten_with_a_final_oracle_of(world, status)
    assert record["outcome"] == ev.UNVERIFIED
    assert record["unverified_reasons"] == [f"oracle_{status}"]
    assert record["unknown_counts"] == ["false_oracle", "lost_success"]
    for metric in ("lost_success", "false_oracle", "verified_success"):
        assert ev.measure_of(record, metric) == (None, ev.UNKNOWN), metric
    shown = ev._render_real_run(record)
    assert "    counts not known: false_oracle, lost_success" in shown


def test_what_the_results_do_establish_stays_measured(world):
    """Being careful about one oracle's silence takes no result away from the others."""
    measured = ev.SYNTHETIC  # A fixture's; what a real run's would be measured.
    lost = _overwritten_with_a_final_oracle_of(world, "fail")
    assert lost["unknown_counts"] == []
    assert ev.measure_of(lost, "lost_success") == (1.0, measured)
    assert ev.measure_of(lost, "false_oracle") == (1.0, measured)
    assert ev.measure_of(lost, "verified_success") == (0.0, measured)

    task = "correct_candidate_overwritten"
    first, final = world["records"][f"{task} seed 1"]["candidates"]
    kept = _score(world, task, oracle={first: "not_run", final: "pass"}.get)
    assert kept["outcome"] == ev.VERIFIED and kept["unknown_counts"] == []
    assert ev.measure_of(kept, "lost_success") == (0.0, measured)


def _take_the_oracles_word(record: dict, scored: dict) -> None:
    """A planned run of the synthetic world, as far as the oracle decides it, as ``scored`` is."""
    for key in ("outcome", "unverified_reasons", "oracle", "unknown_counts"):
        record[key] = scored[key]
    for key in ("lost_success", "false_oracle"):
        record["counts"][key] = scored["counts"][key]


def test_a_certified_candidate_nobody_could_assess_is_no_clean_count(world):
    """A human's push moved the final candidate, which passes; what was certified went
    unassessed. Its staleness alone says this run is no success, so that stays measured."""
    task = "stale_completed_node"
    record = world["records"][f"{task} seed 1"]
    certified, final = record["certificate"]["candidate"], record["final_candidate"]
    assert certified != final
    scored = _score(world, task, oracle={certified: "not_run", final: "pass"}.get)
    assert scored["unknown_counts"] == ["false_oracle"]
    assert ev.measure_of(scored, "lost_success") == (0.0, ev.SYNTHETIC)
    assert "stale_certificate" in scored["unverified_reasons"]
    assert ev.measure_of(scored, "verified_success") == (0.0, ev.SYNTHETIC)

    manifest, prereg, records = _world()
    assert _decide(manifest, prereg, records)["decision"] == ev.GO
    _take_the_oracles_word(_treated(records), scored)
    result = _decide(manifest, prereg, records)
    assert {c["metric"]: c["verdict"] for c in result["non_regression"]}["false_oracle"] == (
        "unknown"
    )
    assert result["decision"] == ev.INCONCLUSIVE and "incomplete" in result["why"]


@pytest.mark.parametrize("status", _NO_RESULT)
def test_an_oracle_that_gave_no_result_is_no_harm_where_a_failed_one_is(world, status):
    """Through `score_run` and the decision. Every treated run has a first candidate that
    passed. Read as a loss, an oracle that never finished on the second is a `NO_GO` on
    the arm, which nothing the oracle said supports."""
    manifest, prereg, records = _world()
    silent = _overwritten_with_a_final_oracle_of(world, status)
    for record in records:
        if record["arm"] == TREATMENT:
            _take_the_oracles_word(record, silent)
    result = _decide(manifest, prereg, records)
    verdicts = {c["metric"]: c["verdict"] for c in result["non_regression"]}
    for metric in (
        "lost_success",
        "false_oracle",
        "verified_success",
        "cost_per_verified_success",
    ):
        assert verdicts[metric] == "unknown", metric
    assert result["primary"]["verdict"] == "unknown"
    assert result["decision"] == ev.INCONCLUSIVE and "incomplete" in result["why"]

    refuted = _overwritten_with_a_final_oracle_of(world, "fail")
    for record in records:
        if record["arm"] == TREATMENT:
            _take_the_oracles_word(record, refuted)
    harmed = _decide(manifest, prereg, records)
    assert harmed["decision"] == ev.NO_GO and "lost_success" in harmed["why"]


# -- how long a run took ---------------------------------------------------------------------


class _RanForTwoHours(ScriptedProvider):
    """An agent that began now and, by its own stamps, ended two hours from now."""

    def __init__(self, **ending) -> None:
        super().__init__()
        self.ending = ending

    def execute(self, req):
        began = datetime.now(UTC)
        return dataclasses.replace(
            super().execute(req),
            started_at=began.isoformat(),
            finished_at=(began + timedelta(hours=2)).isoformat(),
            **self.ending,
        )


def _ended_by_a_launch_seven_hours_in(state_dir: Path, github, **ending) -> dict:
    """Through the unchanged engine, which saves and then launches: its last write is seven
    hours after the run's creation, and the launch it makes then ends the run."""
    eng = make_engine(state_dir, [], github=github, origin=True)
    provider = _RanForTwoHours(**ending)
    eng.providers._overrides = {"claude": provider, "opencode": provider}
    with pytest.raises(ExecutionError, match="State unchanged"):
        eng.run(max_steps=fx.MAX_STEPS)
    eng.close()
    saved = datetime.fromisoformat(load_state(state_dir / "state.json").updated_at)
    _edit_state(state_dir, created_at=(saved - timedelta(hours=7)).isoformat())
    return ev.score_run(
        ev.RunInput(
            evidence=ev.REAL,
            task={
                "id": "ended-by-its-last-launch",
                "memory_class": "memory_independent",
                "protected_paths": [],
                "hidden_files": [],
                "leak_markers": ["no-such-marker"],
            },
            arm=fx.BASELINE_ARM,
            seed=1,
            state_dir=state_dir,
            repo=str(state_dir.parent),
            base_commit="HEAD",
            live_head="",
            cfg=default_config(),
            oracle=lambda commit: "pass",
            manifest_digest="m",
            preregistration_digest="p",
        )
    )


@pytest.mark.parametrize(
    "ending, outcome",
    [({"exit_code": -1, "timed_out": True}, "timeout"), ({"exit_code": 3}, "incomplete")],
    ids=["timeout", "failed-exit"],
)
def test_the_launch_that_ended_a_run_is_part_of_how_long_it_took(
    tmp_state_dir, fake_github, ending, outcome
):
    """The engine does not save after a launch that timed out or failed, so the state's last
    write says seven hours of a run that took nine, and misses the ceiling it passed."""
    record = _ended_by_a_launch_seven_hours_in(tmp_state_dir, fake_github, **ending)
    assert record["outcome"] == outcome
    wall = record["volatile"]["wall_clock_seconds"]
    assert wall == {"value": pytest.approx(9 * 3600, abs=60), "provenance": ev.MEASURED}

    manifest, prereg, records = _world()
    assert _decide(manifest, prereg, records)["decision"] == ev.GO
    assert [c["ceiling"] for c in ev._ceiling_stopped([record], prereg)] == ["wall_clock_ceiling"]
    treated = _treated(records)
    treated["volatile"] = record["volatile"]
    result = _decide(manifest, prereg, records)
    assert [
        (c["task"], c["ceiling"], c["wall_clock_seconds"]) for c in result["ceiling_stopped"]
    ] == [(treated["task"], "wall_clock_ceiling", wall["value"])]
    assert result["decision"] == ev.INCONCLUSIVE and "ceiling" in result["why"]


def _last_launch_ran(state_dir: Path, began: float, ended: float) -> None:
    """The last agent line's stamps, in hours from the run's creation."""
    created = datetime.fromisoformat(load_state(state_dir / "state.json").created_at)

    def stamped(lines: list[dict]) -> None:
        [line for line in lines if line.get("provider")][-1].update(
            started_at=(created + timedelta(hours=began)).isoformat(),
            finished_at=(created + timedelta(hours=ended)).isoformat(),
        )

    _rewrite_lines(state_dir, stamped)


def test_a_run_inside_the_ceiling_is_not_at_it_and_a_later_write_ends_the_run(world, tmp_path):
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _last_launch_ran(state_dir, 7, 7.5)
    within = _score(world, "fx-slugify", state_dir=state_dir, evidence=ev.REAL)
    assert within["volatile"]["wall_clock_seconds"] == {
        "value": 7.5 * 3600,
        "provenance": ev.MEASURED,
    }
    assert ev._ceiling_stopped([within], ev.load_preregistration()) == []

    created = datetime.fromisoformat(load_state(state_dir / "state.json").created_at)
    _edit_state(state_dir, updated_at=(created + timedelta(hours=7.75)).isoformat())
    saved_last = _score(world, "fx-slugify", state_dir=state_dir, evidence=ev.REAL)
    assert saved_last["volatile"]["wall_clock_seconds"]["value"] == 7.75 * 3600


_NO_END_ESTABLISHED = {
    "refused-before-it-started": {"started_at": "", "finished_at": ""},
    "no-end": {"finished_at": ""},
    "no-offset": {"finished_at": "2099-01-01T00:00:00"},
    "garbled": {"finished_at": "soon"},
    "ends-before-it-began": {"finished_at": "2000-01-01T00:00:00+00:00"},
    "began-before-the-run": {"started_at": "2000-01-01T00:00:00+00:00"},
}


@pytest.mark.parametrize("stamps", _NO_END_ESTABLISHED.values(), ids=_NO_END_ESTABLISHED)
def test_a_launch_with_no_end_established_leaves_the_elapsed_time_unknown(world, tmp_path, stamps):
    """Only that: the rest of its line, and the run's other measures, read as they did. The
    last write stays what is known of the total, and nothing crashes on a naive stamp."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _rewrite_lines(
        state_dir, lambda lines: [line for line in lines if line.get("provider")][-1].update(stamps)
    )
    record = _score(world, "fx-slugify", state_dir=state_dir, evidence=ev.REAL)
    wall = record["volatile"]["wall_clock_seconds"]
    assert wall["value"] is None and wall["provenance"] == ev.UNKNOWN and wall["known_part"] >= 0
    assert ev.measure_of(record, "wall_clock_seconds") == (None, ev.UNKNOWN)
    assert record["journal_problems"] == [] and record["outcome"] == ev.VERIFIED


def test_a_stamp_of_another_type_is_a_malformed_line(world, tmp_path):
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _rewrite_lines(
        state_dir,
        lambda lines: [line for line in lines if line.get("provider")][-1].update(finished_at=7),
    )
    record = _score(world, "fx-slugify", state_dir=state_dir, evidence=ev.REAL)
    assert "1 line(s) of the run log hold a malformed finished_at" in record["journal_problems"]
    assert record["volatile"]["wall_clock_seconds"]["provenance"] == ev.UNKNOWN


@pytest.mark.parametrize(
    "fields, part_known",
    [
        ({"created_at": "soon"}, False),
        ({"created_at": "2026-01-01T00:00:00"}, False),
        ({"updated_at": "soon"}, True),
        ({"updated_at": "2000-01-01T00:00:00+00:00"}, True),
    ],
    ids=["no-start", "start-without-an-offset", "no-last-write", "last-write-before-the-start"],
)
def test_a_state_file_with_no_endpoint_established_gives_no_elapsed_time(
    world, tmp_path, fields, part_known
):
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _edit_state(state_dir, **fields)
    wall = _score(world, "fx-slugify", state_dir=state_dir, evidence=ev.REAL)["volatile"][
        "wall_clock_seconds"
    ]
    assert wall["value"] is None and wall["provenance"] == ev.UNKNOWN
    assert ("known_part" in wall) == part_known


@pytest.mark.parametrize(
    "lose",
    [
        lambda log: (log / "099-review-1").mkdir(),
        lambda log: (log / "events.jsonl").unlink(),
    ],
    ids=["a-launch", "the-whole-run-log"],
)
def test_lost_evidence_leaves_the_end_unknown_and_what_is_known_still_reaches_a_ceiling(
    world, tmp_path, lose
):
    """A run can be past the ceiling whenever its last launch ended: not knowing how far past
    takes nothing back, and the criterion still has no total to read. With no run log left,
    the state's last write is what is known."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    created = datetime.fromisoformat(load_state(state_dir / "state.json").created_at)
    _edit_state(state_dir, created_at=(created - timedelta(hours=9)).isoformat())
    lose(_log_dir(state_dir))
    record = _score(world, "fx-slugify", state_dir=state_dir, evidence=ev.REAL)
    wall = record["volatile"]["wall_clock_seconds"]
    assert wall["value"] is None and wall["provenance"] == ev.UNKNOWN
    assert wall["known_part"] >= 9 * 3600
    assert ev.measure_of(record, "wall_clock_seconds") == (None, ev.UNKNOWN)
    (stopped,) = ev._ceiling_stopped([record], ev.load_preregistration())
    assert stopped["ceiling"] == "wall_clock_ceiling"
    assert stopped["wall_clock_seconds"] == wall["known_part"]
    assert f"at least {wall['known_part']}" in ev._render_real_run(record)[1]


def test_an_operators_later_line_ends_nothing(world, tmp_path):
    """A refused `unblock` the next day neither writes the state nor runs anything of it."""
    state_dir = _copied_state(world, "fx-slugify", tmp_path)
    _last_launch_ran(state_dir, 1, 2)

    def refused_later(lines: list[dict]) -> None:
        seq = lines[-1]["seq"] + 1
        (_log_dir(state_dir) / f"{seq:03d}-blocked-unblock-1").mkdir()
        lines.append(
            {
                **lines[-1],
                "seq": seq,
                "phase": "BLOCKED-unblock",
                "profile": "(operator unblock; no agent)",
                "provider": "",
                "command": [],
                "metadata": {"applied": False},
                "parsed_result": None,
                "started_at": "2099-01-01T00:00:00+00:00",
                "finished_at": "2099-01-01T00:00:00+00:00",
            }
        )

    _rewrite_lines(state_dir, refused_later)
    record = _score(world, "fx-slugify", state_dir=state_dir, evidence=ev.REAL)
    assert record["journal_problems"] == []
    assert record["volatile"]["wall_clock_seconds"] == {
        "value": 2 * 3600,
        "provenance": ev.MEASURED,
    }


def test_a_fixtures_elapsed_time_is_synthetic_and_read_from_its_own_launches(world):
    """A scripted agent's canned stamps predate any run, which would leave every fixture's
    time unknown: the fixture's provider says when it ran, as an executor does."""
    for record in world["records"].values():
        wall = record["volatile"]["wall_clock_seconds"]
        assert wall["provenance"] == ev.SYNTHETIC and wall["value"] > 0, record["task"]
