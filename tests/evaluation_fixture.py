"""The evaluation baseline on fakes: the real engine, a scripted agent, a local origin (#318).

No test: ``make eval-fixture`` runs it, and ``tests/test_evaluation_fixture.py``
holds what it must keep showing. It plays ``tests/fixtures/evaluation/fixture_tasks.json``
through the unchanged :class:`~autoforge.engine.ControllerEngine`, with a
:class:`~autoforge.providers.ScriptedProvider` for every agent, ``FakeGitHub`` for
GitHub and a bare repository for its git data, then scores each run by
:func:`tests.evaluation.score_run`, the code a real run's record goes through.
No model is called, no credential is read and nothing is written outside the
directory it is given.

It is reproducible: every commit is made at a fixed date, so each base and
candidate has the SHA its fixture pins, and two runs give byte-identical
reports. Wall-clock time and run ids live in a record's ``volatile`` part, which
the report leaves out.

**What this shows and what it does not.** It shows that the harness classifies
a known trajectory correctly and that the single-agent path still holds: the
phases, controller-owned effects, exact candidate validation, the certificate,
a closed merge gate, no credential in a launch. A fixture's success says nothing
about any model, and its report says so.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import autoforge.child_env as child_env
import autoforge.engine as engine_module
import autoforge.providers as providers_module
from autoforge.config import default_config
from autoforge.evidence import parse_candidate
from autoforge.executor import select_environment
from autoforge.providers import ScriptedProvider
from autoforge.sandbox_bwrap import ProbeResult
from autoforge.state import load_state, utcnow_iso
from autoforge.transitions import Phase
from tests import evaluation
from tests.conftest import (
    EPIC,
    ISOLATED_GIT_ENV,
    ISOLATED_GIT_ENV_ALLOWLIST,
    ISOLATED_GIT_SWITCHES,
    ISSUE,
    NO_AUTO_MAINTENANCE,
    FakeGitHub,
    analyze_payload,
    assert_isolated_names,
    block,
    connect_origin,
    fix_payload,
    fixed,
    launch_environment,
    make_engine,
    review_result,
    reviewed_head_in,
)

FIXTURE_TASKS = evaluation.FIXTURES / "fixture_tasks.json"
BASELINE_ARM = "single_agent_no_memory"
# The controller's own typed writes on this path (ADR 0004): nothing else may appear.
CONTROLLER_EFFECTS = frozenset(
    {"create_pull_request", "create_pr_comment", "write_pr_body", "create_issue_comment"}
)
# What the single-agent path launches, and nothing else: no replan, no merge.
SINGLE_AGENT_PHASES = frozenset({"ANALYZE_EXECUTE", "REVIEW", "FIX"})
# A credential an operator's shell may hold. The launch must not inherit it.
OPERATOR_CREDENTIAL = "ghp_" + "F" * 36
# More than any trajectory here takes: a run that holds still ends the loop first.
MAX_STEPS = 30


class SimulatedCrash(BaseException):
    """The process dies here; a BaseException, so no ``except Exception`` handles it."""


def load_fixture_tasks() -> dict:
    return evaluation.read_json(FIXTURE_TASKS)


def _fixed_git(fixtures: Mapping, cwd: str | Path, *args: str) -> str:
    """git at the fixture's fixed date and identity, reading no configuration of the host's."""
    date = fixtures["commit_date"]
    env = {
        **select_environment(ISOLATED_GIT_ENV_ALLOWLIST),
        **ISOLATED_GIT_ENV,
        "GIT_AUTHOR_NAME": "AutoForge fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": "AutoForge fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_DATE": date,
    }
    res = subprocess.run(
        ["git", *ISOLATED_GIT_SWITCHES, *NO_AUTO_MAINTENANCE, "-C", str(cwd), *args],
        env=env,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise evaluation.EvaluationError(f"git {' '.join(args)}: {res.stderr.strip()}")
    return res.stdout.strip()


def _write_files(root: Path, files: Mapping[str, str]) -> None:
    for relpath, text in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def make_base(fixtures: Mapping, base: Mapping, root: Path) -> str:
    """The base repository at ``root``; its one commit's SHA, which the fixture pins."""
    root.mkdir(parents=True)
    _fixed_git(fixtures, root, "init", "-q", "-b", "main", "--object-format=sha1")
    _write_files(root, base["files"])
    _fixed_git(fixtures, root, "add", "-A")
    _fixed_git(fixtures, root, "commit", "-q", "-m", "Fixture base")
    return _fixed_git(fixtures, root, "rev-parse", "HEAD")


