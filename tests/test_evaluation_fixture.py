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

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoforge.config import default_config
from autoforge.executor import execute
from autoforge.state import load_state
from tests import evaluation as ev
from tests import evaluation_fixture as fx

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


def _score(world, task: str, *, cfg=None, seed: int = 1) -> dict:
    """The run scored again, with an oracle that passes everything."""
    record = world["records"][f"{task} seed {seed}"]
    fixtures = fx.load_fixture_tasks()
    used = cfg
    if used is None:
        used = default_config()
        used.validation.commands = [list(fixtures["validation_command"])]
    root = world["workspace"] / f"{task}-{seed}"
    return ev.score_run(
        ev.RunInput(
            evidence=ev.FIXTURE,
            task={
                "id": task,
                "memory_class": record["memory_class"],
                "protected_paths": [],
                "leak_markers": ["no-such-marker"],
            },
            arm=fx.BASELINE_ARM,
            seed=seed,
            state_dir=root / "state",
            repo=str(root / "checkout"),
            base_commit=record["candidates"][0] + "^",
            live_head=record["final_candidate"],
            cfg=used,
            oracle=lambda commit: "pass",
            manifest_digest="m",
            preregistration_digest="p",
        )
    )


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


def test_a_journal_line_that_does_not_parse_is_counted(tmp_path):
    state_dir = tmp_path / "state"
    logs = state_dir / "logs" / "run"
    logs.mkdir(parents=True)
    (logs / "events.jsonl").write_bytes(b'{"phase": "REVIEW"}\n{torn\n[1]\n')
    lines, bad, status = ev.read_journal(state_dir, "run")
    assert (lines, bad, status) == ([{"phase": "REVIEW"}], 2, "read")
    assert ev.read_journal(state_dir, "other") == ([], 0, "absent")


def test_duplicate_exploration_counts_read_only_calls_repeated_across_launches(tmp_path):
    state_dir = tmp_path / "state"
    logs = state_dir / "logs" / "run"
    first, second = logs / "001-ANALYZE_EXECUTE-1", logs / "002-REVIEW-1"
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
        "--state-dir": str(root / "state"),
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
    assert record["validation"] == "absent"
    assert record["outcome"] == ev.UNVERIFIED


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
