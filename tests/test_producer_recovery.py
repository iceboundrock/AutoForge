"""Evidence-driven recovery of a producing step, end to end (#320).

Offline, as ``tests/test_validation_gates.py`` runs the gates: real git
repositories and a local bare origin under ``tmp_path``, real Python
validators the controller runs on each candidate's exported tree, the
in-memory GitHub and scripted agents. What is held:

- a negative verdict vetoes the agent's ``success``, whatever its result
  claims, and the decision taken on it is saved with the record and the
  failure count it reads, in one write;
- an empty validation set is ``not-run``, never a pass;
- progress is read from objective deltas between the task's candidates;
- the strategy change is opt-in, once per producing task, within the
  failure bound, and with it off every prompt is what it was;
- the bound stops the step with every judged candidate kept in the state,
  the best one named, and nothing published;
- a decision written into the state by hand is refused at load;
- an ``unblock`` opens a new producing task in the same step;
- a failing FIX never moves the PR head the controller published.

The pure decisions are in ``tests/test_task_recovery.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from autoforge import cli
from autoforge.config import default_config
from autoforge.engine import render_validation_failure
from autoforge.errors import StateError, ValidationInconclusiveError
from autoforge.evidence import MAX_FAILURE_COUNT, parse_record
from autoforge.state import load_state
from autoforge.task_recovery import Ledger
from autoforge.transitions import Phase

from .conftest import (
    BRANCH,
    GIT_IDENT,
    PR,
    FakeGitHub,
    analyze_payload,
    block,
    connect_origin,
    fixed,
    fixer,
    git_out,
    implement,
    make_engine,
    publish_pr_head,
    scripted,
)

PY = sys.executable
IMPLEMENTATION_BRANCH = "autoforge/2"


def needs(name: str) -> list[str]:
    """A validator that passes only on a tree holding ``name``."""
    return [
        PY,
        "-c",
        f"import os, sys\nif os.path.exists({name!r}):\n    sys.exit(0)\n"
        f"print({name!r} + ' is missing')\nsys.exit(1)",
    ]


FAILS = [PY, "-c", "import sys; print('the suite failed'); sys.exit(3)"]
FAILURE_HEADER = "## The controller's validation failed"
STRATEGY_HEADER = "## The controller asks for another approach"


def config(*commands: list[str], strategy_change: bool = False):
    cfg = default_config()
    cfg.validation.commands = [list(c) for c in commands]
    cfg.workflow.producer_strategy_change = strategy_change
    return cfg


def saved(eng):
    return load_state(eng.paths.state_file)


def ledger_of(state) -> Ledger:
    return Ledger.from_dict(state.producer_recovery)


def head(req) -> str:
    return git_out("-C", req.cwd, "rev-parse", "HEAD")


def reports_head(req) -> str:
    """An agent that changes nothing and hands back its worktree's HEAD again."""
    return block(analyze_payload(head(req)))


def writes(*names: str, removes: tuple[str, ...] = ()):
    """An agent that commits ``names`` (and deletes ``removes``) and reports the commit."""

    def agent(req) -> str:
        for name in names:
            (Path(req.cwd) / name).write_text(f"{name}\n", encoding="utf-8")
            git_out("-C", req.cwd, "add", name)
        for name in removes:
            git_out("-C", req.cwd, "rm", "-q", name)
        git_out("-C", req.cwd, *GIT_IDENT, "commit", "-q", "-m", "Work on the feature (#2)")
        return block(analyze_payload(head(req)))

    return agent