@contextmanager
def isolated_process(root: Path) -> Iterator[None]:
    """What ``tests/conftest.py``'s autouse fixtures do, for a run outside pytest.

    An empty operator home and agent configuration, a sandbox probe that answers
    without a launch and a credential in the environment the launches must not
    inherit. Restored afterwards, so that it nests inside those fixtures.
    """
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    data = root / "opencode-data"
    claude = root / "claude-config"
    data.mkdir(exist_ok=True)
    claude.mkdir(exist_ok=True)
    claude_files = [claude / "settings.json", claude / ".claude.json"]
    saved = (
        child_env.operator_home,
        providers_module.opencode_data_dir,
        providers_module.claude_config_files,
        engine_module.probe_sandbox,
    )
    environment = {name: os.environ.get(name) for name in ("GITHUB_TOKEN", "GH_TOKEN")}
    child_env.operator_home = lambda: str(home)  # type: ignore[assignment]
    providers_module.opencode_data_dir = lambda env: data  # type: ignore[assignment]
    providers_module.claude_config_files = lambda env: claude_files  # type: ignore[assignment]
    engine_module.probe_sandbox = lambda: ProbeResult(True, "", "0.0-fixture")  # type: ignore[assignment]
    os.environ.update(dict.fromkeys(environment, OPERATOR_CREDENTIAL))
    try:
        yield
    finally:
        (
            child_env.operator_home,
            providers_module.opencode_data_dir,
            providers_module.claude_config_files,
            engine_module.probe_sandbox,
        ) = saved
        for name, value in environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class FixtureProvider(ScriptedProvider):
    """A scripted agent that also reports a fixture's cost, as a provider's summary would, and
    when it ran, as an executor would: a scripted agent's canned stamps predate any run."""

    def __init__(self, script) -> None:
        super().__init__(script)
        self.next_cost: int | None = None

    def execute(self, req):
        self.next_cost = None
        started = utcnow_iso()
        result = super().execute(req)
        result.started_at, result.finished_at = started, utcnow_iso()
        if self.next_cost is not None:
            result.provider_summary["cost_micro_usd"] = self.next_cost
        return result


@dataclass
class _Agent:
    """Plays one trajectory's steps through a run, one per launch."""

    fixtures: Mapping
    base_sha: str
    gh: FakeGitHub
    steps: list = field(default_factory=list)
    provider: FixtureProvider | None = None
    engine: object | None = None
    isolation_failures: list = field(default_factory=list)
    launched: list = field(default_factory=list)

    def _commit(self, req, parent: str, source: str) -> str:
        cwd = Path(req.cwd)
        _fixed_git(self.fixtures, cwd, "checkout", "-q", "--detach", parent)
        _write_files(cwd, self.fixtures["sources"][source])
        _fixed_git(self.fixtures, cwd, "add", "-A")
        _fixed_git(self.fixtures, cwd, "commit", "-q", "--allow-empty", "-m", f"Apply {source}")
        return _fixed_git(self.fixtures, cwd, "rev-parse", "HEAD")

    def _check_isolation(self, req) -> None:
        env = launch_environment(self.engine, req)
        try:
            assert_isolated_names(env)
            if OPERATOR_CREDENTIAL in env.values():
                raise AssertionError("the operator's credential reached the launch")
        except AssertionError as exc:
            self.isolation_failures.append(f"{req.phase}: {exc}")

    def __call__(self, req) -> str:
        self.launched.append(req.phase)
        self._check_isolation(req)
        if not self.steps:
            return "the fixture's agent has no step left and prints no result"
        step = self.steps.pop(0)
        if "cost_micro_usd" in step:
            self.provider.next_cost = step["cost_micro_usd"]
        kind = step["do"]
        if kind == "implement":
            head = self._commit(req, self.base_sha, step["source"])
            overrides = {"tests": [step["claim"]]} if "claim" in step else {}
            return block(analyze_payload(head, issue_url=ISSUE, **overrides))
        if kind == "review":
            (pr,) = self.gh.prs.values()
            findings = [
                {
                    "id": f"R{self._round()}-F{i + 1}",
                    "classification": "non-blocked",
                    "title": title,
                    "location": "slug.py:1",
                    "required_resolution": title,
                }
                for i, title in enumerate(step.get("findings", ()))
            ]
            return block(review_result(self._round(), pr.head_sha, findings))
        if kind == "fix":
            reviewed = reviewed_head_in(req.prompt)
            head = self._commit(req, reviewed, step["source"])
            return block(fix_payload(reviewed, head, [fixed(f"R{self._round()}-F1")]))
        if kind == "blocked":
            return block(
                {
                    "phase": req.phase,
                    "status": "blocked",
                    "issue_url": ISSUE,
                    "message": step["message"],
                }
            )
        raise evaluation.EvaluationError(f"unknown fixture step {kind!r}")

    def _round(self) -> int:
        """The review round in progress, or the one a FIX answers: reviews launched so far."""
        return self.launched.count("REVIEW")


def _expected_problems(record: Mapping, expect: Mapping) -> list[str]:
    """Where a record differs from what its fixture says it must classify as."""
    problems = []
    for key, wanted in expect.items():
        found = record["counts"][key] if key in record["counts"] else record[key]
        if found != wanted:
            problems.append(f"{key} is {found!r}, not {wanted!r}")
    return problems


def _single_agent_path(launched: list[str]) -> bool:
    return (
        bool(launched) and launched[0] == "ANALYZE_EXECUTE" and set(launched) <= SINGLE_AGENT_PHASES
    )


def run_scenario(
    fixtures: Mapping,
    scenario: Mapping,
    workspace: Path,
    manifest_digest: str,
    preregistration_digest: str,
) -> tuple[dict, dict[str, bool]]:
    """One trajectory through the real engine; its record and the harness's checks of it."""
    base = fixtures["bases"][scenario["base"]]
    root = workspace / f"{scenario['task']}-{scenario['seed']}"
    checkout = root / "checkout"
    state_dir = root / "state"
    base_sha = make_base(fixtures, base, checkout)
    state_dir.mkdir(parents=True)

    cfg = default_config()
    cfg.validation.commands = (
        [] if scenario.get("no_validation") else [list(fixtures["validation_command"])]
    )
    gh = FakeGitHub()
    statement = base["statement"]
    if scenario.get("statement_suffix"):
        statement = f"{statement}\n\n{scenario['statement_suffix']}"
    gh.add_issue(ISSUE, scenario["task"], body=statement, parent=EPIC)
    if scenario.get("crash_after"):
        gh.write_failures.append((scenario["crash_after"], SimulatedCrash(), True))

    agent = _Agent(fixtures, base_sha, gh, steps=list(scenario["steps"]))
    provider = FixtureProvider(agent)
    agent.provider = provider

    def engine(*, origin: bool):
        built = make_engine(state_dir, None, github=gh, cfg=cfg, workdir=checkout, origin=origin)
        built.providers = providers_module.ProviderRegistry(
            overrides={"claude": provider, "opencode": provider}
        )
        agent.engine = built
        return built

    eng = engine(origin=True)
    # As `autoforge run` does before its first save: what collect holds a run to.
    eng.record_run_configuration()
    eng.save()
    crashed = False
    try:
        eng.run(max_steps=MAX_STEPS)
    except SimulatedCrash:
        # A new controller process on the same state, git remote and GitHub.
        crashed = True
        origin = eng.origin
        eng.close()
        eng = engine(origin=False)
        connect_origin(eng, origin)
        eng.origin = origin
        eng.load()
        eng.run(max_steps=MAX_STEPS)

    state = load_state(state_dir / "state.json")
    if scenario.get("human_push_after"):
        pushed = _human_push(
            fixtures,
            checkout,
            gh.get_pr_head_sha(state.current_pr_url),
            scenario["human_push_after"],
        )
        eng.origin.publish(checkout, pushed, state.current_branch)
    # What GitHub holds now, read as the fake follows its origin: never the state's copy.
    live_head = gh.get_pr_head_sha(state.current_pr_url) if state.current_pr_url else ""
    eng.close()

    task = {
        "id": scenario["task"],
        "memory_class": scenario["memory_class"],
        "protected_paths": base["protected_paths"],
        "hidden_files": list(base["hidden_files"]),
        "leak_markers": base["leak_markers"],
    }
    overlay = {path: text.encode("utf-8") for path, text in base["hidden_files"].items()}
    record = evaluation.score_run(
        evaluation.RunInput(
            evidence=evaluation.FIXTURE,
            task=task,
            arm=BASELINE_ARM,
            seed=scenario["seed"],
            state_dir=state_dir,
            repo=str(checkout),
            base_commit=base_sha,
            live_head=live_head,
            cfg=cfg,
            oracle=lambda commit: (
                evaluation.run_oracle(
                    str(checkout), commit, overlay, fixtures["oracle_command"], cfg
                ).status
            ),
            manifest_digest=manifest_digest,
            preregistration_digest=preregistration_digest,
            pristine_checkers=lambda commit: evaluation.run_oracle(
                str(checkout),
                commit,
                {},
                fixtures["validation_command"],
                cfg,
                pristine=(str(checkout), base_sha),
            ),
        )
    )
    pinned = base_sha == base["base_sha"]
    return record, _checks(fixtures, scenario, record, state, gh, agent, checkout, pinned, crashed)