def producing(tmp_state_dir, gh, script, *commands, strategy_change: bool = False):
    cfg = config(*commands, strategy_change=strategy_change)
    eng = make_engine(tmp_state_dir, script, github=gh, cfg=cfg, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    return eng


def resumed(eng, tmp_state_dir, gh, script=None):
    eng.close()
    eng2 = make_engine(tmp_state_dir, script, github=gh, cfg=eng.config)
    eng2.origin = eng.origin
    connect_origin(eng2, eng.origin)
    eng2.load()
    return eng2


def nothing_published(eng, gh) -> None:
    assert eng.state.effect_records == [] and eng.state.completion_context == {}
    assert gh.effect_writes == [] and gh.prs == {}
    assert eng.origin.head(IMPLEMENTATION_BRANCH) is None


def summary(ledger: Ledger) -> list[tuple[str, str, str]]:
    return [(r.verdict.value, r.progress.value, r.decision.value) for r in ledger.receipts]


def status_text(eng) -> str:
    return "\n".join(cli._render_producer_recovery(saved(eng)))


# -- the verdict vetoes the agent's claim -------------------------------------------------


def test_a_negative_verdict_vetoes_the_agents_success_in_the_write_that_counts_it(
    tmp_state_dir,
):
    """The agent reports ``success`` and passing tests; the controller's FAIL decides."""

    def claims_success(req) -> str:
        implement(req)
        return block(analyze_payload(head(req), tests=["pytest: 412 passed", "all green"]))

    gh = FakeGitHub()
    eng = producing(tmp_state_dir, gh, scripted(claims_success), FAILS)
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE"
    assert "did not pass" in out.message and "progress first" in out.message
    s = saved(eng)
    (record,) = s.validation_records
    (receipt,) = ledger_of(s).receipts
    # One write: the record, the counter naming it and the receipt deciding on it.
    assert s.producer_validation_failure["last_record_seq"] == record["seq"]
    assert receipt.judgment.failure.record_seq == record["seq"]
    assert receipt.failure_count == s.producer_validation_failure["count"] == 1
    assert (receipt.verdict.value, receipt.decision.value) == ("fail", "retry")
    assert receipt.candidate_commit == record["candidate"]["commit"]
    assert receipt.judgment.candidate_tree == record["candidate"]["tree"]
    assert receipt.launch_seq == s.launch_seq
    assert ledger_of(s).step.phase is Phase.ANALYZE_EXECUTE
    nothing_published(eng, gh)


def test_missing_validation_is_recorded_as_not_run_never_as_a_pass(tmp_state_dir):
    gh = FakeGitHub()
    eng = producing(tmp_state_dir, gh, scripted(implement))
    assert eng.step().next_phase == "REVIEW"
    s = saved(eng)
    (receipt,) = ledger_of(s).receipts
    assert (receipt.verdict.value, receipt.decision.value) == ("not-run", "promote")
    assert receipt.judgment.definitions == 0 and receipt.judgment.passed == 0
    assert receipt.candidate_commit == s.current_head_sha
    text = status_text(eng)
    assert "not-run (no validation commands are configured: nothing was checked, not a pass)" in (
        text
    )
    assert ": pass (" not in text and "Most validation definitions passed" not in text


def test_a_promoted_candidate_is_the_one_authoritative_sha_and_keeps_its_receipt(tmp_state_dir):
    """The candidate transition leaves one SHA: the pushed head, which its receipt names."""
    gh = FakeGitHub()
    eng = producing(tmp_state_dir, gh, scripted(implement, writes("a.txt")), needs("a.txt"))
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    assert eng.step().next_phase == "REVIEW"
    s = saved(eng)
    failed, promoted = ledger_of(s).receipts
    assert summary(ledger_of(s)) == [("fail", "first", "retry"), ("pass", "new-pass", "promote")]
    assert promoted.candidate_commit == s.current_head_sha
    assert eng.origin.head(IMPLEMENTATION_BRANCH) == s.current_head_sha
    assert failed.candidate_commit != s.current_head_sha
    # Kept past the transition, until another producing step judges a candidate.
    assert s.phase is Phase.REVIEW and ledger_of(s).step.phase is Phase.ANALYZE_EXECUTE


# -- progress and the strategy change -------------------------------------------------------


def test_progress_is_read_from_objective_deltas_between_the_tasks_candidates(tmp_state_dir):
    gh = FakeGitHub()
    script = scripted(implement, writes("note.txt"), writes("a.txt"))
    eng = producing(tmp_state_dir, gh, script, needs("a.txt"), needs("b.txt"))
    phases = [eng.step().next_phase for _ in range(MAX_FAILURE_COUNT)]
    assert phases == ["ANALYZE_EXECUTE", "ANALYZE_EXECUTE", "BLOCKED"]
    assert summary(ledger_of(saved(eng))) == [
        ("fail", "first", "retry"),
        # Other code, the same failure: progress, not a repeat.
        ("fail", "changed-candidate", "retry"),
        # Definition [0] passes now.
        ("fail", "new-pass", "stop"),
    ]
    nothing_published(eng, gh)


def test_a_stagnation_asks_for_another_approach_once(tmp_state_dir):
    gh = FakeGitHub()
    script = scripted(implement, reports_head, writes("a.txt"))
    eng = producing(tmp_state_dir, gh, script, needs("a.txt"), strategy_change=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    out = eng.step()
    assert out.next_phase == "ANALYZE_EXECUTE"
    assert "asks it for another approach" in out.message and "progress none" in out.message
    assert eng.step().next_phase == "REVIEW"
    first, second, third = (call.prompt for call in eng.provider.calls)
    assert FAILURE_HEADER not in first and STRATEGY_HEADER not in first
    assert FAILURE_HEADER in second and STRATEGY_HEADER not in second
    assert third.count(STRATEGY_HEADER) == 1
    assert third.index(FAILURE_HEADER) < third.index(STRATEGY_HEADER)
    s = saved(eng)
    assert summary(ledger_of(s)) == [
        ("fail", "first", "retry"),
        ("fail", "none", "retry-new-approach"),
        ("pass", "new-pass", "promote"),
    ]
    assert eng.origin.head(IMPLEMENTATION_BRANCH) == s.current_head_sha


def test_the_dry_run_says_when_the_strategy_change_is_on(tmp_state_dir):
    gh = FakeGitHub()
    on = producing(tmp_state_dir, gh, scripted(), needs("a.txt"), strategy_change=True)
    notes = "\n".join(on.step(dry_run=True).plan.notes)
    assert "workflow.producer_strategy_change is on" in notes
    on.config.workflow.producer_strategy_change = False
    notes = "\n".join(on.step(dry_run=True).plan.notes)
    assert "producer_strategy_change" not in notes


def test_a_second_stagnation_is_a_plain_retry_and_the_bound_stops_it(tmp_state_dir):
    gh = FakeGitHub()
    script = scripted(implement, reports_head, reports_head)
    eng = producing(tmp_state_dir, gh, script, needs("a.txt"), strategy_change=True)
    phases = [eng.step().next_phase for _ in range(MAX_FAILURE_COUNT)]
    assert phases == ["ANALYZE_EXECUTE", "ANALYZE_EXECUTE", "BLOCKED"]
    assert summary(ledger_of(saved(eng))) == [
        ("fail", "first", "retry"),
        ("fail", "none", "retry-new-approach"),
        ("fail", "none", "stop"),
    ]
    assert len(eng.provider.calls) == MAX_FAILURE_COUNT, "no launch past the bound"
    nothing_published(eng, gh)


def test_with_the_strategy_change_off_every_prompt_is_what_it_was(tmp_state_dir):
    gh = FakeGitHub()
    script = scripted(implement, reports_head, writes("a.txt"))
    eng = producing(tmp_state_dir, gh, script, needs("a.txt"))
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    s = saved(eng)
    view = parse_record(s.validation_records[-1])
    assert eng.step().next_phase == "REVIEW"
    third = eng.provider.calls[2].prompt
    assert STRATEGY_HEADER not in "".join(call.prompt for call in eng.provider.calls)
    assert render_validation_failure(view, 2) in third
    assert render_validation_failure(view, 2) == render_validation_failure(view, 2, False)
    assert summary(ledger_of(saved(eng)))[1] == ("fail", "none", "retry")


# -- the bound, the kept candidates and the operator ----------------------------------------


def regressing_run(tmp_state_dir, gh):
    """Three failures: one check passes, then none (a regression), then the same tree again."""
    script = scripted(writes("a.txt"), writes("note.txt", removes=("a.txt",)), reports_head)
    eng = producing(tmp_state_dir, gh, script, needs("a.txt"), needs("b.txt"))
    for _ in range(MAX_FAILURE_COUNT):
        eng.step()
    return eng


def test_budget_exhaustion_stops_with_every_candidate_kept_and_the_best_named(tmp_state_dir):
    gh = FakeGitHub()
    eng = regressing_run(tmp_state_dir, gh)
    s = saved(eng)
    assert s.phase is Phase.BLOCKED and s.stop_code == "producer-validation-bound"
    ledger = ledger_of(s)
    assert summary(ledger) == [
        ("fail", "first", "retry"),
        ("fail", "regressed", "retry"),
        ("fail", "none", "stop"),
    ]
    best, last = ledger.receipts[0], ledger.receipts[2]
    assert ledger.best() == best and best.judgment.passed == 1
    assert f"the last is {last.candidate_commit[:12]}, left in the agent worktree" in (
        s.block_reason
    )
    assert (
        f"the one that passed the most validation definitions is {best.candidate_commit[:12]} "
        "(1 of 2)"
    ) in s.block_reason
    # The receipts outlive the stop that cleared the failure counter.
    assert s.producer_validation_failure == {}
    worktree = eng.provider.calls[-1].cwd
    assert git_out("-C", worktree, "rev-parse", "HEAD") == last.candidate_commit
    git_out("-C", worktree, "cat-file", "-e", f"{best.candidate_commit}^{{commit}}")
    nothing_published(eng, gh)
    text = status_text(eng)
    assert "Producer recovery:" in text and "producing task 1" in text
    for receipt in ledger.receipts:
        assert receipt.candidate_commit[:12] in text
    assert f"commit {best.candidate_commit[:12]} (1 of 2)" in text


def test_a_crash_after_the_stop_decision_blocks_without_relaunching(tmp_state_dir):
    gh = FakeGitHub()
    script = scripted(implement, implement, implement)
    eng = producing(tmp_state_dir, gh, script, FAILS)
    eng.step()
    eng.step()

    def crashing(*args, **kwargs):
        raise KeyboardInterrupt

    eng._producer_bound_text = crashing
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    s = saved(eng)
    assert s.phase is Phase.ANALYZE_EXECUTE
    assert ledger_of(s).last().decision.value == "stop", "saved with the record and the count"
    eng2 = resumed(eng, tmp_state_dir, gh, ["the implementer must not run again"])
    assert eng2.step().next_phase == "BLOCKED"
    assert eng2.provider.calls == []
    assert len(ledger_of(saved(eng2)).receipts) == MAX_FAILURE_COUNT


def test_a_decision_written_into_the_state_is_refused_never_followed(tmp_state_dir):
    """An agent cannot authorize its own extra attempt by rewriting its stop as a retry."""
    gh = FakeGitHub()
    eng = regressing_run(tmp_state_dir, gh)
    path = eng.paths.state_file
    data = json.loads(path.read_text(encoding="utf-8"))
    data["producer_recovery"]["receipts"][-1].update(decision="retry", stop_cause="")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="producer_recovery.*records the decision retry"):
        load_state(path)


def test_an_unblock_opens_a_new_producing_task_in_the_same_step(tmp_state_dir):
    gh = FakeGitHub()
    eng = regressing_run(tmp_state_dir, gh)
    before = ledger_of(saved(eng))
    result = eng.unblock("added b.txt's fixture by hand")
    assert result.unblocked and result.phase == "ANALYZE_EXECUTE"
    s = saved(eng)
    reopened = ledger_of(s)
    assert reopened.task == 2 and reopened.receipts == before.receipts
    assert reopened.step == before.step
    eng.provider._handler = scripted(reports_head)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    receipt = ledger_of(saved(eng)).last()
    assert (receipt.task, receipt.progress.value, receipt.decision.value) == (2, "first", "retry")


def test_an_inconclusive_validation_is_an_infra_receipt_and_publishes_nothing(tmp_state_dir):
    gh = FakeGitHub()
    eng = producing(tmp_state_dir, gh, scripted(implement), FAILS)

    def unavailable(*args, **kwargs):
        raise ValidationInconclusiveError("the validator could not be launched (2 attempts)")

    eng._validate_in_export = unavailable
    assert eng.step().next_phase == "BLOCKED"
    s = saved(eng)
    assert s.stop_code == "validation-inconclusive"
    (receipt,) = ledger_of(s).receipts
    assert (receipt.verdict.value, receipt.decision.value, receipt.stop_cause.value) == (
        "infra-error",
        "stop",
        "infra-error",
    )
    assert "not a pass" in status_text(eng)
    nothing_published(eng, gh)


# -- FIX: the published candidate is never overwritten --------------------------------------


def in_fix(tmp_state_dir, gh, script, *commands):
    eng = make_engine(tmp_state_dir, script, github=gh, cfg=config(*commands), origin=True)
    reviewed = publish_pr_head(eng)
    gh.add_pr(head_sha=reviewed)
    eng.state.phase = Phase.FIX
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = reviewed
    eng.state.reviewed_head_sha = reviewed
    eng.state.review_round = 1
    eng.state.last_review_comment_url = f"{PR}#issuecomment-100"
    eng.state.open_findings = [
        {
            "id": "R1-F1",
            "classification": "non-blocked",
            "title": "Typo",
            "location": "README.md",
            "required_resolution": "Fix the typo.",
        }
    ]
    return eng, reviewed


def test_a_failing_fix_never_moves_the_candidate_the_controller_published(tmp_state_dir):
    gh = FakeGitHub()
    eng, reviewed = in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1")), FAILS)
    assert eng.step().next_phase == "FIX"
    s = saved(eng)
    ledger = ledger_of(s)
    (receipt,) = ledger.receipts
    assert ledger.step.phase is Phase.FIX and ledger.step.pr_url == PR
    assert ledger.step.review_round == 1
    assert (receipt.verdict.value, receipt.decision.value) == ("fail", "retry")
    assert receipt.candidate_commit != reviewed
    # The PR head and the state's authoritative SHA stay the published candidate.
    assert eng.origin.head(BRANCH) == reviewed == s.current_head_sha
    assert gh.effect_writes == [] and s.effect_records == []


def test_the_receipts_go_with_the_issue(tmp_state_dir):
    gh = FakeGitHub()
    eng = producing(tmp_state_dir, gh, scripted(implement), FAILS)
    eng.step()
    s = saved(eng)
    assert ledger_of(s).receipts
    s.reset_for_new_issue("https://github.com/owner/repo/issues/3")
    assert s.producer_recovery == {}
    data = saved(eng).to_dict()
    data["current_issue_url"] = "https://github.com/owner/repo/issues/3"
    path = eng.paths.state_file
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="producer_recovery' names the step of another issue"):
        load_state(path)