def _human_push(fixtures: Mapping, checkout: Path, parent: str, source: str) -> str:
    """A commit made on the PR's head by someone other than the run: the input changed."""
    scratch = Path(tempfile.mkdtemp(prefix="human-push-", dir=checkout.parent))
    try:
        _fixed_git(fixtures, checkout, "worktree", "add", "-q", "--detach", str(scratch), parent)
        _write_files(scratch, fixtures["sources"][source])
        _fixed_git(fixtures, scratch, "add", "-A")
        _fixed_git(fixtures, scratch, "commit", "-q", "-m", "A human's push")
        return _fixed_git(fixtures, scratch, "rev-parse", "HEAD")
    finally:
        _fixed_git(fixtures, checkout, "worktree", "remove", "--force", str(scratch))
        shutil.rmtree(scratch, ignore_errors=True)


def _checks(
    fixtures: Mapping,
    scenario: Mapping,
    record: Mapping,
    state,
    gh: FakeGitHub,
    agent: _Agent,
    checkout: Path,
    base_pinned: bool,
    crashed: bool,
) -> dict[str, bool]:
    """What this run shows of the single-agent path's own guarantees."""
    writes = [write[0] for write in gh.effect_writes]
    certificate = state.review_certificate
    certified = parse_candidate(certificate["candidate"]).commit if certificate else ""
    views = state.validation_evidence()
    checks = {
        "the base is the pinned commit": base_pinned,
        "the single-agent path ran": _single_agent_path(agent.launched),
        "only the controller's typed effects were written": set(writes) <= CONTROLLER_EFFECTS,
        "nothing was merged and the run stopped at the closed gate": not gh.merges
        and state.phase not in (Phase.MERGE, Phase.DONE),
        "no launch held the operator's credential": not agent.isolation_failures,
        "each record names its commit's real tree": all(
            _fixed_git(fixtures, checkout, "rev-parse", f"{v.candidate.commit}^{{tree}}")
            == v.candidate.tree
            for v in views
        ),
        "the certificate names the reviewed head": not certificate
        or certified == state.reviewed_head_sha,
        "the run classified as its fixture says": not _expected_problems(
            record, scenario["expect"]
        ),
    }
    if scenario.get("crash_after"):
        checks["the crash happened"] = crashed
        checks["the crash repeated no effect"] = writes.count(scenario["crash_after"]) == 1
        checks["the crash relaunched no agent"] = agent.launched.count("ANALYZE_EXECUTE") == 1
        checks["the run recovered to READY_FOR_MERGE"] = state.phase is Phase.READY_FOR_MERGE
    return checks


def run_all(
    workspace: Path, *, groups: tuple[str, ...] = ("baseline", "negative")
) -> tuple[list[dict], dict[str, dict[str, bool]]]:
    """Every scenario of ``groups``; the records and the checks, by ``<task> seed <n>``."""
    fixtures = load_fixture_tasks()
    manifest_digest = evaluation.digest(evaluation.load_manifest())
    preregistration_digest = evaluation.digest(evaluation.load_preregistration())
    records, checks = [], {}
    with isolated_process(workspace / "isolation"):
        for group in groups:
            for scenario in fixtures[group]:
                record, shown = run_scenario(
                    fixtures, scenario, workspace, manifest_digest, preregistration_digest
                )
                records.append(record)
                checks[f"{scenario['task']} seed {scenario['seed']}"] = shown
    return records, checks


def fixture_report(workspace: Path, *, groups: tuple[str, ...] = ("baseline", "negative")) -> dict:
    """The report of a fixture run: harness correctness, and no model-quality verdict."""
    records, checks = run_all(workspace, groups=groups)
    return evaluation.build_report(
        records,
        evaluation.load_manifest(),
        evaluation.load_preregistration(),
        harness_checks=checks,
    )


def report_bytes(report: Mapping) -> bytes:
    """The canonical bytes two runs of the same inputs must share."""
    return (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tests.evaluation_fixture", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--out", help="write report.json and its digest here (a directory)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="autoforge-eval-") as workspace:
        report = fixture_report(Path(workspace))
    data = report_bytes(report)
    print(data.decode("utf-8") if args.json else evaluation.render(report))
    print(f"report sha256 {evaluation.hashlib.sha256(data).hexdigest()}")
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_bytes(data)
    return 0 if report["harness_correctness"]["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
